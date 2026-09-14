from __future__ import annotations

import time
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from conftest import ReadyProbe
from datalens.api.app import build_app
from datalens.application.agent import (
    AgentInvalidUpstreamResponse,
    AgentInternalError,
    AgentLimitError,
    AgentPolicyError,
    AgentQueryRejected,
    AgentResult,
    AgentTimeoutError,
    AgentUpstreamUnavailable,
    DatasetReference,
)
from datalens.application.chat import ChatApplicationService
from datalens.application.sessions import SessionService
from datalens.infrastructure.session_store import InMemorySessionStore


HEADERS = {"x-api-key": "data-secret"}


class FakeAgent:
    def __init__(self, outcome=None) -> None:
        self.outcome = outcome
        self.calls = []

    async def run(self, session, message, deadline):
        self.calls.append((session, message, deadline))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        updated = replace(
            session,
            queryforge_session_id="Q" * 22,
            active_dataset_id="ds_000000001",
            turn_state=({"role": "assistant", "content": "내부 요약"},),
        )
        return AgentResult(
            "한 건을 확인했습니다.",
            (DatasetReference("ds_000000001", row_count=1, preview=({"value": 7},)),),
            (),
            2,
            0,
            updated,
        )


def wired(settings, agent: FakeAgent):
    store = InMemorySessionStore(settings.session_ttl_seconds)
    sessions = SessionService(store)
    chat = ChatApplicationService(sessions, agent)
    return build_app(settings, session_service=sessions, readiness=ReadyProbe(), message_handler=chat), store


def create(client: TestClient) -> str:
    return client.post("/v1/sessions", headers=HEADERS, json={}).json()["session_id"]


def test_message_runs_application_agent_and_returns_public_result(settings) -> None:
    agent = FakeAgent()
    app, store = wired(settings, agent)
    with TestClient(app) as client:
        session_id = create(client)
        before = time.monotonic()
        response = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "조회해줘"}
        )
        assert store.get(session_id).queryforge_session_id == "Q" * 22
    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == response.headers["x-request-id"]
    assert payload["session_id"] == session_id
    assert payload["status"] == "completed"
    assert payload["answer"] == "한 건을 확인했습니다."
    assert payload["datasets"][0]["dataset_id"] == "ds_000000001"
    assert payload["metadata"]["tool_calls"] == 2
    assert agent.calls[0][2] > before
    assert agent.calls[0][2] <= before + settings.request_deadline_seconds + 0.1
    assert store.get(session_id) is None
    serialized = response.text
    assert "Q" * 22 not in serialized
    assert "mcp-session" not in serialized


def test_busy_session_maps_to_409(settings) -> None:
    agent = FakeAgent()
    app, store = wired(settings, agent)
    with TestClient(app) as client:
        session_id = create(client)
        assert store.try_begin_turn(session_id) is not None
        response = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "조회"}
        )
        store.end_turn(session_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DL_SESSION_BUSY"
    assert agent.calls == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (AgentLimitError("limit"), 422, "DL_AGENT_LIMIT"),
        (AgentTimeoutError("late"), 504, "DL_UPSTREAM_TIMEOUT"),
        (AgentUpstreamUnavailable("down"), 503, "DL_UPSTREAM_UNAVAILABLE"),
        (AgentQueryRejected("UNKNOWN_COLUMN"), 422, "DL_QUERY_REJECTED"),
        (AgentInternalError("private detail"), 500, "DL_INTERNAL_ERROR"),
        (AgentPolicyError("shell"), 422, "DL_AGENT_INVALID_TOOL"),
        (AgentInvalidUpstreamResponse("provider raw error"), 502, "DL_UPSTREAM_INVALID_RESPONSE"),
    ],
)
def test_agent_errors_use_public_allowlist(settings, error, status, code) -> None:
    app, _ = wired(settings, FakeAgent(error))
    with TestClient(app) as client:
        session_id = create(client)
        response = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "조회"}
        )
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "private detail" not in response.text
    assert "UNKNOWN_COLUMN" not in response.text


def test_turn_lock_is_released_after_error(settings) -> None:
    app, _ = wired(settings, FakeAgent(AgentLimitError("limit")))
    with TestClient(app) as client:
        session_id = create(client)
        first = client.post(f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "x"})
        second = client.post(f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "x"})
    assert first.status_code == second.status_code == 422


@pytest.mark.parametrize(
    "private_detail",
    [
        "QueryForge Application Session QQQ",
        "MCP Transport Session MMM",
        "API key top-secret",
        "Authorization Bearer hidden",
        "SQL internal detail SELECT * FROM private",
        "stack trace at handler.py:10",
        "provider raw error payload",
    ],
)
def test_public_error_response_never_leaks_internal_details(settings, private_detail) -> None:
    app, _ = wired(settings, FakeAgent(RuntimeError(private_detail)))
    with TestClient(app) as client:
        session_id = create(client)
        response = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "조회"}
        )
    assert response.status_code == 500
    assert private_detail not in response.text


class Closeable:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_lifecycle_closes_provider_resource(settings) -> None:
    resource = Closeable()
    with TestClient(build_app(settings, readiness=ReadyProbe(), closeables=(resource,))):
        assert resource.closed is False
    assert resource.closed is True
