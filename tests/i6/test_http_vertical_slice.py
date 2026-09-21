from __future__ import annotations

import time
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import anyio
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
    BoundedAgent,
)
from datalens.application.chat import ChatApplicationService
from datalens.application.sessions import SessionService
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.infrastructure.queryforge_mcp import DEFAULT_ALLOWED_TOOLS, McpQueryForgeClient
from datalens.ports.llm import AssistantTurn, ToolCall
from datalens.ports.queryforge import QueryForgeFailure


HEADERS = {"x-api-key": "data-secret"}


class FakeAgent:
    def __init__(self, outcome=None) -> None:
        self.outcome = outcome
        self.calls = []

    async def run(self, session, message, deadline, event_sink=None):
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


class ToolCallingProvider:
    def __init__(self) -> None:
        self._turn = 0

    async def complete(self, messages, tools, deadline, output_policy):
        self._turn += 1
        if self._turn == 1:
            return AssistantTurn(
                "", (ToolCall("call-1", "schema", {"action": "list_tables"}),), "tool_calls"
            )
        return AssistantTurn("정상 완료", (), "stop")

    async def complete_stream(self, messages, tools, deadline, output_policy, on_token):
        turn = await self.complete(messages, tools, deadline, output_policy)
        if not turn.tool_calls:
            await on_token("정상 ")
            await on_token("완료")
            return AssistantTurn("정상 완료", (), "stop")
        return turn

    async def close(self):
        return None


class MockMcpSession:
    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=name,
                    description=f"{name} description",
                    inputSchema={"type": "object", "additionalProperties": True},
                )
                for name in DEFAULT_ALLOWED_TOOLS
            ]
        )

    async def call_tool(self, name, arguments):
        return SimpleNamespace(
            structuredContent={
                "ok": True,
                "session_id": "Q" * 22,
                "action": "list_tables",
                "tables": [],
                "dataset_id": "ds_000000001",
                "row_count": 1,
                "columns": [{"name": "value", "type": "integer"}],
                "warnings": [],
                "error": None,
            },
            isError=False,
        )


def test_asgi_ac1_ac2_tool_call_returns_completed_response_without_cancel_scope_error(
    settings, caplog
) -> None:
    queryforge = McpQueryForgeClient("http://queryforge.test/mcp", "query-secret")
    scope_tasks = []

    @asynccontextmanager
    async def request_scoped_session(timeout):
        entered = asyncio.current_task()
        async with anyio.create_task_group():
            yield MockMcpSession()
        scope_tasks.append((entered, asyncio.current_task()))

    queryforge._session_scope = request_scoped_session
    store = InMemorySessionStore(settings.session_ttl_seconds)
    sessions = SessionService(store)
    agent = BoundedAgent(
        ToolCallingProvider(), queryforge, max_tool_calls=3, recovery_budget=1
    )
    app = build_app(
        settings,
        session_service=sessions,
        readiness=ReadyProbe(),
        message_handler=ChatApplicationService(sessions, agent),
        closeables=(queryforge,),
    )

    with caplog.at_level("INFO", logger="datalens.agent"), TestClient(app) as client:
        session_id = create(client)
        response = client.post(
            f"/v1/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"message": "테이블 목록"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["metadata"]["tool_calls"] == 1
    assert any(
        record.message == "agent_turn_completed" and record.status == response.json()["status"]
        for record in caplog.records
    )
    assert len(scope_tasks) == 2
    assert all(entered is exited for entered, exited in scope_tasks)


def test_strm_ac1_ac2_ac5_ac6_message_accept_negotiation_and_events(settings) -> None:
    queryforge = McpQueryForgeClient("http://queryforge.test/mcp", "query-secret")

    @asynccontextmanager
    async def request_scoped_session(timeout):
        yield MockMcpSession()

    queryforge._session_scope = request_scoped_session
    store = InMemorySessionStore(settings.session_ttl_seconds)
    sessions = SessionService(store)
    agent = BoundedAgent(ToolCallingProvider(), queryforge, max_tool_calls=3, recovery_budget=1)
    app = build_app(
        settings,
        session_service=sessions,
        readiness=ReadyProbe(),
        message_handler=ChatApplicationService(sessions, agent),
        closeables=(queryforge,),
    )
    with TestClient(app) as client:
        stream_session = create(client)
        streamed = client.post(
            f"/v1/sessions/{stream_session}/messages",
            headers={**HEADERS, "accept": "text/event-stream"},
            json={"message": "테이블 목록"},
        )
        json_session = create(client)
        synchronous = client.post(
            f"/v1/sessions/{json_session}/messages",
            headers=HEADERS,
            json={"message": "테이블 목록"},
        )
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert "event: start" in streamed.text
    assert streamed.text.count("event: tool_call") == 2
    assert "event: dataset" in streamed.text
    assert "event: token" in streamed.text
    assert "event: done" in streamed.text
    assert synchronous.headers["content-type"].startswith("application/json")
    assert synchronous.json()["status"] == "completed"


def test_strm_ac7_error_event_ends_stream_and_next_request_recovers(settings) -> None:
    agent = FakeAgent(AgentUpstreamUnavailable("down"))
    app, _ = wired(settings, agent)
    with TestClient(app) as client:
        session_id = create(client)
        failed = client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={**HEADERS, "accept": "text/event-stream"},
            json={"message": "조회"},
        )
        agent.outcome = None
        recovered = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "재시도"}
        )
    assert "event: error" in failed.text
    assert "DL_UPSTREAM_UNAVAILABLE" in failed.text
    assert recovered.status_code == 200


