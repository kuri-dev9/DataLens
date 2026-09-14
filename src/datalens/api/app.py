from __future__ import annotations

import contextlib
import asyncio
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
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
from datalens.infrastructure.session_store import InMemorySessionStore


SESSION_PATTERN = re.compile(r"^dls_[A-Za-z0-9_-]{20,64}$")
LOG = logging.getLogger("datalens.http")


class MessageHandler(Protocol):
    async def handle(self, session: Any, message: str, request_id: str, deadline: float) -> dict: ...


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


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = f"dlr_{secrets.token_urlsafe(18)}"
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        LOG.info(
            "http_request",
            extra={
                "request_id": request.state.request_id,
                "method": request.method,
                "path": request.url.path,
                "session_id": request.path_params.get("session_id"),
                "operation": f"{request.method} {request.scope.get('route').path if request.scope.get('route') else request.url.path}",
                "status_code": response.status_code,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
        )
        return response


class ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, api_key: str) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path == "/v1/health":
            return await call_next(request)
        supplied = request.headers.get("x-api-key", "")
        if not self._api_key or not secrets.compare_digest(supplied, self._api_key):
            return error_response(request, "DL_UNAUTHORIZED", "Unauthorized", 401)
        return await call_next(request)


def build_app(
    settings: Settings,
    *,
    session_service: SessionService | None = None,
    readiness: Readiness | None = None,
    llm_readiness: Readiness | None = None,
    message_handler: MessageHandler | None = None,
    closeables: tuple[Any, ...] = (),
) -> Starlette:
    store = InMemorySessionStore(settings.session_ttl_seconds, settings.default_locale)
    sessions = session_service or SessionService(store)
    queryforge = readiness or McpQueryForgeClient(
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
        except AgentQueryRejected:
            return error_response(request, "DL_QUERY_REJECTED", "The data request was rejected", 422, session_id=session_id)
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

    app = Starlette(
        routes=[
            Route("/v1/health", health, methods=["GET"]),
            Route("/v1/ready", ready, methods=["GET"]),
            Route("/v1/sessions", create_session, methods=["POST"]),
            Route("/v1/sessions/{session_id:str}/messages", message, methods=["POST"]),
            Route("/v1/sessions/{session_id:str}", delete_session, methods=["DELETE"]),
        ],
        lifespan=lifespan,
    )
    app.add_middleware(ApiKeyMiddleware, api_key=settings.api_key.get_secret_value())
    app.add_middleware(RequestIdMiddleware)
    return app
