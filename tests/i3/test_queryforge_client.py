from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import httpx

from datalens.infrastructure.queryforge_mcp import DEFAULT_ALLOWED_TOOLS, McpQueryForgeClient
from datalens.ports.queryforge import QueryForgeUnavailable


def tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=f"{name} description",
        inputSchema={"type": "object", "properties": {"action": {"type": "string"}}},
    )


class FakeSession:
    def __init__(self, tools=None, result=None, delay: float = 0) -> None:
        self.tools = tools or [tool(name) for name in sorted(DEFAULT_ALLOWED_TOOLS)]
        self.result = result or {
            "ok": True,
            "session_id": "A" * 22,
            "action": "list_tables",
            "tables": [],
            "error": None,
        }
        self.delay = delay
        self.calls: list[tuple[str, dict]] = []
        self.list_calls = 0

    async def list_tools(self):
        self.list_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(tools=self.tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(
            structuredContent=self.result,
            isError=not self.result.get("ok", False),
        )


def client_with(session: FakeSession) -> McpQueryForgeClient:
    client = McpQueryForgeClient("http://queryforge.test/mcp", "top-secret")
    client._session = session
    return client


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_discovery_normalizes_and_filters_extra_tool() -> None:
    session = FakeSession(tools=[tool(name) for name in DEFAULT_ALLOWED_TOOLS] + [tool("mutate")])
    client = client_with(session)
    discovered = await client.discover_tools()
    assert {item.name for item in discovered} == DEFAULT_ALLOWED_TOOLS
    assert all(item.input_schema["type"] == "object" for item in discovered)


@pytest.mark.anyio
async def test_discovery_is_cached_and_explicitly_refreshable() -> None:
    session = FakeSession()
    client = client_with(session)
    await client.discover_tools()
    await client.discover_tools()
    assert session.list_calls == 1
    await client.discover_tools(refresh=True)
    assert session.list_calls == 2


@pytest.mark.anyio
async def test_missing_required_capability_is_not_ready() -> None:
    session = FakeSession(tools=[tool("schema")])
    client = client_with(session)
    with pytest.raises(QueryForgeUnavailable):
        await client.discover_tools()


@pytest.mark.anyio
async def test_call_forwards_application_session_without_mutating_input() -> None:
    session = FakeSession()
    client = client_with(session)
    arguments = {"action": "list_tables"}
    result = await client.call_tool(
        "schema", arguments, application_session_id="B" * 22, timeout_seconds=1
    )
    assert result.ok is True
    assert result.application_session_id == "A" * 22
    assert session.calls == [("schema", {"action": "list_tables", "session_id": "B" * 22})]
    assert arguments == {"action": "list_tables"}


@pytest.mark.anyio
async def test_first_call_can_receive_new_application_session() -> None:
    session = FakeSession()
    client = client_with(session)
    result = await client.call_tool(
        "schema", {"action": "list_tables"}, application_session_id=None, timeout_seconds=1
    )
    assert result.application_session_id == "A" * 22
    assert "session_id" not in session.calls[0][1]


@pytest.mark.anyio
async def test_unknown_tool_is_rejected_before_transport() -> None:
    session = FakeSession()
    client = client_with(session)
    with pytest.raises(ValueError, match="not allowed"):
        await client.call_tool("shell", {}, application_session_id=None, timeout_seconds=1)
    assert session.calls == []


@pytest.mark.anyio
async def test_caller_deadline_is_enforced() -> None:
    client = client_with(FakeSession(delay=0.05))
    with pytest.raises(TimeoutError, match="timed out"):
        await client.call_tool(
            "schema", {"action": "list_tables"}, application_session_id=None, timeout_seconds=0.001
        )


@pytest.mark.anyio
async def test_structured_error_is_preserved_and_unsafe_details_removed() -> None:
    session = FakeSession(
        result={
            "ok": False,
            "session_id": "A" * 22,
            "error": {
                "code": "UNKNOWN_COLUMN",
                "message": "unknown column",
                "retryable": True,
                "details": {
                    "did_you_mean": ["event_time"],
                    "sql": "SELECT secret FROM private_table",
                    "password": "do-not-leak",
                },
                "hint": "choose one suggested column",
            },
        }
    )
    result = await client_with(session).call_tool(
        "query", {}, application_session_id=None, timeout_seconds=1
    )
    assert result.error is not None
    assert result.error.code == "UNKNOWN_COLUMN"
    assert result.error.retryable is True
    assert result.error.candidates == ("event_time",)
    assert result.error.hint == "choose one suggested column"
    assert set(result.error.safe_metadata) == {"did_you_mean"}


@pytest.mark.anyio
async def test_transport_reconnect_does_not_change_caller_owned_application_session() -> None:
    first = FakeSession()
    client = client_with(first)
    await client.call_tool("schema", {}, application_session_id="C" * 22, timeout_seconds=1)
    client._session = FakeSession()
    await client.call_tool("schema", {}, application_session_id="C" * 22, timeout_seconds=1)
    assert first.calls[0][1]["session_id"] == "C" * 22
    assert client._session.calls[0][1]["session_id"] == "C" * 22


def test_repr_redacts_authentication_secret() -> None:
    client = McpQueryForgeClient("http://queryforge.test/mcp", "top-secret")
    assert "top-secret" not in repr(client)
    assert "redacted" in repr(client)


@pytest.mark.anyio
@pytest.mark.parametrize("status", [200, 404])
async def test_release_uses_current_data_api_contract_and_is_idempotent(status: int) -> None:
    captured = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, json={"released": status == 200})

    data_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = McpQueryForgeClient(
        "http://queryforge.test/mcp",
        "top-secret",
        data_base_url="http://queryforge.test",
        data_client=data_client,
    )
    try:
        await client.release_application_session("qf/session", 1)
    finally:
        await data_client.aclose()
    assert captured[0].url.raw_path == b"/data/sessions/qf%2Fsession/release"
    assert captured[0].headers["x-api-key"] == "top-secret"
