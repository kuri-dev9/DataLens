from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from jsonschema import Draft202012Validator

from datalens.domain.session import Session
from datalens.prompts import load_system_prompt
from datalens.ports.llm import (
    LLMInvalidResponse,
    LLMProvider,
    LLMTimeout,
    LLMUnavailable,
    OutputPolicy,
    ProviderMessage,
)
from datalens.ports.queryforge import (
    QueryForgeClient,
    QueryForgeFailure,
    QueryForgeResult,
    QueryForgeToolDefinition,
    QueryForgeUnavailable,
)


class AgentError(RuntimeError):
    pass


class AgentLimitError(AgentError):
    pass


class AgentTimeoutError(AgentError):
    pass


class AgentUpstreamUnavailable(AgentError):
    pass


class AgentInvalidUpstreamResponse(AgentError):
    pass


class AgentPolicyError(AgentError):
    pass


class AgentQueryRejected(AgentError):
    def __init__(self, failure: QueryForgeFailure | str | None = None) -> None:
        normalized = failure if isinstance(failure, QueryForgeFailure) else None
        super().__init__(normalized.code if normalized else str(failure or "QUERYFORGE_REJECTED"))
        self.failure = normalized


class AgentInternalError(AgentError):
    pass


@dataclass(frozen=True, slots=True)
class DatasetReference:
    dataset_id: str
    role: str = "primary"
    row_count: int | None = None
    columns: tuple[dict[str, Any], ...] = ()
    preview: tuple[dict[str, Any], ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "role": self.role,
            "row_count": self.row_count,
            "columns": list(self.columns),
            "preview": list(self.preview),
        }


@dataclass(frozen=True, slots=True)
class AgentResult:
    answer: str
    datasets: tuple[DatasetReference, ...]
    warnings: tuple[Any, ...]
    tool_calls: int
    recovery_count: int
    updated_session: Session


