from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Literal, Protocol

from datalens.domain.session import Session
from datalens.ports.session_store import SessionStore


class SessionNotFound(LookupError):
    pass


class SessionBusy(RuntimeError):
    pass


class AgentNotReady(RuntimeError):
    pass


class SessionCleanup(Protocol):
    async def cleanup(self, session: Session) -> None: ...


class SessionService:
    def __init__(self, store: SessionStore, cleanup: SessionCleanup | None = None) -> None:
        self._store = store
        self._cleanup = cleanup
        self._shutdown = False

    def create(
        self, now: datetime | None = None, *, locale: Literal["ko", "ja"] | None = None
    ) -> Session:
        return self._store.create(now, locale=locale)

    def require(self, session_id: str, now: datetime | None = None) -> Session:
        session = self._store.get(session_id, now)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def begin_turn(self, session_id: str, now: datetime | None = None) -> Session:
        self.require(session_id, now)
        session = self._store.try_begin_turn(session_id, now)
        if session is None:
            raise SessionBusy(session_id)
        return session

    def end_turn(self, session_id: str) -> None:
        self._store.end_turn(session_id)

    def touch(self, session_id: str, now: datetime | None = None) -> Session:
        session = self._store.touch(session_id, now)
        if session is None:
            raise SessionNotFound(session_id)
        return session

    def bind_queryforge_session(self, session_id: str, queryforge_session_id: str) -> Session:
        session = self.require(session_id).with_queryforge_session(queryforge_session_id)
        return self._store.update(session)

    def commit(self, session: Session) -> Session:
        return self._store.update(session)

    async def delete(self, session_id: str) -> None:
        session = self._store.get(session_id)
        if session is None:
            session = next(
                (item for item in self._store.expired() if item.session_id == session_id), None
            )
            if session is None:
                self._store.delete(session_id)
                return
        await self._cleanup_then_delete(session)

    async def reap_expired(self, now: datetime | None = None) -> int:
        expired = self._store.expired(now)
        for session in expired:
            await self._cleanup_then_delete(session)
        return len(expired)

    async def shutdown_cleanup(self) -> int:
        if self._shutdown:
            return 0
        self._shutdown = True
        sessions = self._store.all()
        await asyncio.gather(*(self._cleanup_then_delete(session) for session in sessions))
        return len(sessions)

    async def _cleanup_then_delete(self, session: Session) -> None:
        try:
            if self._cleanup is not None:
                await self._cleanup.cleanup(session)
        except Exception:
            logging.getLogger("datalens.session").warning(
                "session_cleanup_failed",
                extra={
                    "session_id": session.session_id,
                    "operation": "queryforge_session_release",
                    "status": "failed",
                    "error_code": "DL_SESSION_CLEANUP_FAILED",
                },
            )
        finally:
            self._store.delete(session.session_id)


class UnavailableMessageHandler:
    async def handle(self, session: Session, message: str, request_id: str, deadline: float) -> dict:
        raise AgentNotReady("Agent dependency is not available in Foundation stage")
