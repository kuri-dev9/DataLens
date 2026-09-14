from __future__ import annotations

import os

import pytest

from datalens.infrastructure.queryforge_mcp import McpQueryForgeClient


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.integration
@pytest.mark.anyio
async def test_running_queryforge_initialize_discover_and_schema() -> None:
    endpoint = os.getenv("DATALENS_QUERYFORGE_ENDPOINT")
    api_key = os.getenv("DATALENS_QUERYFORGE_API_KEY")
    if not endpoint or not api_key:
        pytest.skip("BLOCKED_BY_ENVIRONMENT: QueryForge endpoint/key not configured")
    client = McpQueryForgeClient(endpoint, api_key, default_timeout_seconds=5)
    try:
        tools = await client.discover_tools(5)
        assert {tool.name for tool in tools} == {"schema", "relationship", "query", "transform", "describe"}
        result = await client.call_tool(
            "schema", {"action": "list_tables"}, application_session_id=None, timeout_seconds=10
        )
        assert result.ok is True
        assert result.application_session_id
    finally:
        await client.close()

