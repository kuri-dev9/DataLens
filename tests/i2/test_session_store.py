from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from datalens.infrastructure.session_store import InMemorySessionStore


def test_create_unique_opaque_sessions() -> None:
    store = InMemorySessionStore(60)
    sessions = {store.create().session_id for _ in range(100)}
    assert len(sessions) == 100
    assert all(value.startswith("dls_") and len(value) >= 24 for value in sessions)


def test_expiration_touch_and_delete() -> None:
    store = InMemorySessionStore(10)
    start = datetime(2026, 9, 8, tzinfo=UTC)
    session = store.create(start)
    assert store.get(session.session_id, start + timedelta(seconds=9)) is not None
    touched = store.touch(session.session_id, start + timedelta(seconds=9))
    assert touched is not None
    assert store.get(session.session_id, start + timedelta(seconds=15)) is not None
    assert store.get(session.session_id, start + timedelta(seconds=20)) is None
    assert store.delete(session.session_id) is not None


def test_expire_returns_expired_sessions() -> None:
    store = InMemorySessionStore(1)
    start = datetime(2026, 9, 8, tzinfo=UTC)
    session = store.create(start)
    assert store.expire(start + timedelta(seconds=2)) == [session]


def test_expired_detects_without_removing_for_application_cleanup() -> None:
    store = InMemorySessionStore(1)
    start = datetime(2026, 9, 8, tzinfo=UTC)
    session = store.create(start)
    assert store.expired(start + timedelta(seconds=2)) == [session]
    assert store.delete(session.session_id) == session


def test_concurrent_turn_is_rejected_atomically() -> None:
    store = InMemorySessionStore(60)
    session = store.create()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.try_begin_turn(session.session_id), range(8)))
    assert sum(result is not None for result in results) == 1
    store.end_turn(session.session_id)
    assert store.try_begin_turn(session.session_id) is not None


def test_queryforge_session_mapping_is_internal_and_stable() -> None:
    store = InMemorySessionStore(60)
    session = store.create().with_queryforge_session("Q" * 22)
    store.update(session)
    assert store.get(session.session_id).queryforge_session_id == "Q" * 22
    assert "queryforge" not in session.public()