@pytest.mark.anyio
async def test_strm_ac8_ac10_disconnect_cancels_upstream_without_asgi_error(settings) -> None:
    cancelled = asyncio.Event()

    class DisconnectableHandler:
        async def handle_stream(self, session, message, request_id, deadline, event_sink):
            await event_sink("start", {"request_id": request_id, "session_id": session.session_id})
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def handle(self, session, message, request_id, deadline):
            return {
                "request_id": request_id,
                "session_id": session.session_id,
                "status": "completed",
                "answer": "복구됨",
                "datasets": [],
                "warnings": [],
                "metadata": {"duration_ms": 0, "tool_calls": 0},
                "error": None,
            }

    store = InMemorySessionStore(settings.session_ttl_seconds)
    sessions = SessionService(store)
    session = store.create()
    app = build_app(
        settings,
        session_service=sessions,
        readiness=ReadyProbe(),
        message_handler=DisconnectableHandler(),
    )
    body = b'{"message":"stream"}'
    receives = [
        {"type": "http.request", "body": body, "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return receives.pop(0) if receives else {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": f"/v1/sessions/{session.session_id}/messages",
            "raw_path": f"/v1/sessions/{session.session_id}/messages".encode(), "query_string": b"",
            "headers": [(b"x-api-key", b"data-secret"), (b"accept", b"text/event-stream"), (b"content-type", b"application/json")],
            "client": ("test", 1), "server": ("test", 80), "root_path": "",
        },
        receive,
        send,
    )
    await asyncio.wait_for(cancelled.wait(), 1)
    with TestClient(app) as client:
        recovered = client.post(
            f"/v1/sessions/{session.session_id}/messages", headers=HEADERS, json={"message": "again"}
        )
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "completed"


def test_asgi_ac4_deadline_error_returns_timeout_response(settings) -> None:
    app, _ = wired(settings, FakeAgent(AgentTimeoutError("late")))
    with TestClient(app) as client:
        session_id = create(client)
        response = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "조회"}
        )
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "DL_UPSTREAM_TIMEOUT"


def test_asgi_ac3_request_after_upstream_error_reconnects(settings) -> None:
    queryforge = McpQueryForgeClient("http://queryforge.test/mcp", "query-secret")
    attempts = 0

    class FailingDiscoverySession(MockMcpSession):
        async def list_tools(self):
            raise RuntimeError("connection dropped")

    @asynccontextmanager
    async def reconnecting_session(timeout):
        nonlocal attempts
        attempts += 1
        yield FailingDiscoverySession() if attempts == 1 else MockMcpSession()

    queryforge._session_scope = reconnecting_session
    store = InMemorySessionStore(settings.session_ttl_seconds)
    sessions = SessionService(store)
    agent = BoundedAgent(
        ToolCallingProvider(), queryforge, max_tool_calls=3, recovery_budget=1
    )
    app = build_app(
        settings,
        session_service=sessions,
        readiness=ReadyProbe(),
        message_handler=ChatApplicationService(sessions, agent),
        closeables=(queryforge,),
    )

    with TestClient(app) as client:
        session_id = create(client)
        first = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "첫 요청"}
        )
        second = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "재시도"}
        )

    assert first.status_code == 503
    assert first.json()["error"]["code"] == "DL_UPSTREAM_UNAVAILABLE"
    assert second.status_code == 200
    assert second.json()["status"] == "completed"
    assert attempts == 3


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


