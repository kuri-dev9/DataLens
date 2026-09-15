from __future__ import annotations

import contextlib
import asyncio
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from datalens.application.sessions import (
    AgentNotReady,
    SessionBusy,
    SessionNotFound,
    SessionService,
    UnavailableMessageHandler,
)
from datalens.application.agent import (
    AgentInvalidUpstreamResponse,
    AgentInternalError,
    AgentLimitError,
    AgentPolicyError,
    AgentQueryRejected,
    AgentTimeoutError,
    AgentUpstreamUnavailable,
)
from datalens.config import Settings
from datalens.infrastructure.queryforge_mcp import McpQueryForgeClient
from datalens.ports.queryforge import QueryForgeClient, QueryForgeHttpResult, QueryForgeUnavailable
from datalens.infrastructure.session_store import InMemorySessionStore


SESSION_PATTERN = re.compile(r"^dls_[A-Za-z0-9_-]{20,64}$")
DATA_PAGE_DEFAULT = 100
DATA_PAGE_MAX = 1000
LOG = logging.getLogger("datalens.http")
CORS_METHODS = "GET, POST, DELETE, OPTIONS"
CORS_HEADERS = "x-api-key, content-type, accept"


class MessageHandler(Protocol):
    async def handle(self, session: Any, message: str, request_id: str, deadline: float) -> dict: ...
    async def handle_stream(
        self, session: Any, message: str, request_id: str, deadline: float, event_sink: Any
    ) -> dict: ...


class Readiness(Protocol):
    async def ready(self, timeout_seconds: float) -> bool: ...
    async def close(self) -> None: ...


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=8192)


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    locale: str | None = None


