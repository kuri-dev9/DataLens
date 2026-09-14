from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from datalens.application.sessions import SessionService
from datalens.infrastructure.session_store import InMemorySessionStore


class Cleanup:
    def __init__(self, fail: bool = False) -> None:
        self.ids: list[str] = []
        self.fail = fail

    async def cleanup(self, session) -> None:
        self.ids.append(session.queryforge_session_id)
        if self.fail:
            raise RuntimeError("provider raw error Authorization secret")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_delete_releases_before_local_cleanup() -> None:
    store = InMemorySessionStore(60)
    cleanup = Cleanup()
    service = SessionService(store, cleanup)
    session = store.create().with_queryforge_session("Q" * 22)
    store.update(session)
    await service.delete(session.session_id)
    assert cleanup.ids == ["Q" * 22]
    assert store.get(session.session_id) is None


@pytest.mark.anyio
async def test_ttl_reaper_is_best_effort_and_removes_local_session(caplog) -> None:
    start = datetime(2026, 9, 8, tzinfo=UTC)
    store = InMemorySessionStore(1)
    cleanup = Cleanup(fail=True)
    service = SessionService(store, cleanup)
    session = store.create(start).with_queryforge_session("Q" * 22)
    store.update(session)
    assert await service.reap_expired(start + timedelta(seconds=2)) == 1
    assert store.delete(session.session_id) is None
    assert "session_cleanup_failed" in caplog.text
    assert "Authorization secret" not in caplog.text


@pytest.mark.anyio
async def test_shutdown_releases_every_live_session_once_despite_failure() -> None:
    store = InMemorySessionStore(60)
    cleanup = Cleanup(fail=True)
    service = SessionService(store, cleanup)
    sessions = []
    for value in ("A" * 22, "B" * 22):
        session = store.create().with_queryforge_session(value)
        store.update(session)
        sessions.append(session)

    assert await service.shutdown_cleanup() == 2
    assert await service.shutdown_cleanup() == 0
    assert cleanup.ids == ["A" * 22, "B" * 22]
    assert store.all() == []
