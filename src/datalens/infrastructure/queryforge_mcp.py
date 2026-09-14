from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack
from copy import deepcopy
from typing import Any
from urllib.parse import quote

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from datalens.ports.queryforge import (
    QueryForgeFailure,
    QueryForgeResult,
    QueryForgeToolDefinition,
    QueryForgeUnavailable,
)


DEFAULT_ALLOWED_TOOLS = frozenset({"schema", "relationship", "query", "transform", "describe"})


class McpQueryForgeClient:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        data_base_url: str | None = None,
        allowed_tools: frozenset[str] = DEFAULT_ALLOWED_TOOLS,
        default_timeout_seconds: float = 5.0,
        data_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._data_base_url = data_base_url.rstrip("/") if data_base_url else None
        self._allowed_tools = allowed_tools
        self._default_timeout = default_timeout_seconds
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._connect_lock = asyncio.Lock()
        self._discovery_lock = asyncio.Lock()
        self._tools: tuple[QueryForgeToolDefinition, ...] = ()
        self._data_client = data_client or httpx.AsyncClient(headers={"x-api-key": api_key})
        self._owns_data_client = data_client is None
        self._closed = False

    def __repr__(self) -> str:
        return f"McpQueryForgeClient(endpoint={self._endpoint!r}, api_key=<redacted>)"

    async def connect(self, timeout_seconds: float | None = None) -> None:
        timeout = timeout_seconds or self._default_timeout
        async with self._connect_lock:
            if self._closed:
                raise QueryForgeUnavailable("QueryForge client is closed")
            if self._session is not None:
                return
            stack = AsyncExitStack()
            try:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(headers={"x-api-key": self._api_key})
                )
                read_stream, write_stream, _ = await stack.enter_async_context(
                    streamable_http_client(self._endpoint, http_client=client)
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await asyncio.wait_for(session.initialize(), timeout)
            except Exception as exc:
                await stack.aclose()
                raise QueryForgeUnavailable("QueryForge MCP initialization failed") from exc
            self._stack = stack
            self._session = session

    async def close(self) -> None:
        async with self._connect_lock:
            if self._closed:
                return
            self._closed = True
            stack, self._stack, self._session, self._tools = self._stack, None, None, ()
            try:
                if stack is not None:
                    await stack.aclose()
            finally:
                if self._owns_data_client:
                    await self._data_client.aclose()

    async def discover_tools(
        self, timeout_seconds: float | None = None, *, refresh: bool = False
    ) -> tuple[QueryForgeToolDefinition, ...]:
        timeout = timeout_seconds or self._default_timeout
        await self.connect(timeout)
        async with self._discovery_lock:
            if self._tools and not refresh:
                return deepcopy(self._tools)
            assert self._session is not None
            try:
                listed = await asyncio.wait_for(self._session.list_tools(), timeout)
            except Exception as exc:
                self._tools = ()
                raise QueryForgeUnavailable("QueryForge tool discovery failed") from exc
            discovered = tuple(
                QueryForgeToolDefinition(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=deepcopy(tool.inputSchema),
                )
                for tool in listed.tools
                if tool.name in self._allowed_tools
            )
            names = {tool.name for tool in discovered}
            missing = self._allowed_tools - names
            if missing:
                raise QueryForgeUnavailable(f"QueryForge required capabilities are unavailable: {sorted(missing)!r}")
            self._tools = discovered
            return deepcopy(discovered)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        application_session_id: str | None,
        timeout_seconds: float,
    ) -> QueryForgeResult:
        if name not in self._allowed_tools:
            raise ValueError("QueryForge tool is not allowed")
        if timeout_seconds <= 0:
            raise TimeoutError("QueryForge deadline exhausted")
        deadline = time.monotonic() + timeout_seconds
        await self.connect(min(timeout_seconds, self._default_timeout))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("QueryForge deadline exhausted")
        assert self._session is not None
        payload = deepcopy(arguments)
        if application_session_id is not None:
            payload["session_id"] = application_session_id
        try:
            called = await asyncio.wait_for(
                self._session.call_tool(name, payload), remaining
            )
        except asyncio.TimeoutError as exc:
            self._tools = ()
            raise TimeoutError("QueryForge call timed out") from exc
        except Exception as exc:
            self._tools = ()
            raise QueryForgeUnavailable("QueryForge tool call failed") from exc
        structured = deepcopy(called.structuredContent or {})
        if not isinstance(structured, dict):
            raise QueryForgeUnavailable("QueryForge returned an invalid structured response")
        error = self._normalize_error(structured.get("error"))
        return QueryForgeResult(
            ok=bool(structured.get("ok", not called.isError)),
            application_session_id=structured.get("session_id"),
            payload=structured,
            error=error,
        )

    async def ready(self, timeout_seconds: float) -> bool:
        try:
            await asyncio.wait_for(
                self.discover_tools(timeout_seconds, refresh=True), timeout_seconds
            )
            return True
        except (TimeoutError, asyncio.TimeoutError, QueryForgeUnavailable):
            return False

    async def release_application_session(
        self, application_session_id: str, timeout_seconds: float
    ) -> None:
        if not self._data_base_url:
            raise QueryForgeUnavailable("QueryForge Data API base URL is not configured")
        if timeout_seconds <= 0:
            raise TimeoutError("QueryForge release deadline exhausted")
        encoded = quote(application_session_id, safe="")
        try:
            response = await self._data_client.post(
                f"{self._data_base_url}/data/sessions/{encoded}/release",
                headers={"x-api-key": self._api_key},
                timeout=min(timeout_seconds, self._default_timeout),
            )
            if response.status_code == 404:
                return
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise TimeoutError("QueryForge session release timed out") from exc
        except httpx.HTTPError as exc:
            raise QueryForgeUnavailable("QueryForge session release failed") from exc

    @staticmethod
    def _normalize_error(raw: Any) -> QueryForgeFailure | None:
        if not isinstance(raw, dict):
            return None
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        raw_candidates = details.get("candidates", details.get("did_you_mean", []))
        candidates = raw_candidates if isinstance(raw_candidates, list) else []
        safe = {
            key: deepcopy(value)
            for key, value in details.items()
            if key in {"candidates", "did_you_mean", "expected_format", "supported_tools", "feature"}
        }
        return QueryForgeFailure(
            code=str(raw.get("code", "UNKNOWN")),
            message=str(raw.get("message", "QueryForge rejected the request")),
            retryable=bool(raw.get("retryable", False)),
            hint=str(raw["hint"]) if raw.get("hint") is not None else None,
            candidates=tuple(deepcopy(candidates)),
            safe_metadata=safe,
        )