def error_response(
    request: Request,
    code: str,
    message: str,
    status: int,
    *,
    retryable: bool = False,
    session_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    payload = {
            "request_id": request.state.request_id,
            "status": "failed",
            "error": {"code": code, "message": message, "retryable": retryable, "details": details},
        }
    if session_id is not None:
        payload["session_id"] = session_id
    return JSONResponse(payload, status_code=status)


def sse_event(event: str, data: dict[str, Any]) -> bytes:
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {encoded}\n\n".encode()


def wants_sse(request: Request) -> bool:
    return "text/event-stream" in request.headers.get("accept", "").lower()


async def with_keepalive(events: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in events:
                await queue.put(event)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield b": ping\n\n"
                continue
            if item is None:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def sse_response(events: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(
        with_keepalive(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def agent_error_response(request: Request, exc: Exception, session_id: str) -> JSONResponse:
    if isinstance(exc, AgentNotReady):
        return error_response(request, "DL_AGENT_NOT_READY", "Agent is not available", 503, retryable=True, session_id=session_id)
    if isinstance(exc, AgentLimitError):
        return error_response(request, "DL_AGENT_LIMIT", "Agent execution limit reached", 422, session_id=session_id)
    if isinstance(exc, AgentPolicyError):
        return error_response(request, "DL_AGENT_INVALID_TOOL", "Agent requested an invalid tool", 422, session_id=session_id)
    if isinstance(exc, AgentTimeoutError):
        return error_response(request, "DL_UPSTREAM_TIMEOUT", "Request processing timed out", 504, retryable=True, session_id=session_id)
    if isinstance(exc, AgentUpstreamUnavailable):
        return error_response(request, "DL_UPSTREAM_UNAVAILABLE", "Upstream service is unavailable", 503, retryable=True, session_id=session_id)
    if isinstance(exc, AgentInvalidUpstreamResponse):
        return error_response(request, "DL_UPSTREAM_INVALID_RESPONSE", "Upstream service returned an invalid response", 502, session_id=session_id)
    if isinstance(exc, AgentQueryRejected):
        failure = exc.failure
        details = None
        if failure is not None:
            details = {
                "upstream_code": failure.code,
                "hint": failure.hint,
                **failure.safe_metadata,
            }
        return error_response(
            request,
            "DL_QUERY_REJECTED",
            "The data request was rejected",
            422,
            retryable=failure.retryable if failure is not None else False,
            details=details,
            session_id=session_id,
        )
    return error_response(request, "DL_INTERNAL_ERROR", "Internal error", 500, session_id=session_id)


class RequestIdMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})["request_id"] = f"dlr_{secrets.token_urlsafe(18)}"
        started = time.monotonic()
        status_code = 500

        async def send_with_request_id(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", scope["state"]["request_id"].encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_request_id)
        LOG.info(
            "http_request",
            extra={
                "request_id": scope["state"]["request_id"],
                "method": scope["method"],
                "path": scope["path"],
                "session_id": scope.get("path_params", {}).get("session_id"),
                "operation": f"{scope['method']} {getattr(scope.get('route'), 'path', scope['path'])}",
                "status_code": status_code,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
        )


class ApiKeyMiddleware:
    def __init__(self, app: Any, api_key: str) -> None:
        self.app = app
        self._api_key = api_key

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if request.url.path == "/v1/health":
            await self.app(scope, receive, send)
            return
        supplied = request.headers.get("x-api-key", "")
        if not self._api_key or not secrets.compare_digest(supplied, self._api_key):
            await error_response(request, "DL_UNAUTHORIZED", "Unauthorized", 401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class CorsMiddleware:
    def __init__(self, app: Any, origins: tuple[str, ...]) -> None:
        self.app = app
        self._origins = frozenset(origins)

    def _allowed_origin(self, origin: str) -> str | None:
        if "*" in self._origins:
            return "*"
        return origin if origin in self._origins else None

    @staticmethod
    def _headers(origin: str) -> list[tuple[bytes, bytes]]:
        return [
            (b"access-control-allow-origin", origin.encode()),
            (b"access-control-allow-headers", CORS_HEADERS.encode()),
            (b"access-control-allow-methods", CORS_METHODS.encode()),
        ]

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        origin = request.headers.get("origin", "")
        allowed = self._allowed_origin(origin) if origin else None
        if request.method == "OPTIONS":
            headers = {
                "Access-Control-Allow-Origin": allowed,
                "Access-Control-Allow-Headers": CORS_HEADERS,
                "Access-Control-Allow-Methods": CORS_METHODS,
            } if allowed else {}
            await Response(status_code=204, headers=headers)(scope, receive, send)
            return

        async def send_with_cors(message: dict) -> None:
            if message["type"] == "http.response.start" and allowed:
                headers = list(message.get("headers", [])) + self._headers(allowed)
                if allowed != "*":
                    headers.append((b"vary", b"Origin"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cors)


def build_app(
    settings: Settings,
    *,
    session_service: SessionService | None = None,
    readiness: Readiness | None = None,
    llm_readiness: Readiness | None = None,
    message_handler: MessageHandler | None = None,
    queryforge_client: QueryForgeClient | None = None,
    closeables: tuple[Any, ...] = (),
) -> Starlette:
    store = InMemorySessionStore(settings.session_ttl_seconds, settings.default_locale)
    sessions = session_service or SessionService(store)
    queryforge = queryforge_client or readiness or McpQueryForgeClient(
        settings.queryforge_url(),
        settings.queryforge_api_key.get_secret_value(),
        data_base_url=settings.queryforge_data_url(),
        default_timeout_seconds=settings.queryforge_timeout_seconds,
    )
    handler = message_handler or UnavailableMessageHandler()
    llm = llm_readiness or queryforge

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await sessions.shutdown_cleanup()
            closed: set[int] = set()
            for resource in (*closeables, llm, queryforge):
                if id(resource) in closed:
                    continue
                closed.add(id(resource))
                with contextlib.suppress(Exception):
                    await resource.close()

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def ready(_: Request) -> JSONResponse:
        async def available(resource: Readiness) -> bool:
            try:
                return await resource.ready(settings.queryforge_timeout_seconds)
            except Exception:
                return False

        queryforge_available, llm_available = await asyncio.gather(
            available(queryforge), available(llm)
        )
        available = queryforge_available and llm_available
        return JSONResponse(
            {
                "status": "ready" if available else "not_ready",
                "checks": {
                    "queryforge": {"status": "ok" if queryforge_available else "unavailable"},
                    "llm": {"status": "ok" if llm_available else "unavailable"},
                },
            },
            status_code=200 if available else 503,
        )

    async def create_session(request: Request) -> JSONResponse:
        await sessions.reap_expired()
        try:
            raw = await request.body()
            parsed = CreateSessionRequest.model_validate_json(raw) if raw else CreateSessionRequest()
        except (ValueError, ValidationError):
            return error_response(request, "DL_INVALID_REQUEST", "Invalid session request", 400)
        if parsed.locale not in {None, "ko", "ja"}:
            return error_response(
                request,
                "DL_INVALID_LOCALE",
                "Unsupported locale",
                400,
                details={"allowed_locales": ["ko", "ja"]},
            )
        session = sessions.create(locale=parsed.locale)
        response = JSONResponse(session.public(), status_code=201)
        response.headers["Location"] = f"/v1/sessions/{session.session_id}"
        return response

    async def message(request: Request) -> JSONResponse:
        await sessions.reap_expired()
        session_id = request.path_params["session_id"]
        if not SESSION_PATTERN.fullmatch(session_id):
            return error_response(request, "DL_SESSION_NOT_FOUND", "Session not found", 404)
        try:
            body = await request.json()
            parsed = MessageRequest.model_validate(body)
        except (ValueError, ValidationError):
            return error_response(request, "DL_INVALID_REQUEST", "Invalid message request", 400)
        try:
            session = sessions.begin_turn(session_id)
        except SessionNotFound:
            return error_response(request, "DL_SESSION_NOT_FOUND", "Session not found", 404)
        except SessionBusy:
            return error_response(request, "DL_SESSION_BUSY", "Session is processing another turn", 409, retryable=True, session_id=session_id)
        if wants_sse(request):
            async def events() -> AsyncIterator[bytes]:
                queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

                async def emit(event: str, data: dict[str, Any]) -> None:
                    await queue.put((event, data))

                async def execute() -> None:
                    try:
                        await emit("start", {"request_id": request.state.request_id, "session_id": session_id})
                        result = await handler.handle_stream(
                            session,
                            parsed.message,
                            request.state.request_id,
                            time.monotonic() + settings.request_deadline_seconds,
                            emit,
                        )
                        sessions.touch(session_id)
                        await emit("done", result)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await emit("error", json.loads(agent_error_response(request, exc, session_id).body))
                    finally:
                        sessions.end_turn(session_id)
                        await queue.put(None)

                task = asyncio.create_task(execute())
                try:
                    while True:
                        try:
                            item = await asyncio.wait_for(queue.get(), timeout=15)
                        except asyncio.TimeoutError:
                            yield b": ping\n\n"
                            continue
                        if item is None:
                            break
                        yield sse_event(*item)
                finally:
                    if not task.done():
                        task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            return sse_response(events())
        try:
            deadline = time.monotonic() + settings.request_deadline_seconds
            result = await handler.handle(session, parsed.message, request.state.request_id, deadline)
            sessions.touch(session_id)
            return JSONResponse(result)
        except AgentNotReady:
            return error_response(request, "DL_AGENT_NOT_READY", "Agent is not available", 503, retryable=True, session_id=session_id)
        except AgentLimitError:
            return error_response(request, "DL_AGENT_LIMIT", "Agent execution limit reached", 422, session_id=session_id)
        except AgentPolicyError:
            return error_response(request, "DL_AGENT_INVALID_TOOL", "Agent requested an invalid tool", 422, session_id=session_id)
        except AgentTimeoutError:
            return error_response(request, "DL_UPSTREAM_TIMEOUT", "Request processing timed out", 504, retryable=True, session_id=session_id)
        except AgentUpstreamUnavailable:
            return error_response(request, "DL_UPSTREAM_UNAVAILABLE", "Upstream service is unavailable", 503, retryable=True, session_id=session_id)
        except AgentInvalidUpstreamResponse:
            return error_response(request, "DL_UPSTREAM_INVALID_RESPONSE", "Upstream service returned an invalid response", 502, session_id=session_id)
        except AgentQueryRejected as exc:
            return agent_error_response(request, exc, session_id)
        except AgentInternalError:
            return error_response(request, "DL_INTERNAL_ERROR", "Internal error", 500, session_id=session_id)
        except Exception:
            LOG.error(
                "message_handler_failed",
                extra={
                    "request_id": request.state.request_id,
                    "session_id": session_id,
                    "operation": "agent_turn",
                    "status": "failed",
                    "error_code": "DL_INTERNAL_ERROR",
                },
            )
            return error_response(request, "DL_INTERNAL_ERROR", "Internal error", 500, session_id=session_id)
        finally:
            sessions.end_turn(session_id)

    async def delete_session(request: Request) -> Response:
        await sessions.reap_expired()
        await sessions.delete(request.path_params["session_id"])
        return Response(status_code=204)

    def queryforge_session(
        request: Request, *, dataset: bool
    ) -> tuple[str, str | None] | JSONResponse:
        session_id = request.path_params.get("session_id", "")
        try:
            session = sessions.require(session_id)
        except SessionNotFound:
            code = "DL_DATASET_NOT_FOUND" if dataset else "DL_SESSION_NOT_FOUND"
            message = "Dataset not found" if dataset else "Session not found"
            return error_response(request, code, message, 404)
        if session.queryforge_session_id is None:
            if dataset:
                return error_response(request, "DL_DATASET_NOT_FOUND", "Dataset not found", 404)
        return session_id, session.queryforge_session_id

    def bind_queryforge_session(session_id: str, application_session_id: str | None) -> None:
        if application_session_id is not None:
            sessions.bind_queryforge_session(session_id, application_session_id)

    def pagination(request: Request) -> tuple[int, int] | JSONResponse:
        try:
            offset = int(request.query_params.get("offset", 0))
            requested_limit = int(request.query_params.get("limit", DATA_PAGE_DEFAULT))
        except ValueError:
            return error_response(request, "DL_INVALID_PAGINATION", "Invalid pagination", 400)
        if offset < 0 or requested_limit < 1:
            return error_response(request, "DL_INVALID_PAGINATION", "Invalid pagination", 400)
        return offset, min(requested_limit, DATA_PAGE_MAX)

    def data_result(request: Request, result: QueryForgeHttpResult) -> JSONResponse:
        if 200 <= result.status_code < 300:
            return JSONResponse(result.payload, status_code=result.status_code)
        if result.status_code == 410:
            return error_response(request, "DL_DATASET_EXPIRED", "Dataset expired", 410)
        if result.status_code == 404:
            return error_response(request, "DL_DATASET_NOT_FOUND", "Dataset not found", 404)
        if result.status_code == 400:
            return error_response(request, "DL_INVALID_PAGINATION", "Invalid pagination", 400)
        return error_response(
            request, "DL_UPSTREAM_UNAVAILABLE", "Upstream service is unavailable", 503, retryable=True
        )

    async def dataset_rows(request: Request) -> JSONResponse:
        owner = queryforge_session(request, dataset=True)
        if isinstance(owner, JSONResponse):
            return owner
        _, application_session_id = owner
        assert application_session_id is not None
        page = pagination(request)
        if isinstance(page, JSONResponse):
            return page
        offset, limit = page
        if wants_sse(request):
            async def events() -> AsyncIterator[bytes]:
                try:
                    meta = await queryforge.fetch_dataset_meta(
                        request.path_params["dataset_id"], application_session_id,
                        timeout_seconds=settings.queryforge_timeout_seconds,
                    )
                    if not 200 <= meta.status_code < 300:
                        yield sse_event("error", json.loads(data_result(request, meta).body))
                        return
                    yield sse_event("meta", meta.payload)
                    returned = 0
                    while returned < limit:
                        chunk_limit = min(100, limit - returned)
                        result = await queryforge.fetch_dataset_rows(
                            request.path_params["dataset_id"], application_session_id,
                            offset=offset + returned, limit=chunk_limit,
                            timeout_seconds=settings.queryforge_timeout_seconds,
                        )
                        if not 200 <= result.status_code < 300:
                            yield sse_event("error", json.loads(data_result(request, result).body))
                            return
                        rows = result.payload.get("rows", [])
                        yield sse_event("rows", {"offset": offset + returned, "rows": rows})
                        returned += len(rows)
                        if len(rows) < chunk_limit:
                            break
                    yield sse_event("done", {"returned": returned, "total": meta.payload.get("row_count")})
                except TimeoutError:
                    yield sse_event("error", json.loads(error_response(request, "DL_UPSTREAM_TIMEOUT", "Upstream request timed out", 504, retryable=True).body))
                except QueryForgeUnavailable:
                    yield sse_event("error", json.loads(error_response(request, "DL_UPSTREAM_UNAVAILABLE", "Upstream service is unavailable", 503, retryable=True).body))

            return sse_response(events())
        try:
            result = await queryforge.fetch_dataset_rows(
                request.path_params["dataset_id"],
                application_session_id,
                offset=offset,
                limit=limit,
                timeout_seconds=settings.queryforge_timeout_seconds,
            )
        except TimeoutError:
            return error_response(
                request, "DL_UPSTREAM_TIMEOUT", "Upstream request timed out", 504, retryable=True
            )
        except QueryForgeUnavailable:
            return error_response(
                request, "DL_UPSTREAM_UNAVAILABLE", "Upstream service is unavailable", 503, retryable=True
            )
        return data_result(request, result)

    async def dataset_meta(request: Request) -> JSONResponse:
        owner = queryforge_session(request, dataset=True)
        if isinstance(owner, JSONResponse):
            return owner
        _, application_session_id = owner
        assert application_session_id is not None
        try:
            result = await queryforge.fetch_dataset_meta(
                request.path_params["dataset_id"],
                application_session_id,
                timeout_seconds=settings.queryforge_timeout_seconds,
            )
        except TimeoutError:
            return error_response(
                request, "DL_UPSTREAM_TIMEOUT", "Upstream request timed out", 504, retryable=True
            )
        except QueryForgeUnavailable:
            return error_response(
                request, "DL_UPSTREAM_UNAVAILABLE", "Upstream service is unavailable", 503, retryable=True
            )
        return data_result(request, result)

    async def catalog_tables(request: Request) -> JSONResponse:
        owner = queryforge_session(request, dataset=False)
        if isinstance(owner, JSONResponse):
            return owner
        session_id, application_session_id = owner
        page = pagination(request)
        if isinstance(page, JSONResponse):
            return page
        offset, limit = page
        arguments: dict[str, Any] = {
            "action": "list_tables",
            "limit": min(DATA_PAGE_MAX, offset + limit),
        }
        pattern = request.query_params.get("pattern")
        if pattern is not None:
            arguments["name_pattern"] = pattern
        try:
            result = await queryforge.call_tool(
                "schema",
                arguments,
                application_session_id=application_session_id,
                timeout_seconds=settings.queryforge_timeout_seconds,
            )
        except TimeoutError:
            return error_response(
                request, "DL_UPSTREAM_TIMEOUT", "Upstream request timed out", 504, retryable=True
            )
        except QueryForgeUnavailable:
            return error_response(
                request, "DL_UPSTREAM_UNAVAILABLE", "Upstream service is unavailable", 503, retryable=True
            )
        if not result.ok:
            return error_response(request, "DL_CATALOG_UNAVAILABLE", "Catalog request failed", 502)
        bind_queryforge_session(session_id, result.application_session_id)
        tables = result.payload.get("tables", [])
        warnings = result.payload.get("warnings", [])
        total_count = next(
            (
                warning.get("original_items")
                for warning in warnings
                if warning.get("code") == "TABLES_TRUNCATED"
                and isinstance(warning.get("original_items"), int)
            ),
            len(tables),
        )
        returned = tables[offset : offset + limit]
        if wants_sse(request):
            async def events() -> AsyncIterator[bytes]:
                yield sse_event("meta", {"offset": offset, "limit": limit, "total_count": total_count})
                for index in range(0, len(returned), 100):
                    yield sse_event("tables", {"offset": offset + index, "tables": returned[index:index + 100]})
                yield sse_event("done", {"returned": len(returned), "total": total_count})

            return sse_response(events())
        return JSONResponse(
            {
                "offset": offset,
                "limit": limit,
                "total_count": total_count,
                "returned_count": len(returned),
                "tables": returned,
                "truncated": offset + len(returned) < total_count,
                "warnings": warnings,
            }
        )

    async def catalog_columns(request: Request) -> JSONResponse:
        owner = queryforge_session(request, dataset=False)
        if isinstance(owner, JSONResponse):
            return owner
        session_id, application_session_id = owner
        try:
            result = await queryforge.call_tool(
                "schema",
                {"action": "list_columns", "table": request.path_params["table"]},
                application_session_id=application_session_id,
                timeout_seconds=settings.queryforge_timeout_seconds,
            )
        except TimeoutError:
            return error_response(
                request, "DL_UPSTREAM_TIMEOUT", "Upstream request timed out", 504, retryable=True
            )
        except QueryForgeUnavailable:
            return error_response(
                request, "DL_UPSTREAM_UNAVAILABLE", "Upstream service is unavailable", 503, retryable=True
            )
        if not result.ok:
            status = 404 if result.error and result.error.code in {"UNKNOWN_TABLE", "TABLE_NOT_ALLOWED"} else 502
            code = "DL_CATALOG_NOT_FOUND" if status == 404 else "DL_CATALOG_UNAVAILABLE"
            return error_response(request, code, "Catalog entry not found" if status == 404 else "Catalog request failed", status)
        bind_queryforge_session(session_id, result.application_session_id)
        columns = result.payload.get("columns", [])
        warnings = result.payload.get("warnings", [])
        total_count = next(
            (
                warning.get("original_items")
                for warning in warnings
                if warning.get("code") == "COLUMNS_TRUNCATED"
                and isinstance(warning.get("original_items"), int)
            ),
            len(columns),
        )
        return JSONResponse(
            {
                "table": result.payload.get("table"),
                "total_count": total_count,
                "returned_count": len(columns),
                "columns": columns,
                "truncated": bool(result.payload.get("truncated")),
                "warnings": warnings,
            }
        )

    app = Starlette(
        routes=[
            Route("/v1/health", health, methods=["GET"]),
            Route("/v1/ready", ready, methods=["GET"]),
            Route("/v1/sessions", create_session, methods=["POST"]),
            Route("/v1/sessions/{session_id:str}/messages", message, methods=["POST"]),
            Route("/v1/sessions/{session_id:str}", delete_session, methods=["DELETE"]),
            Route("/v1/sessions/{session_id:str}/datasets/{dataset_id:str}/rows", dataset_rows, methods=["GET"]),
            Route("/v1/sessions/{session_id:str}/datasets/{dataset_id:str}/meta", dataset_meta, methods=["GET"]),
            Route("/v1/sessions/{session_id:str}/catalog/tables", catalog_tables, methods=["GET"]),
            Route("/v1/sessions/{session_id:str}/catalog/tables/{table:str}/columns", catalog_columns, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(ApiKeyMiddleware, api_key=settings.api_key.get_secret_value())
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(CorsMiddleware, origins=settings.allowed_cors_origins)
    return app
