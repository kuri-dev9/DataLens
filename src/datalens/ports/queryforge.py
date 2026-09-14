from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class QueryForgeToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QueryForgeFailure:
    code: str
    message: str
    retryable: bool
    hint: str | None = None
    candidates: tuple[Any, ...] = field(default_factory=tuple)
    safe_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QueryForgeResult:
    ok: bool
    application_session_id: str | None
    payload: dict[str, Any]
    error: QueryForgeFailure | None = None


@dataclass(frozen=True, slots=True)
class QueryForgeHttpResult:
    status_code: int
    payload: dict[str, Any]


class QueryForgeUnavailable(RuntimeError):
    pass


class QueryForgeClient(Protocol):
    async def connect(self, timeout_seconds: float | None = None) -> None: ...
    async def close(self) -> None: ...
    async def discover_tools(
        self, timeout_seconds: float | None = None, *, refresh: bool = False
    ) -> tuple[QueryForgeToolDefinition, ...]: ...
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        application_session_id: str | None,
        timeout_seconds: float,
    ) -> QueryForgeResult: ...
    async def ready(self, timeout_seconds: float) -> bool: ...
    async def release_application_session(
        self, application_session_id: str, timeout_seconds: float
    ) -> None: ...
    async def fetch_dataset_rows(
        self,
        dataset_id: str,
        application_session_id: str,
        *,
        offset: int,
        limit: int,
        timeout_seconds: float,
    ) -> QueryForgeHttpResult: ...
    async def fetch_dataset_meta(
        self,
        dataset_id: str,
        application_session_id: str,
        *,
        timeout_seconds: float,
    ) -> QueryForgeHttpResult: ...
