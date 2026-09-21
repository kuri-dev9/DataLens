from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator
from jsonschema.exceptions import relevance

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
from datalens.observability import current_request_id, safe_log_value


LOG = logging.getLogger("datalens.agent")
# 애플리케이션 계층이 가로채는 내부 이벤트. SSE로 내보내지 않는다.
QUERYFORGE_SESSION_EVENT = "_queryforge_session"


class AgentError(RuntimeError):
    def __init__(self, message: str, *, tool_calls: int = 0, recovery_count: int = 0) -> None:
        super().__init__(message)
        self.tool_calls = tool_calls
        self.recovery_count = recovery_count
        # 실패해도 그때까지 확보한 결과는 버리지 않는다.
        self.datasets: tuple[DatasetReference, ...] = ()
        self.warnings: tuple[Any, ...] = ()
        self.failed_step: dict[str, Any] | None = None


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
    def __init__(self, failure: QueryForgeFailure | str | None = None, *, tool_calls: int = 0, recovery_count: int = 0) -> None:
        normalized = failure if isinstance(failure, QueryForgeFailure) else None
        super().__init__(normalized.code if normalized else str(failure or "QUERYFORGE_REJECTED"), tool_calls=tool_calls, recovery_count=recovery_count)
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
    # 성공한 도구 호출만 담는다. 다음 번 같은 질문에서 탐색을 건너뛰기 위한 재료다.
    plan: tuple[dict[str, Any], ...] = ()


