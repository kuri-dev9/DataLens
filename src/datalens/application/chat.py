from __future__ import annotations

import time
import logging
from typing import Protocol

from datalens.application.agent import AgentResult
from datalens.application.sessions import SessionService
from datalens.domain.session import Session


class Agent(Protocol):
    async def run(self, session: Session, message: str, deadline: float) -> AgentResult: ...


class ChatApplicationService:
    def __init__(self, sessions: SessionService, agent: Agent) -> None:
        self._sessions = sessions
        self._agent = agent

    async def handle(self, session: Session, message: str, request_id: str, deadline: float) -> dict:
        started = time.monotonic()
        result = await self._agent.run(session, message, deadline)
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
            },
            "error": None,
        }
