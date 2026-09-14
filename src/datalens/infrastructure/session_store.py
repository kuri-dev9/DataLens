from __future__ import annotations

import secrets
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from datalens.domain.session import Session


class InMemorySessionStore:
    def __init__(self, ttl_seconds: float, default_locale: Literal["ko", "ja"] = "ko") -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = timedelta(seconds=ttl_seconds)
        self._default_locale = default_locale
        self._sessions: dict[str, Session] = {}
        self._active_turns: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        return now or datetime.now(UTC)

    def create(
        self, now: datetime | None = None, *, locale: Literal["ko", "ja"] | None = None
    ) -> Session:
        current = self._now(now)
        with self._lock:
            while True:
                session_id = f"dls_{secrets.token_urlsafe(18)}"
                if session_id not in self._sessions:
                    break
            session = Session(
                session_id, current, current, current + self._ttl, locale=locale or self._default_locale
            )
            self._sessions[session_id] = session
            return session

    def get(self, session_id: str, now: datetime | None = None) -> Session | None:
        current = self._now(now)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at <= current:
                return None
            return session

    def touch(self, session_id: str, now: datetime | None = None) -> Session | None:
        current = self._now(now)
        with self._lock:
            session = self.get(session_id, current)
            if session is None:
                return None
            touched = replace(session, last_accessed_at=current, expires_at=current + self._ttl)
            self._sessions[session_id] = touched
            return touched

    def update(self, session: Session) -> Session:
        with self._lock:
            if session.session_id not in self._sessions:
                raise KeyError(session.session_id)
            self._sessions[session.session_id] = session
            return session

    def delete(self, session_id: str) -> Session | None:
        with self._lock:
            self._active_turns.discard(session_id)
            return self._sessions.pop(session_id, None)

    def expire(self, now: datetime | None = None) -> list[Session]:
        current = self._now(now)
        with self._lock:
            expired = [session for session in self._sessions.values() if session.expires_at <= current]
            for session in expired:
                self._sessions.pop(session.session_id, None)
                self._active_turns.discard(session.session_id)
            return expired

    def expired(self, now: datetime | None = None) -> list[Session]:
        current = self._now(now)
        with self._lock:
            return [
                session
                for session in self._sessions.values()
                if session.expires_at <= current and session.session_id not in self._active_turns
            ]

    def all(self) -> list[Session]:
        with self._lock:
            return list(self._sessions.values())

    def try_begin_turn(self, session_id: str, now: datetime | None = None) -> Session | None:
        with self._lock:
            session = self.get(session_id, now)
            if session is None or session_id in self._active_turns:
                return None
            self._active_turns.add(session_id)
            return session

    def end_turn(self, session_id: str) -> None:
        with self._lock:
            self._active_turns.discard(session_id)