def test_err_ac_n1_n2_n4_query_rejection_preserves_safe_hint_and_retryable(settings) -> None:
    failure = QueryForgeFailure(
        "MISSING_PARTITION_SCOPE",
        "internal upstream message",
        True,
        hint='Use {"kind":"not_partitioned"}',
        safe_metadata={"table": "PM_CEI_PGW_5M", "expected_kind": "not_partitioned"},
    )
    app, _ = wired(settings, FakeAgent(AgentQueryRejected(failure, tool_calls=2, recovery_count=1)))
    with TestClient(app) as client:
        session_id = create(client)
        response = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "조회"}
        )
    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "DL_QUERY_REJECTED",
        "message": "The data request was rejected",
        "retryable": True,
        "details": {
            "upstream_code": "MISSING_PARTITION_SCOPE",
            "hint": 'Use {"kind":"not_partitioned"}',
            "table": "PM_CEI_PGW_5M",
            "expected_kind": "not_partitioned",
        },
    }
    assert response.json()["metadata"]["stop_reason"] == "failed"
    assert response.json()["metadata"]["duration_ms"] >= 0
    assert response.json()["metadata"]["tool_calls"] == 2
    assert response.json()["metadata"]["recovery_count"] == 1
    assert "internal upstream message" not in response.text
    assert "SELECT " not in response.text


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


def test_err_partial_datasets_and_failed_step_survive_a_rejection(settings) -> None:
    failure = QueryForgeFailure("PARTITION_SCOPE_TOO_WIDE", "out of range", True, hint="narrow the range")
    rejected = AgentQueryRejected(failure, tool_calls=3, recovery_count=1)
    rejected.datasets = (DatasetReference("ds_000000001", row_count=2),)
    rejected.warnings = ({"code": "RESULT_TRUNCATED"},)
    rejected.failed_step = {"index": 3, "tool": "query", "upstream_code": "PARTITION_SCOPE_TOO_WIDE"}
    app, _ = wired(settings, FakeAgent(rejected))
    with TestClient(app) as client:
        session_id = create(client)
        response = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "어제 트래픽"}
        )
    body = response.json()
    assert response.status_code == 422
    assert [dataset["dataset_id"] for dataset in body["datasets"]] == ["ds_000000001"]
    assert body["warnings"] == [{"code": "RESULT_TRUNCATED"}]
    assert body["metadata"]["failed_step"] == {"index": 3, "tool": "query", "upstream_code": "PARTITION_SCOPE_TOO_WIDE"}
    assert body["error"]["details"]["upstream_code"] == "PARTITION_SCOPE_TOO_WIDE"
    assert body["error"]["details"]["failed_step"]["tool"] == "query"


def test_dataset_rows_are_reachable_while_the_turn_is_still_running(settings) -> None:
    # dataset 이벤트가 나간 시점에 이미 QueryForge 세션이 바인딩되어 있어야 한다.
    from datalens.application.agent import QUERYFORGE_SESSION_EVENT
    from datalens.application.chat import ChatApplicationService
    from datalens.application.sessions import SessionService
    from datalens.infrastructure.session_store import InMemorySessionStore

    store = InMemorySessionStore(60)
    sessions = SessionService(store)
    created = store.create()
    bound: list[str | None] = []

    class Agent:
        async def run(self, session, message, deadline, event_sink=None):
            await event_sink(QUERYFORGE_SESSION_EVENT, {"application_session_id": "Q" * 22})
            bound.append(sessions.require(created.session_id).queryforge_session_id)
            await event_sink("dataset", {"dataset_id": "ds_000000001"})
            return AgentResult("완료", (), (), 1, 0, sessions.require(created.session_id))

    emitted: list[str] = []

    async def sink(event, data):
        emitted.append(event)

    asyncio.run(
        ChatApplicationService(sessions, Agent()).handle_stream(
            created, "조회", "dlr_x", time.monotonic() + 5, sink
        )
    )
    assert bound == ["Q" * 22]
    assert QUERYFORGE_SESSION_EVENT not in emitted
    assert "dataset" in emitted