class BoundedAgent:
    # 모델이 인자를 고쳐도 결과가 달라지지 않는 실패. 그 외는 도구 결과로 되돌려 자기수정시킨다.
    _SUMMARY_KEYS = {
        "schema": ("action", "table", "name_pattern", "limit"),
        "relationship": ("from_table", "to_table", "max_depth"),
        "describe": ("dataset_id", "level"),
        "transform": ("dataset_id",),
    }
    _TERMINAL_CODES = frozenset(
        {
            "UNAUTHORIZED",
            "INVALID_SESSION",
            "TABLE_NOT_ALLOWED",
            "CAPABILITY_NOT_ENABLED",
            "NOT_IMPLEMENTED",
            "DB_CONNECTION_ERROR",
            "CATALOG_NOT_INITIALIZED",
            "UPSTREAM_UNSTRUCTURED_ERROR",
        }
    )

    def __init__(
        self,
        provider: LLMProvider,
        queryforge: QueryForgeClient,
        *,
        max_tool_calls: int,
        recovery_budget: int,
        preview_rows: int = 5,
        timezone: str = "Asia/Seoul",
    ) -> None:
        self._provider = provider
        self._queryforge = queryforge
        self._max_tool_calls = max_tool_calls
        self._recovery_budget = recovery_budget
        self._preview_rows = preview_rows
        self._timezone = timezone

    async def run(
        self,
        session: Session,
        message: str,
        deadline: float,
        event_sink: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        hints: str | None = None,
    ) -> AgentResult:
        self._remaining(deadline)
        try:
            tools = await self._queryforge.discover_tools(self._remaining(deadline))
        except (TimeoutError, QueryForgeUnavailable) as exc:
            raise AgentUpstreamUnavailable("QueryForge capability discovery failed") from exc
        tool_map = {tool.name: tool for tool in tools}
        messages = self._context(session, message, hints)
        plan: list[dict[str, Any]] = []
        tool_count = 0
        recovery_count = 0
        qf_session_id = session.queryforge_session_id
        datasets: dict[str, DatasetReference] = {}
        warnings: list[Any] = []
        active_table = session.active_table
        active_period = deepcopy(session.active_period)
        step: dict[str, Any] = {}

        try:
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
                        plan=tuple(plan),
                    )

                messages.append(ProviderMessage("assistant", turn.content or "", turn.tool_calls))
                should_retry = False
                for call in turn.tool_calls:
                    if tool_count >= self._max_tool_calls:
                        raise AgentLimitError("Tool call budget exhausted")
                    tool_count += 1
                    step.clear()
                    step.update({"index": tool_count, "tool": call.name})
                    # 무엇을 하려는 단계인지 먼저 알린다. 인자 검증에서 걸러지는 호출도
                    # 사용자 화면에서는 하나의 시도로 보여야 한다.
                    intent = self._call_summary(call.name, call.arguments)
                    if intent:
                        step["intent"] = deepcopy(intent)
                    if event_sink is not None:
                        await event_sink(
                            "tool_call",
                            {"index": tool_count, "tool": call.name, "status": "started", "intent": intent},
                        )
                    definition = tool_map.get(call.name)
                    if definition is None:
                        raise AgentPolicyError("LLM requested a tool outside the allowlist")
                    validation_error = self._validate(definition, call.arguments)
                    if validation_error is not None:
                        self._log_tool_call(session, tool_count, call.name, call.arguments, False, 0, recovery_count < self._recovery_budget, code="INVALID_TOOL_ARGUMENTS", details={"reason": "schema_validation_failed"})
                        step["upstream_code"] = "INVALID_TOOL_ARGUMENTS"
                        step["detail"] = validation_error
                        if event_sink is not None:
                            await event_sink(
                                "tool_call",
                                {
                                    "index": tool_count, "tool": call.name, "status": "rejected",
                                    "elapsed_ms": 0, "intent": intent,
                                    "error": {"code": "INVALID_TOOL_ARGUMENTS", "detail": validation_error},
                                    "recovery": self._recovery_state(recovery_count, True),
                                },
                            )
                        recovery_count = self._claim_recovery(recovery_count)
                        messages.append(self._recovery_message(call.name, "INVALID_TOOL_ARGUMENTS", validation_error))
                        should_retry = True
                        break
                    tool_arguments = self._bounded_arguments(call.name, call.arguments)
                    tool_started = time.monotonic()
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
                    previous_qf_session_id = qf_session_id
                    qf_session_id = result.application_session_id or qf_session_id
                    if event_sink is not None and qf_session_id != previous_qf_session_id:
                        # 턴이 끝나기 전에도 dataset을 조회할 수 있도록 즉시 알린다.
                        await event_sink(QUERYFORGE_SESSION_EVENT, {"application_session_id": qf_session_id})
                    elapsed_ms = int((time.monotonic() - tool_started) * 1000)
                    if not result.ok:
                        failure = self._failure_of(result)
                        step["upstream_code"] = failure.code
                        step["detail"] = failure.hint or failure.message
                        will_recover = self._recoverable(failure) and recovery_count < self._recovery_budget
                        self._log_tool_call(session, tool_count, call.name, tool_arguments, False, elapsed_ms, will_recover, failure=failure)
                        if event_sink is not None:
                            await event_sink(
                                "tool_call",
                                {
                                    "index": tool_count, "tool": call.name, "status": "rejected",
                                    "elapsed_ms": elapsed_ms, "intent": intent,
                                    "error": {
                                        "code": failure.code,
                                        "detail": failure.hint or failure.message,
                                        "details": deepcopy(failure.safe_metadata),
                                    },
                                    "recovery": self._recovery_state(recovery_count, will_recover),
                                },
                            )
                        if self._recoverable(failure):
                            if recovery_count >= self._recovery_budget:
                                raise AgentQueryRejected(failure, tool_calls=tool_count, recovery_count=recovery_count)
                            recovery_count += 1
                            messages.append(self._queryforge_recovery_message(call.name, failure))
                            should_retry = True
                            break
                        raise AgentQueryRejected(failure, tool_calls=tool_count, recovery_count=recovery_count)
                    self._log_tool_call(session, tool_count, call.name, tool_arguments, True, elapsed_ms, False)
                    step.clear()
                    plan.append({"tool": call.name, "intent": deepcopy(intent)})
                    bounded = self._bounded_tool_result(result)
                    if event_sink is not None:
                        await event_sink(
                            "tool_call",
                            {
                                "index": tool_count, "tool": call.name, "status": "completed",
                                "elapsed_ms": elapsed_ms, "intent": intent,
                                "result": self._result_summary(bounded),
                            },
                        )
                    messages.append(
                        ProviderMessage(
                            "tool",
                            json.dumps(bounded, ensure_ascii=False, separators=(",", ":")),
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
        except AgentError as exc:
            # 실패 지점과 그때까지 확보한 결과를 예외에 실어 API 계층으로 넘긴다.
            exc.tool_calls = exc.tool_calls or tool_count
            exc.recovery_count = exc.recovery_count or recovery_count
            exc.datasets = tuple(datasets.values())
            exc.warnings = tuple(warnings)
            exc.failed_step = dict(step) or None
            raise


    @staticmethod
    def _log_tool_call(
        session: Session,
        step: int,
        tool: str,
        arguments: Any,
        ok: bool,
        elapsed_ms: int,
        recovery_attempt: bool,
        *,
        failure: QueryForgeFailure | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "request_id": current_request_id(),
            "session_id": session.session_id,
            "step": step,
            "tool": tool,
            "argument_keys": sorted(arguments) if isinstance(arguments, dict) else [],
            "ok": ok,
            "error_code": failure.code if failure else code,
            "error_details": safe_log_value(failure.safe_metadata if failure else details),
            "hint": failure.hint if failure else None,
            "elapsed_ms": elapsed_ms,
            "recovery_attempt": recovery_attempt,
        }
        if LOG.isEnabledFor(logging.DEBUG):
            fields["arguments"] = safe_log_value(arguments)
        LOG.log(logging.INFO if ok else logging.WARNING, "tool_call", extra=fields)

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
        # oneOf/anyOf 스키마에서 최상위 오류는 "is not valid under any of the given schemas"
        # 수준이라 모델이 무엇을 고쳐야 하는지 알 수 없다. 판별자가 맞는 가지까지 내려가
        # 구체적인 오류를 앞세우고, 해당 위치의 기대 스키마를 함께 돌려준다.
        primary = BoundedAgent._most_specific(max(errors, key=relevance))
        ordered = [primary, *(item for item in errors if item is not primary)]
        reported = [f"{BoundedAgent._error_path(item)}: {item.message}" for item in ordered[:3]]
        detail = " | ".join(reported)
        expected = json.dumps(primary.schema, ensure_ascii=False, separators=(",", ":"))
        if len(expected) <= 400:
            detail = f"{detail} | expected at {BoundedAgent._error_path(primary)}: {expected}"
        return detail[:1024]

    def _recovery_state(self, recovery_count: int, will_retry: bool) -> dict[str, Any]:
        return {"attempt": recovery_count + 1, "budget": self._recovery_budget, "will_retry": will_retry}

    @classmethod
    def _call_summary(cls, tool_name: str, arguments: Any) -> dict[str, Any]:
        """무엇을 하려는 호출인지 사용자에게 보여줄 최소 요약. 행 데이터는 담지 않는다."""
        if not isinstance(arguments, dict):
            return {}
        if tool_name == "query":
            return cls._query_summary(arguments)
        keys = cls._SUMMARY_KEYS.get(tool_name, ())
        return {key: arguments[key] for key in keys if isinstance(arguments.get(key), (str, int, bool))}

    @staticmethod
    def _query_summary(arguments: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        source = arguments.get("source")
        if isinstance(source, dict) and isinstance(source.get("table"), str):
            summary["table"] = source["table"]
        select = arguments.get("select")
        if isinstance(select, list):
            columns = [item["column"] for item in select if isinstance(item, dict) and isinstance(item.get("column"), str)]
            if columns:
                summary["select"] = columns[:10]
        aggregations = arguments.get("aggregations")
        if isinstance(aggregations, list):
            rendered = [
                f"{item.get('function')}({item.get('column')})" if item.get("column") else str(item.get("function"))
                for item in aggregations
                if isinstance(item, dict)
            ]
            if rendered:
                summary["aggregations"] = rendered[:10]
        group_by = arguments.get("group_by")
        if isinstance(group_by, list):
            grouped = [item for item in group_by if isinstance(item, str)]
            if grouped:
                summary["group_by"] = grouped[:10]
        scope = arguments.get("partition_scope")
        if isinstance(scope, dict):
            rendered_scope = {
                key: scope[key]
                for key in ("kind", "column", "from", "to")
                if isinstance(scope.get(key), (str, int))
            }
            if rendered_scope:
                summary["partition_scope"] = rendered_scope
        return summary

    @staticmethod
    def _result_summary(bounded: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for key in ("action", "table", "dataset_id", "row_count"):
            if isinstance(bounded.get(key), (str, int)):
                summary[key] = bounded[key]
        for key in ("tables", "columns", "relationships"):
            value = bounded.get(key)
            if isinstance(value, list):
                summary[f"{key}_count"] = len(value)
        if bounded.get("partitioned") is not None:
            summary["partitioned"] = bool(bounded["partitioned"])
        if bounded.get("truncated"):
            summary["truncated"] = True
        return summary

    @staticmethod
    def _error_path(error: Any) -> str:
        return "/".join(str(item) for item in error.absolute_path) or "$"

    @staticmethod
    def _most_specific(error: Any) -> Any:
        """판별자(const)가 어긋난 anyOf/oneOf 가지를 버리고 의도한 가지의 오류까지 내려간다."""
        while error.context:
            branches: dict[Any, list[Any]] = {}
            for item in error.context:
                branches.setdefault(item.schema_path[0] if item.schema_path else 0, []).append(item)
            viable = {
                key: items
                for key, items in branches.items()
                if not any(sub.validator in {"const", "enum"} for sub in items)
            }
            chosen = viable or branches
            error = min(chosen.values(), key=len)[0]
        return error

    def _claim_recovery(self, current: int) -> int:
        if current >= self._recovery_budget:
            raise AgentLimitError("Recovery budget exhausted")
        return current + 1

    @classmethod
    def _recoverable(cls, failure: QueryForgeFailure | None) -> bool:
        # retryable=False여도 인자를 고치면 통과할 수 있는 실패(UNKNOWN_COLUMN 등)가 많다.
        # 재시도로 결과가 달라질 수 없는 코드만 즉시 종료한다.
        return failure is not None and failure.code not in cls._TERMINAL_CODES

    @staticmethod
    def _failure_of(result: QueryForgeResult) -> QueryForgeFailure:
        if result.error is not None:
            return result.error
        # QueryForge가 structuredContent 없이 실패를 돌려준 경우에도 상류 코드를 남긴다.
        return QueryForgeFailure(
            code="UPSTREAM_UNSTRUCTURED_ERROR",
            message="QueryForge returned a failure without a structured error",
            retryable=False,
            hint="QueryForge tool response carried no error object",
        )

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
            # query.partition_scope를 만들려면 반드시 필요한 필드들. 빼면 모델이 파티션 키를 추측한다.
            "partitioned", "partition", "primary_key", "comment", "entity_labels",
            "parent_dataset_id",
        }
        bounded = {key: deepcopy(value) for key, value in result.payload.items() if key in allowed}
        if isinstance(bounded.get("preview"), list):
            bounded["preview"] = bounded["preview"][: self._preview_rows]
        # 목록을 말없이 자르면 모델은 전부 봤다고 착각한다. 찾던 컬럼이 잘려나간 뒤에도
        # 같은 자리를 계속 뒤지게 되므로, 잘랐다는 사실과 좁히는 방법을 함께 알린다.
        notices: list[dict[str, Any]] = []
        for key, limit in {"tables": 200, "columns": 120, "relationships": 20}.items():
            value = bounded.get(key)
            if isinstance(value, list) and len(value) > limit:
                notices.append(
                    {
                        "code": "DATALENS_TRUNCATED",
                        "field": key,
                        "action": "truncated",
                        "original_items": len(value),
                        "returned_items": limit,
                        "hint": f"{key} is truncated; narrow with name_pattern and call the tool again",
                    }
                )
                bounded[key] = value[:limit]
        if notices:
            bounded["truncated"] = True
            bounded["warnings"] = [*(bounded.get("warnings") or []), *notices]
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

    def _context(self, session: Session, message: str, hints: str | None = None) -> list[ProviderMessage]:
        now = datetime.now(ZoneInfo(self._timezone))
        state = {
            "now": now.isoformat(timespec="seconds"),
            "today": now.date().isoformat(),
            "timezone": self._timezone,
            "active_table": session.active_table,
            "active_period": session.active_period,
            "active_dataset_id": session.active_dataset_id,
        }
        messages = [
            ProviderMessage("system", load_system_prompt(session.locale)),
            ProviderMessage("system", f"DataLens session context: {json.dumps(state, ensure_ascii=False)}"),
        ]
        if hints:
            messages.append(ProviderMessage("system", f"DataLens memory:\n{hints}"))
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
