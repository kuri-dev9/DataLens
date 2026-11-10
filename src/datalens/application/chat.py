from __future__ import annotations

import time
import logging
from typing import Any, Awaitable, Callable, Protocol

from datalens.application import answer_style
from datalens.application.agent import QUERYFORGE_SESSION_EVENT, AgentResult
from datalens.application.memory import MemoryService, plan_from_events, render_hints
from datalens.application.runtime_config import Mode, RuntimeConfig
from datalens.application.trace import TurnLog, blocks
from datalens.application.sessions import SessionService
from datalens.domain.memory import Recall
from datalens.domain.session import Session
from datalens.observability import bind_request_id, reset_request_id


class Agent(Protocol):
    async def run(
        self,
        session: Session,
        message: str,
        deadline: float,
        event_sink: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        hints: str | None = None,
        mode: Mode = "agent",
    ) -> AgentResult: ...


class ChatApplicationService:
    def __init__(
        self,
        sessions: SessionService,
        agent: Agent,
        memory: MemoryService | None = None,
        runtime: RuntimeConfig | None = None,
        turn_log: TurnLog | None = None,
    ) -> None:
        self._sessions = sessions
        self._agent = agent
        self._memory = memory
        self._runtime = runtime
        self._turn_log = turn_log
        # 진행 중인 실제 턴(request_id → 시작 시각). 프롬프트 실험은 이것이 비었을 때만 모델을 쓴다(사용자 질의 우선)
        self._active: dict[str, tuple[float, str]] = {}

    def active_turns(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [{"request_id": rid, "mode": mode, "elapsed_s": round(now - began, 1)}
                for rid, (began, mode) in list(self._active.items())]

    async def handle(
        self, session: Session, message: str, request_id: str, deadline: float, mode: Mode = "agent"
    ) -> dict:
        started = time.monotonic()
        self._active[request_id] = (started, mode)
        try:
            return await self._handle(session, message, request_id, deadline, mode, started)
        finally:
            self._active.pop(request_id, None)

    async def _handle(
        self, session: Session, message: str, request_id: str, deadline: float, mode: Mode, started: float
    ) -> dict:
        context = bind_request_id(request_id)
        received, style = message, None
        if mode == "evidence" and self._runtime is not None:
            # graphio `[답변 형식]` 구역만 화면 설정으로 바꿔 끼운다. 기록의 받은 원문은 바꾸기 전 것이다
            message, style = answer_style.apply(message, self._runtime.answer_style())
        recall = await self._recall(session, message, mode)
        try:
            result = await self._agent.run(session, message, deadline, hints=render_hints(recall), **self._mode_kwargs(mode))
        except Exception:
            self._feedback(recall, succeeded=False)
            raise
        finally:
            reset_request_id(context)
        await self._remember(session, message, recall, result, mode)
        committed = self._sessions.commit(result.updated_session)
        self._record(mode, result, started, committed.session_id, message, received, style, session.locale)
        logging.getLogger("datalens.agent").info(
            "agent_turn_completed",
            extra={
                "request_id": request_id,
                "session_id": committed.session_id,
                "operation": "agent_turn",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "status": "completed",
                "tool_call_count": result.tool_calls,
            },
        )
        return {
            "request_id": request_id,
            "session_id": committed.session_id,
            "mode": mode,
            "status": "completed",
            "answer": result.answer,
            "datasets": [dataset.public() for dataset in result.datasets],
            "warnings": list(result.warnings),
            "metadata": {
                "duration_ms": int((time.monotonic() - started) * 1000),
                "tool_calls": result.tool_calls,
                "recovery_count": result.recovery_count,
                "stop_reason": "completed",
                "memory": recall.public() if recall else None,
                "grounding": result.grounding,
                # 프롬프트가 컨텍스트 창에 들어갔는지. status != "ok"면 모델이 증거의
                # 일부를 못 봤다는 뜻이고, 답변의 "확인할 수 없다"는 그 결과일 수 있다.
                "context": result.context,
                # 이번 턴에 실은 것(모드·맥락 구성·파라미터·도구 수·이력 항목 수).
                "profile": result.profile,
            },
            "error": None,
        }

    async def handle_stream(
        self,
        session: Session,
        message: str,
        request_id: str,
        deadline: float,
        event_sink: Callable[[str, dict[str, Any]], Awaitable[None]],
        mode: Mode = "agent",
    ) -> dict:
        started = time.monotonic()
        self._active[request_id] = (started, mode)
        try:
            return await self._handle_stream(session, message, request_id, deadline, event_sink, mode, started)
        finally:
            self._active.pop(request_id, None)

    async def _handle_stream(
        self,
        session: Session,
        message: str,
        request_id: str,
        deadline: float,
        event_sink: Callable[[str, dict[str, Any]], Awaitable[None]],
        mode: Mode,
        started: float,
    ) -> dict:
        context = bind_request_id(request_id)

        async def sink(event: str, data: dict[str, Any]) -> None:
            # QueryForge 세션은 턴 커밋 전에 바인딩해야 dataset 조회가 404가 되지 않는다.
            if event == QUERYFORGE_SESSION_EVENT:
                application_session_id = data.get("application_session_id")
                if application_session_id:
                    self._sessions.bind_queryforge_session(session.session_id, application_session_id)
                return
            await event_sink(event, data)

        recall = await self._recall(session, message, mode)
        if recall:
            # 무엇을 근거로 지름길을 탔는지 사용자가 볼 수 있어야 한다.
            await event_sink("memory", recall.public())
        try:
            result = await self._agent.run(session, message, deadline, sink, render_hints(recall), **self._mode_kwargs(mode))
        except Exception:
            self._feedback(recall, succeeded=False)
            raise
        finally:
            reset_request_id(context)
        await self._remember(session, message, recall, result, mode)
        committed = self._sessions.commit(result.updated_session)
        self._record(mode, result, started, committed.session_id, message, locale=session.locale)
        duration_ms = int((time.monotonic() - started) * 1000)
        logging.getLogger("datalens.agent").info(
            "agent_turn_completed",
            extra={
                "request_id": request_id,
                "session_id": committed.session_id,
                "operation": "agent_turn",
                "elapsed_ms": duration_ms,
                "status": "completed",
                "tool_call_count": result.tool_calls,
            },
        )
        return {
            "status": "completed",
            "session_id": committed.session_id,
            "mode": mode,
            # JSON 응답과 달리 스트림에는 최종 본문이 없다. QF_EMPTY_RANGE 같은
            # 상류 경고가 사용자에게 닿는 유일한 통로가 done 이벤트다.
            "warnings": list(result.warnings),
            "metadata": {
                "duration_ms": duration_ms,
                "tool_calls": result.tool_calls,
                "recovery_count": result.recovery_count,
                "stop_reason": "completed",
                "memory": recall.public() if recall else None,
                "grounding": result.grounding,
                # 프롬프트가 컨텍스트 창에 들어갔는지. status != "ok"면 모델이 증거의
                # 일부를 못 봤다는 뜻이고, 답변의 "확인할 수 없다"는 그 결과일 수 있다.
                "context": result.context,
                "profile": result.profile,
            },
        }

    @staticmethod
    def _mode_kwargs(mode: Mode) -> dict[str, Any]:
        # agent 는 예전 호출 모양 그대로 부른다 — mode 인자를 모르는 Agent 구현도 그대로 돈다.
        return {} if mode == "agent" else {"mode": mode}

    def _record(
        self,
        mode: Mode,
        result: AgentResult,
        started: float,
        session_id: str = "",
        message: str = "",
        received: str | None = None,
        style: dict[str, Any] | None = None,
        locale: str | None = None,
    ) -> None:
        at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        duration_ms = int((time.monotonic() - started) * 1000)
        if self._runtime is not None:
            self._runtime.record_turn(
                mode,
                {
                    "at": at,
                    "duration_ms": duration_ms,
                    "tool_calls": result.tool_calls,
                    "answer_chars": len(result.answer),
                    "context": result.context,
                    "profile": result.profile,
                },
            )
        if self._turn_log is None:
            return
        raw = result.trace or {}
        usage = raw.get("usage")
        sent = None
        if raw:
            sent = blocks(raw["messages"], raw["tools"], raw["base_len"], getattr(usage, "prompt_tokens", None))
        profile = result.profile or {}
        context = profile.get("context") or {}
        params = profile.get("params") or {}
        context_info = result.context or {}
        row = {
            "at": at,
            "mode": mode,
            "session_id": session_id,
            # 효과 비교의 열 — 설정이 바뀐 지점을 화면이 이 묶음의 차이로 찾는다.
            "settings": {
                "system_prompt": context.get("system_prompt"),
                "tools": context.get("tools"),
                "history_items": context.get("history_items"),
                "temperature": params.get("temperature"),
                "repeat_penalty": params.get("repeat_penalty"),
                "num_predict": params.get("num_predict"),
                "num_ctx": params.get("num_ctx"),
                "think": params.get("think"),
                "keep_alive": params.get("keep_alive"),
            },
            "result": {
                "prompt_chars": sent["total_chars"] if sent else context_info.get("prompt_chars"),
                "prompt_tokens": context_info.get("prompt_tokens"),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "duration_ms": duration_ms,
                "tool_calls": result.tool_calls,
                "context_status": context_info.get("status"),
                "stop_reason": raw.get("stop_reason"),
                "answer_chars": len(result.answer),
                "history_used": profile.get("history_used"),
                # Ollama 단계별 시간 — load 가 크면 모델을 다시 올린 것이다(keep_alive 판단 근거).
                **(usage.timings() if usage is not None and hasattr(usage, "timings") else {"load_ms": None, "prompt_eval_ms": None, "eval_ms": None}),
            },
        }
        trace = None
        if sent is not None:
            trace = {
                "at": at,
                "mode": mode,
                "session_id": session_id,
                "question_chars": len(message),
                # 프롬프트 실험이 같은 언어로 다시 돌리려면 필요하다(지시문·단위가 언어별이다)
                "locale": locale,
                "sent": sent,
                "response": {
                    "answer": result.answer,
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "stop_reason": raw.get("stop_reason"),
                    "duration_ms": duration_ms,
                    "tool_calls": result.tool_calls,
                    # 강제 조회·실패 같은 경고 — 실험 비교에서 "무엇이 달라졌나"의 한 칸이다
                    "warnings": list(result.warnings),
                    "load_ms": row["result"]["load_ms"],
                    "prompt_eval_ms": row["result"]["prompt_eval_ms"],
                    "eval_ms": row["result"]["eval_ms"],
                    # 본문도 도구 호출도 없던 생성의 진단(원문·사고 앞 500자 · 청크 수) — 있을 때만
                    **({"empty_outputs": raw["empty_outputs"]} if raw.get("empty_outputs") else {}),
                    # 도구 0회 답의 근거 없는 수치로 한 번 다시 생성했다(첫 답 앞부분 · 걸린 수치)
                    **({"unbacked_retry": raw["unbacked_retry"]} if raw.get("unbacked_retry") else {}),
                },
                "settings": row["settings"],
            }
            if mode == "evidence":
                trace["received"] = message if received is None else received
                trace["style"] = style
        self._turn_log.add(mode, trace, row)

    def _memory_on(self, mode: Mode) -> bool:
        if self._memory is None:
            return False
        if self._runtime is None:
            return mode == "agent"
        return self._runtime.profile(mode).memory

    async def _recall(self, session: Session, message: str, mode: Mode = "agent") -> Recall:
        if not self._memory_on(mode):
            return Recall()
        return await self._memory.recall(message, session.locale)

    async def _remember(
        self, session: Session, message: str, recall: Recall, result: AgentResult, mode: Mode = "agent"
    ) -> None:
        # 증거 전용 턴은 조회 계획이 없다. 빈 계획을 "성공한 레시피"로 학습시키면 안 된다.
        if not self._memory_on(mode):
            return
        self._feedback(recall, succeeded=True)
        await self._memory.record_success(message, session.locale, plan_from_events(result.plan))

    def _feedback(self, recall: Recall, *, succeeded: bool) -> None:
        if self._memory is not None:
            self._memory.record_reuse(recall, succeeded=succeeded)
