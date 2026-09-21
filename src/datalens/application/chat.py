from __future__ import annotations

import time
import logging
from typing import Any, Awaitable, Callable, Protocol

from datalens.application.agent import QUERYFORGE_SESSION_EVENT, AgentResult
from datalens.application.memory import MemoryService, plan_from_events, render_hints
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
    ) -> AgentResult: ...


class ChatApplicationService:
    def __init__(
        self, sessions: SessionService, agent: Agent, memory: MemoryService | None = None
    ) -> None:
        self._sessions = sessions
        self._agent = agent
        self._memory = memory

    async def handle(self, session: Session, message: str, request_id: str, deadline: float) -> dict:
        started = time.monotonic()
        context = bind_request_id(request_id)
        recall = await self._recall(session, message)
        try:
            result = await self._agent.run(session, message, deadline, hints=render_hints(recall))
        except Exception:
            self._feedback(recall, succeeded=False)
            raise
        finally:
            reset_request_id(context)
        await self._remember(session, message, recall, result)
        committed = self._sessions.commit(result.updated_session)
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
    ) -> dict:
        started = time.monotonic()
        context = bind_request_id(request_id)

        async def sink(event: str, data: dict[str, Any]) -> None:
            # QueryForge 세션은 턴 커밋 전에 바인딩해야 dataset 조회가 404가 되지 않는다.
            if event == QUERYFORGE_SESSION_EVENT:
                application_session_id = data.get("application_session_id")
                if application_session_id:
                    self._sessions.bind_queryforge_session(session.session_id, application_session_id)
                return
            await event_sink(event, data)

        recall = await self._recall(session, message)
        if recall:
            # 무엇을 근거로 지름길을 탔는지 사용자가 볼 수 있어야 한다.
            await event_sink("memory", recall.public())
        try:
            result = await self._agent.run(session, message, deadline, sink, render_hints(recall))
        except Exception:
            self._feedback(recall, succeeded=False)
            raise
        finally:
            reset_request_id(context)
        await self._remember(session, message, recall, result)
        committed = self._sessions.commit(result.updated_session)
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
            "metadata": {
                "duration_ms": duration_ms,
                "tool_calls": result.tool_calls,
                "recovery_count": result.recovery_count,
                "stop_reason": "completed",
                "memory": recall.public() if recall else None,
            },
        }

    async def _recall(self, session: Session, message: str) -> Recall:
        if self._memory is None:
            return Recall()
        return await self._memory.recall(message, session.locale)

    async def _remember(
        self, session: Session, message: str, recall: Recall, result: AgentResult
    ) -> None:
        if self._memory is None:
            return
        self._feedback(recall, succeeded=True)
        await self._memory.record_success(message, session.locale, plan_from_events(result.plan))

    def _feedback(self, recall: Recall, *, succeeded: bool) -> None:
        if self._memory is not None:
            self._memory.record_reuse(recall, succeeded=succeeded)