class BoundedAgent:
    _RECOVERABLE_CODES = frozenset({"MISSING_PARTITION_SCOPE", "UNKNOWN_COLUMN", "UNKNOWN_TABLE"})

    def __init__(
        self,
        provider: LLMProvider,
        queryforge: QueryForgeClient,
        *,
        max_tool_calls: int,
        recovery_budget: int,
        preview_rows: int = 5,
    ) -> None:
        self._provider = provider
        self._queryforge = queryforge
        self._max_tool_calls = max_tool_calls
        self._recovery_budget = recovery_budget
        self._preview_rows = preview_rows

    async def run(
        self,
        session: Session,
        message: str,
        deadline: float,
        event_sink: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AgentResult:
        self._remaining(deadline)
        try:
            tools = await self._queryforge.discover_tools(self._remaining(deadline))
        except (TimeoutError, QueryForgeUnavailable) as exc:
            raise AgentUpstreamUnavailable("QueryForge capability discovery failed") from exc
        tool_map = {tool.name: tool for tool in tools}
        messages = self._context(session, message)
        tool_count = 0
        recovery_count = 0
        qf_session_id = session.queryforge_session_id
        datasets: dict[str, DatasetReference] = {}
        warnings: list[Any] = []
        active_table = session.active_table
        active_period = deepcopy(session.active_period)

        while True:
            self._remaining(deadline)
            try:
                if event_sink is not None and hasattr(self._provider, "complete_stream"):
                    turn = await self._provider.complete_stream(
                        messages,
                        tools,
                        deadline,
                        OutputPolicy(),
                        lambda text: event_sink("token", {"text": text}),
                    )
                else:
                    turn = await self._provider.complete(messages, tools, deadline, OutputPolicy())
            except LLMTimeout as exc:
                raise AgentTimeoutError("LLM deadline exceeded") from exc
            except LLMUnavailable as exc:
                raise AgentUpstreamUnavailable("LLM provider failed") from exc
            except LLMInvalidResponse as exc:
                raise AgentInvalidUpstreamResponse("LLM provider returned an invalid response") from exc

            if not turn.tool_calls:
                answer = (turn.content or "").strip()
                if not answer:
                    raise AgentInternalError("LLM returned an empty final answer")
                updated = self._updated_session(
                    session,
                    message,
                    answer,
                    qf_session_id,
                    tuple(datasets),
                    active_table,
                    active_period,
                )
                return AgentResult(
                    answer=answer,
                    datasets=tuple(datasets.values()),
                    warnings=tuple(warnings),
                    tool_calls=tool_count,
                    recovery_count=recovery_count,
                    updated_session=updated,
                )

            messages.append(ProviderMessage("assistant", turn.content or "", turn.tool_calls))
            should_retry = False
            for call in turn.tool_calls:
                if tool_count >= self._max_tool_calls:
                    raise AgentLimitError("Tool call budget exhausted")
                tool_count += 1
                definition = tool_map.get(call.name)
                if definition is None:
                    raise AgentPolicyError("LLM requested a tool outside the allowlist")
                validation_error = self._validate(definition, call.arguments)
                if validation_error is not None:
                    recovery_count = self._claim_recovery(recovery_count)
                    messages.append(self._recovery_message(call.name, "INVALID_TOOL_ARGUMENTS", validation_error))
                    should_retry = True
                    break
                tool_arguments = self._bounded_arguments(call.name, call.arguments)
                tool_started = time.monotonic()
                if event_sink is not None:
                    await event_sink(
                        "tool_call", {"index": tool_count, "tool": call.name, "status": "started"}
                    )
                try:
                    result = await self._queryforge.call_tool(
                        call.name,
                        tool_arguments,
                        application_session_id=qf_session_id,
                        timeout_seconds=self._remaining(deadline),
                    )
                except TimeoutError as exc:
                    raise AgentTimeoutError("QueryForge call deadline exceeded") from exc
                except QueryForgeUnavailable as exc:
                    raise AgentUpstreamUnavailable("QueryForge call failed") from exc
                qf_session_id = result.application_session_id or qf_session_id
                if event_sink is not None:
                    await event_sink(
                        "tool_call",
                        {
                            "index": tool_count,
                            "tool": call.name,
                            "status": "completed",
                            "elapsed_ms": int((time.monotonic() - tool_started) * 1000),
                        },
                    )
                if not result.ok:
                    failure = result.error
                    if self._recoverable(failure):
                        if recovery_count >= self._recovery_budget:
                            raise AgentQueryRejected(failure)
                        recovery_count += 1
                        messages.append(self._queryforge_recovery_message(call.name, failure))
                        should_retry = True
                        break
                    raise AgentQueryRejected(failure)
                messages.append(
                    ProviderMessage(
                        "tool",
                        json.dumps(self._bounded_tool_result(result), ensure_ascii=False, separators=(",", ":")),
                        tool_name=call.name,
                    )
                )
                warnings.extend(result.payload.get("warnings") or [])
                reference = self._dataset_reference(result)
                if reference is not None:
                    datasets[reference.dataset_id] = reference
                    if event_sink is not None:
                        await event_sink("dataset", reference.public())
                if call.name == "query":
                    active_table = tool_arguments.get("source", {}).get("table", active_table)
                    scope = tool_arguments.get("partition_scope")
                    if isinstance(scope, dict) and scope.get("kind") == "time_range":
                        active_period = {key: str(scope[key]) for key in ("from", "to") if key in scope}
            if should_retry:
                continue

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentTimeoutError("Agent turn deadline exhausted")
        return remaining

    @staticmethod
    def _validate(tool: QueryForgeToolDefinition, arguments: Any) -> str | None:
        if not isinstance(arguments, dict):
            return "arguments must be a JSON object"
        errors = sorted(Draft202012Validator(tool.input_schema).iter_errors(arguments), key=lambda error: list(error.path))
        if not errors:
            return None
        error = errors[0]
        path = "/".join(str(item) for item in error.absolute_path) or "$"
        return f"{path}: {error.message}"[:512]

    def _claim_recovery(self, current: int) -> int:
        if current >= self._recovery_budget:
            raise AgentLimitError("Recovery budget exhausted")
        return current + 1

    @classmethod
    def _recoverable(cls, failure: QueryForgeFailure | None) -> bool:
        return failure is not None and failure.retryable

    @staticmethod
    def _failure_hint(failure: QueryForgeFailure) -> str:
        if failure.candidates:
            return f"Use the only candidate: {failure.candidates[0]!r}"
        return failure.hint or failure.message

    @staticmethod
    def _recovery_message(tool_name: str, code: str, detail: str) -> ProviderMessage:
        return ProviderMessage(
            "tool",
            json.dumps({"ok": False, "error": {"code": code, "detail": detail}}, ensure_ascii=False),
            tool_name=tool_name,
        )

    @staticmethod
    def _queryforge_recovery_message(
        tool_name: str, failure: QueryForgeFailure
    ) -> ProviderMessage:
        error = {
            "code": failure.code,
            "retryable": failure.retryable,
            "hint": failure.hint,
            "details": deepcopy(failure.safe_metadata),
        }
        return ProviderMessage(
            "tool",
            json.dumps({"ok": False, "error": error}, ensure_ascii=False, separators=(",", ":")),
            tool_name=tool_name,
        )

    def _bounded_tool_result(self, result: QueryForgeResult) -> dict[str, Any]:
        allowed = {
            "ok", "action", "tables", "table", "columns", "relationships", "dataset_id",
            "row_count", "preview", "summary", "statistics", "warnings", "truncated",
        }
        bounded = {key: deepcopy(value) for key, value in result.payload.items() if key in allowed}
        for key, limit in {
            "tables": 50,
            "columns": 50,
            "relationships": 20,
            "preview": self._preview_rows,
        }.items():
            if isinstance(bounded.get(key), list):
                bounded[key] = bounded[key][:limit]
        return bounded

    def _bounded_arguments(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        bounded = deepcopy(arguments)
        # Session identity is owned by DataLens, never by model-generated arguments.
        bounded.pop("session_id", None)
        if tool_name in {"query", "transform"}:
            requested = bounded.get("preview_rows", self._preview_rows)
            bounded["preview_rows"] = min(requested, self._preview_rows)
        return bounded

    def _dataset_reference(self, result: QueryForgeResult) -> DatasetReference | None:
        dataset_id = result.payload.get("dataset_id")
        if not isinstance(dataset_id, str):
            return None
        columns = result.payload.get("columns") or []
        preview = result.payload.get("preview") or []
        return DatasetReference(
            dataset_id=dataset_id,
            row_count=result.payload.get("row_count") if isinstance(result.payload.get("row_count"), int) else None,
            columns=tuple(item for item in columns if isinstance(item, dict)),
            preview=tuple(item for item in preview[: self._preview_rows] if isinstance(item, dict)),
        )

    @staticmethod
    def _context(session: Session, message: str) -> list[ProviderMessage]:
        state = {
            "active_table": session.active_table,
            "active_period": session.active_period,
            "active_dataset_id": session.active_dataset_id,
        }
        messages = [
            ProviderMessage("system", load_system_prompt(session.locale)),
            ProviderMessage("system", f"DataLens session context: {json.dumps(state, ensure_ascii=False)}"),
        ]
        for item in session.turn_state[-6:]:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str):
                messages.append(ProviderMessage(role, content))
        messages.append(ProviderMessage("user", message))
        return messages

    @staticmethod
    def _updated_session(
        session: Session,
        user_message: str,
        answer: str,
        qf_session_id: str | None,
        dataset_ids: tuple[str, ...],
        active_table: str | None,
        active_period: dict[str, str] | None,
    ) -> Session:
        history = (*session.turn_state, {"role": "user", "content": user_message}, {"role": "assistant", "content": answer})[-12:]
        return replace(
            session,
            queryforge_session_id=qf_session_id,
            active_dataset_id=dataset_ids[-1] if dataset_ids else session.active_dataset_id,
            active_table=active_table,
            active_period=active_period,
            turn_state=tuple(history),
        )
