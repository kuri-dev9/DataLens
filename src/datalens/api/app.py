from __future__ import annotations

import contextlib
import asyncio
import json
import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal, Protocol

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
from datalens.application.memory import MemoryService
from dataclasses import replace as dc_replace
from datetime import UTC, datetime

from datalens.application import answer_style
from datalens.application.runtime_config import MODES, RuntimeConfig, RuntimeConfigError
from datalens.application.trace import TurnLog, blocks
from datalens.domain.session import Session
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
# 사용자 메시지 1건의 문자 수 상한(ADR-017 개정 2026-10-15). ollama_num_ctx=16384에서
# 한국어 2.15자/토큰 실측 기준 약 7.6K 토큰이며, 나머지는 시스템 프롬프트·도구 정의·
# 스키마·preview·생성 예약(num_predict)이 쓴다. num_ctx를 올리면 같이 올려야 한다.
MESSAGE_MAX_CHARS = 16384


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
    message: str = Field(min_length=1, max_length=MESSAGE_MAX_CHARS)
    # 요청 모드(2026-11-04). 생략하면 agent — 지금까지의 동작이다.
    # evidence 는 보낸 맥락만으로 답한다(QueryForge 지침·데이터셋 목록·도구를 싣지 않는다).
    # 무엇을 싣는지는 모드별로 설정 화면(/ui)이 정한다.
    mode: Literal["agent", "evidence"] | None = None


class RuntimeSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["agent", "evidence"]
    # 값으로 null 을 주면 그 항목의 화면 값을 지운다(→ .env·기본값).
    context: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    reset: bool = False


# 미리보기 내장 예시 — 실제 요청 기록이 없을 때의 바탕. RCA 응답 예시는 graphio 질의 모양을 줄여 옮겼다
PREVIEW_EXAMPLES = {
    "evidence": (
        "너는 LTE 망 RCA 보조다. 아래 증거팩만 근거로 답한다. (graphio 질의 지시를 줄인 내장 예시)\n"
        "1. 증거팩에 없는 수치·사실을 만들지 않는다. 문장마다 근거 증거 ID 를 붙인다.\n"
        + answer_style.block(answer_style.DEFAULTS)
        + "\n\n질문: 지금 결론이 무엇인가요? 근거와 영향 범위를 함께 설명해 주세요.\n"
        "증거팩:\n{\"evidence\": [{\"id\": \"EV-00\", \"data\": {\"trigger\": \"C-02\", \"drop_pp\": -47.7}}]}"
    ),
    "agent": "지난주 서울 지역 일별 접속 성공률을 보여 줘",
}


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["agent", "evidence"]
    # 바탕 입력 — last: 그 모드의 가장 최근 실제 요청 · example: 내장 예시 · custom: message 그대로.
    # 생략하면 message 가 있으면 custom, 없으면 last(없으면 example 로 물러난다).
    baseline: Literal["last", "example", "custom"] | None = None
    message: str | None = Field(default=None, min_length=1, max_length=MESSAGE_MAX_CHARS)
    locale: Literal["ko", "ja"] | None = None
    # 주면 그 세션의 실제 이력으로 조립한다(최근 전송의 세션 등). 없으면 이력 없이 조립한다.
    session_id: str | None = Field(default=None, max_length=80)
    # 저장하지 않은 화면 값 — "이렇게 바꾸면 얼마나 달라지나"를 저장 전에 본다.
    context: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    # 저장하지 않은 답변 스타일(RCA 응답). 빠진 항목은 저장된 값 — answer_style.apply 로 실제와 같게 조립한다
    answer_style: dict[str, Any] | None = None


class GlossaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    term: str = Field(min_length=1, max_length=128)
    locale: Literal["ko", "ja"] | None = None
    table: str | None = Field(default=None, max_length=128)
    columns: list[str] = Field(default_factory=list, max_length=30)
    note: str | None = Field(default=None, max_length=512)


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
    metadata: dict[str, Any] | None = None,
    datasets: list[dict[str, Any]] | None = None,
    warnings: list[Any] | None = None,
) -> JSONResponse:
    payload = {
            "request_id": request.state.request_id,
            "status": "failed",
            "error": {"code": code, "message": message, "retryable": retryable, "details": details},
        }
    if metadata is not None:
        payload["metadata"] = metadata
    if datasets is not None:
        payload["datasets"] = datasets
    if warnings is not None:
        payload["warnings"] = warnings
    if session_id is not None:
        payload["session_id"] = session_id
    return JSONResponse(payload, status_code=status)


def invalid_request(request: Request, message: str, exc: ValidationError | None) -> JSONResponse:
    """검증 실패를 사유와 함께 400으로 돌려준다.

    JSON 자체가 깨진 것("Malformed JSON")과 필드가 규격에 안 맞는 것을 가른다 —
    클라이언트가 고치는 방법이 전혀 다르다. 사유는 loc·msg·type만 추린다:
    pydantic errors()는 기본적으로 입력값을 포함하는데, 그대로 두면 질문 본문이
    에러 응답과 로그에 실려 운영 데이터가 샌다.
    """
    if exc is None:
        return error_response(request, "DL_INVALID_REQUEST", "Malformed JSON", 400)
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    if any(error["type"] == "json_invalid" for error in errors):
        return error_response(request, "DL_INVALID_REQUEST", "Malformed JSON", 400)
    return error_response(
        request,
        "DL_INVALID_REQUEST",
        message,
        400,
        details={
            "fields": [
                {
                    "field": ".".join(str(part) for part in error["loc"]) or "(body)",
                    "error": error["msg"],
                    "type": error["type"],
                }
                for error in errors
            ]
        },
    )


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


def agent_error_response(request: Request, exc: Exception, session_id: str, started: float | None = None) -> JSONResponse:
    failed_step = getattr(exc, "failed_step", None)
    metadata = {
        "duration_ms": int((time.monotonic() - started) * 1000) if started is not None else 0,
        "tool_calls": getattr(exc, "tool_calls", 0),
        "recovery_count": getattr(exc, "recovery_count", 0),
        "stop_reason": "failed",
        "failed_step": failed_step,
    }
    # 실패 전에 확보한 데이터셋은 그대로 돌려준다.
    datasets = [dataset.public() for dataset in getattr(exc, "datasets", ())]
    warnings = list(getattr(exc, "warnings", ()))

    def failed(code: str, message: str, status: int, *, retryable: bool = False, details: dict[str, Any] | None = None) -> JSONResponse:
        enriched = {**(details or {}), "failed_step": failed_step} if failed_step else details
        return error_response(request, code, message, status, retryable=retryable, details=enriched, session_id=session_id, metadata=metadata, datasets=datasets, warnings=warnings)
    if isinstance(exc, AgentNotReady):
        return failed("DL_AGENT_NOT_READY", "Agent is not available", 503, retryable=True)
    if isinstance(exc, AgentLimitError):
        return failed("DL_AGENT_LIMIT", "Agent execution limit reached", 422)
    if isinstance(exc, AgentPolicyError):
        return failed("DL_AGENT_INVALID_TOOL", "Agent requested an invalid tool", 422)
    if isinstance(exc, AgentTimeoutError):
        return failed("DL_UPSTREAM_TIMEOUT", "Request processing timed out", 504, retryable=True)
    if isinstance(exc, AgentUpstreamUnavailable):
        return failed("DL_UPSTREAM_UNAVAILABLE", "Upstream service is unavailable", 503, retryable=True)
    if isinstance(exc, AgentInvalidUpstreamResponse):
        return failed("DL_UPSTREAM_INVALID_RESPONSE", "Upstream service returned an invalid response", 502)
    if isinstance(exc, AgentQueryRejected):
        failure = exc.failure
        details = None
        if failure is not None:
            details = {
                "upstream_code": failure.code,
                "hint": failure.hint,
                **failure.safe_metadata,
            }
        return failed(
            "DL_QUERY_REJECTED",
            "The data request was rejected",
            422,
            retryable=failure.retryable if failure is not None else False,
            details=details,
        )
    return failed("DL_INTERNAL_ERROR", "Internal error", 500)


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
        if request.url.path in {"/v1/health", "/ui"}:
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
    memory: MemoryService | None = None,
    closeables: tuple[Any, ...] = (),
    runtime_config: RuntimeConfig | None = None,
    turn_log: TurnLog | None = None,
    previewer: Any = None,
) -> Starlette:
    store = InMemorySessionStore(settings.session_ttl_seconds, settings.default_locale)
    sessions = session_service or SessionService(store)
    queryforge = queryforge_client or readiness or McpQueryForgeClient(
        settings.queryforge_url(),
        settings.queryforge_api_key.get_secret_value(),
        data_base_url=settings.queryforge_data_url(),
        default_timeout_seconds=settings.queryforge_timeout_seconds,
        call_timeout_seconds=settings.queryforge_call_timeout_seconds,
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
            for resource in (*closeables, *( (memory,) if memory is not None else () ), llm, queryforge):
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
                    "memory": {"status": "disabled" if memory is None else ("ok" if await memory.ready(settings.queryforge_timeout_seconds) else "unavailable")},
                },
            },
            status_code=200 if available else 503,
        )

    async def create_session(request: Request) -> JSONResponse:
        await sessions.reap_expired()
        try:
            raw = await request.body()
            parsed = CreateSessionRequest.model_validate_json(raw) if raw else CreateSessionRequest()
        except ValidationError as exc:
            return invalid_request(request, "Invalid session request", exc)
        except ValueError:
            return invalid_request(request, "Invalid session request", None)
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
        except ValidationError as exc:
            return invalid_request(request, "Invalid message request", exc)
        except ValueError:
            return invalid_request(request, "Invalid message request", None)
        try:
            session = sessions.begin_turn(session_id)
        except SessionNotFound:
            return error_response(request, "DL_SESSION_NOT_FOUND", "Session not found", 404)
        except SessionBusy:
            return error_response(request, "DL_SESSION_BUSY", "Session is processing another turn", 409, retryable=True, session_id=session_id)
        turn_started = time.monotonic()
        # 모드를 지정한 요청만 넘긴다 — 생략한 요청은 예전 호출 모양 그대로 간다.
        mode_kwargs = {"mode": parsed.mode} if parsed.mode else {}
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
                            **mode_kwargs,
                        )
                        sessions.touch(session_id)
                        await emit("done", result)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        await emit("error", json.loads(agent_error_response(request, exc, session_id, turn_started).body))
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
            result = await handler.handle(session, parsed.message, request.state.request_id, deadline, **mode_kwargs)
            sessions.touch(session_id)
            return JSONResponse(result)
        except (
            AgentNotReady,
            AgentLimitError,
            AgentPolicyError,
            AgentTimeoutError,
            AgentUpstreamUnavailable,
            AgentInvalidUpstreamResponse,
            AgentQueryRejected,
            AgentInternalError,
        ) as exc:
            return agent_error_response(request, exc, session_id, turn_started)
        except Exception as exc:
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
            return agent_error_response(request, exc, session_id, turn_started)
        finally:
            sessions.end_turn(session_id)

    async def delete_session(request: Request) -> Response:
        await sessions.reap_expired()
        await sessions.delete(request.path_params["session_id"])
        return Response(status_code=204)

    def memory_unavailable(request: Request) -> JSONResponse:
        return error_response(request, "DL_MEMORY_DISABLED", "Memory is not enabled", 503)

    async def memory_recipes(request: Request) -> JSONResponse:
        if memory is None:
            return memory_unavailable(request)
        locale = request.query_params.get("locale")
        if locale not in {None, "ko", "ja"}:
            return error_response(request, "DL_INVALID_LOCALE", "Unsupported locale", 400)
        limit = min(int(request.query_params.get("limit", 50) or 50), 200)
        recipes = memory.list_recipes(locale, limit=limit)
        return JSONResponse({"recipes": [recipe.public() for recipe in recipes]})

    async def memory_delete_recipe(request: Request) -> Response:
        if memory is None:
            return memory_unavailable(request)
        memory.delete_recipe(request.path_params["recipe_id"])
        return Response(status_code=204)

    async def memory_terms(request: Request) -> JSONResponse:
        if memory is None:
            return memory_unavailable(request)
        if request.method == "GET":
            locale = request.query_params.get("locale")
            if locale not in {None, "ko", "ja"}:
                return error_response(request, "DL_INVALID_LOCALE", "Unsupported locale", 400)
            limit = min(int(request.query_params.get("limit", 100) or 100), 500)
            return JSONResponse({"terms": [entry.public() for entry in memory.list_terms(locale, limit=limit)]})
        try:
            parsed = GlossaryRequest.model_validate(await request.json())
        except ValidationError as exc:
            return invalid_request(request, "Invalid glossary request", exc)
        except ValueError:
            return invalid_request(request, "Invalid glossary request", None)
        entry = await memory.upsert_term(
            parsed.term,
            parsed.locale or settings.default_locale,
            table=parsed.table,
            columns=parsed.columns,
            note=parsed.note,
        )
        if entry is None:
            return error_response(
                request, "DL_MEMORY_UNAVAILABLE", "Memory backend is unavailable", 503, retryable=True
            )
        return JSONResponse(entry.public(), status_code=201)

    async def memory_delete_term(request: Request) -> Response:
        if memory is None:
            return memory_unavailable(request)
        memory.delete_term(request.path_params["term_id"])
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
                        timeout_seconds=settings.queryforge_call_timeout_seconds,
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
                            timeout_seconds=settings.queryforge_call_timeout_seconds,
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
                timeout_seconds=settings.queryforge_call_timeout_seconds,
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
                timeout_seconds=settings.queryforge_call_timeout_seconds,
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
                timeout_seconds=settings.queryforge_call_timeout_seconds,
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
                timeout_seconds=settings.queryforge_call_timeout_seconds,
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

    def runtime_unavailable(request: Request) -> JSONResponse:
        return error_response(request, "DL_SETTINGS_DISABLED", "Runtime settings are not enabled", 404)

    def runtime_payload() -> dict[str, Any]:
        assert runtime_config is not None
        described = runtime_config.describe()
        described["limits"] = {
            "message_max_chars": MESSAGE_MAX_CHARS,
            "request_deadline_seconds": settings.request_deadline_seconds,
            "session_ttl_seconds": settings.session_ttl_seconds,
        }
        return described

    async def runtime_settings(request: Request) -> JSONResponse:
        if runtime_config is None:
            return runtime_unavailable(request)
        if request.method == "GET":
            return JSONResponse(runtime_payload())
        try:
            parsed = RuntimeSettingsRequest.model_validate(await request.json())
        except ValidationError as exc:
            return invalid_request(request, "Invalid settings request", exc)
        except ValueError:
            return invalid_request(request, "Invalid settings request", None)
        try:
            if parsed.reset:
                runtime_config.reset(parsed.mode)
            else:
                runtime_config.update(parsed.mode, parsed.context, parsed.params)
        except RuntimeConfigError as exc:
            return error_response(request, "DL_INVALID_SETTINGS", str(exc), 400)
        LOG.info(
            "runtime_settings_updated",
            extra={"request_id": request.state.request_id, "operation": "runtime_settings", "status": "completed"},
        )
        return JSONResponse(runtime_payload())

    async def settings_traces(request: Request) -> JSONResponse:
        if turn_log is None:
            return runtime_unavailable(request)
        mode = request.query_params.get("mode") or None
        if mode is not None and mode not in MODES:
            return error_response(request, "DL_INVALID_SETTINGS", "unknown mode", 400)
        return JSONResponse({"keep": turn_log.keep, "traces": turn_log.traces(mode)})

    async def settings_evidence_turns(request: Request) -> JSONResponse:
        rows = turn_log.evidence_turns() if turn_log is not None else None
        if rows is None:
            return runtime_unavailable(request)
        return JSONResponse({"rows": rows})

    async def settings_answer_style(request: Request) -> JSONResponse:
        # evidence 답 형식·길이 — graphio `[답변 형식]` 구역을 바꿔 끼울 값. 저장하면 다음 요청부터 적용
        if runtime_config is None:
            return runtime_unavailable(request)
        if request.method == "GET":
            return JSONResponse({"answer_style": runtime_config.answer_style(), "defaults": answer_style.DEFAULTS})
        try:
            body = await request.json()
        except ValueError:
            return invalid_request(request, "Invalid answer style request", None)
        if not isinstance(body, dict):
            return invalid_request(request, "Invalid answer style request", None)
        try:
            style = runtime_config.update_answer_style(body)
        except RuntimeConfigError as exc:
            return error_response(request, "DL_INVALID_SETTINGS", str(exc), 400)
        LOG.info(
            "answer_style_updated",
            extra={"request_id": request.state.request_id, "operation": "answer_style", "status": "completed"},
        )
        return JSONResponse({"answer_style": style, "defaults": answer_style.DEFAULTS})

    async def session_verdict(request: Request) -> JSONResponse:
        # graphio 검증 판정 수신 — 그 세션의 가장 최근 evidence 기록에 붙여 재현 판정을 덮는다
        if turn_log is None:
            return runtime_unavailable(request)
        try:
            body = await request.json()
        except ValueError:
            return invalid_request(request, "Invalid verdict request", None)
        if not isinstance(body, dict) or not isinstance(body.get("dropped", []), list):
            return invalid_request(request, "Invalid verdict request", None)
        at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        attached = turn_log.attach_verdict(request.path_params["session_id"], body, at)
        if attached is None:
            return error_response(request, "DL_SESSION_NOT_FOUND", "No evidence turn for this session", 404)
        return JSONResponse({"attached": attached, "at": at})

    async def settings_history(request: Request) -> JSONResponse:
        if turn_log is None:
            return runtime_unavailable(request)
        return JSONResponse({"rows": turn_log.history()})

    async def settings_preview(request: Request) -> JSONResponse:
        if runtime_config is None or previewer is None:
            return runtime_unavailable(request)
        try:
            parsed = PreviewRequest.model_validate(await request.json())
        except ValidationError as exc:
            return invalid_request(request, "Invalid preview request", exc)
        except ValueError:
            return invalid_request(request, "Invalid preview request", None)
        profile = runtime_config.profile(parsed.mode)
        try:
            context = {k: RuntimeConfig._check_context(k, v) for k, v in (parsed.context or {}).items()}
            params = {k: RuntimeConfig._check_param(k, v) for k, v in (parsed.params or {}).items()}
        except RuntimeConfigError as exc:
            return error_response(request, "DL_INVALID_SETTINGS", str(exc), 400)
        profile = dc_replace(
            profile,
            **{k: v for k, v in context.items() if v is not None},
            params={**profile.params, **{k: v for k, v in params.items() if v is not None}},
        )
        kind = parsed.baseline or ("custom" if parsed.message else "last")
        if kind == "custom" and not parsed.message:
            return invalid_request(request, "Invalid preview request", None)
        basis: dict[str, Any] = {"kind": kind}
        message_text = parsed.message or ""
        if kind == "last":
            last = turn_log.last_request(parsed.mode, answer_style.OPEN if parsed.mode == "evidence" else None) if turn_log else None
            if last is None:
                kind = basis["kind"] = "example"
                basis["fallback"] = "이 모드의 실제 요청 기록이 없다"
            else:
                message_text = last["message"]
                basis.update({"at": last["at"], "session_id": last["session_id"], "source": last["source"]})
        if kind == "example":
            message_text = PREVIEW_EXAMPLES[parsed.mode]
        style_record = None
        if parsed.mode == "evidence":
            try:
                style = answer_style.check({**runtime_config.answer_style(), **(parsed.answer_style or {})})
            except answer_style.AnswerStyleError as exc:
                return error_response(request, "DL_INVALID_SETTINGS", str(exc), 400)
            # 실제 전송(chat.handle)과 같은 함수로 바꿔 끼운다 — 미리보기와 전송이 갈라지지 않게
            message_text, style_record = answer_style.apply(message_text, style)
        session = None
        if parsed.session_id:
            try:
                session = sessions.require(parsed.session_id)
            except SessionNotFound:
                session = None
        if session is None:
            now = datetime.now(UTC)
            session = Session("dls_preview", now, now, now, locale=parsed.locale or settings.default_locale)
        messages, tools, base_len, note = await previewer.preview(
            session, message_text, profile, settings.queryforge_timeout_seconds
        )
        if parsed.session_id and session.session_id == "dls_preview":
            note = (note + " · " if note else "") + "그 세션이 만료돼 이력 없이 조립했다"
        built = blocks(messages, tools, base_len, None, turn_log.chars_per_token(parsed.mode) if turn_log else None)
        num_ctx = int(profile.params.get("num_ctx") or settings.ollama_num_ctx)
        num_predict = int(profile.params.get("num_predict") or settings.ollama_num_predict)
        need = built["tokens_est_total"] + num_predict
        built.update(
            {
                "profile": profile.public(),
                "num_ctx": num_ctx,
                "num_predict": num_predict,
                # 입력(추정) + 출력 예약이 컨텍스트 창을 얼마나 쓰나. ports.llm.TokenUsage.context_status 와 같은 판정.
                "usage_pct": round(100 * need / num_ctx, 1),
                "status_est": "truncated" if built["tokens_est_total"] >= num_ctx else ("tight" if need > num_ctx else "ok"),
                "note": note,
                "basis": basis,
                "style": style_record,
                # 요청 검증 상한은 graphio 가 보낸 메시지에 걸린다 — 바꿔 끼운 뒤 길이와 함께 보인다
                "message_chars": len(message_text),
                "message_max_chars": MESSAGE_MAX_CHARS,
            }
        )
        return JSONResponse(built)

    async def settings_ui(_: Request) -> Response:
        # 화면 자체는 인증 없이 내려준다. 화면이 부르는 /v1/settings 는 x-api-key 가 필요하다.
        from importlib.resources import files

        html = files("datalens.ui").joinpath("settings.html").read_text(encoding="utf-8")
        return Response(html, media_type="text/html; charset=utf-8")

    routes = [
        Route("/v1/health", health, methods=["GET"]),
        Route("/ui", settings_ui, methods=["GET"]),
        Route("/v1/settings", runtime_settings, methods=["GET", "PUT"]),
        Route("/v1/settings/traces", settings_traces, methods=["GET"]),
        Route("/v1/settings/evidence-turns", settings_evidence_turns, methods=["GET"]),
        Route("/v1/settings/history", settings_history, methods=["GET"]),
        Route("/v1/settings/answer-style", settings_answer_style, methods=["GET", "PUT"]),
        Route("/v1/settings/preview", settings_preview, methods=["POST"]),
        Route("/v1/ready", ready, methods=["GET"]),
        Route("/v1/sessions", create_session, methods=["POST"]),
        Route("/v1/sessions/{session_id:str}/messages", message, methods=["POST"]),
        Route("/v1/sessions/{session_id:str}", delete_session, methods=["DELETE"]),
        Route("/v1/sessions/{session_id:str}/verdict", session_verdict, methods=["POST"]),
        Route("/v1/sessions/{session_id:str}/datasets/{dataset_id:str}/rows", dataset_rows, methods=["GET"]),
        Route("/v1/sessions/{session_id:str}/datasets/{dataset_id:str}/meta", dataset_meta, methods=["GET"]),
        Route("/v1/sessions/{session_id:str}/catalog/tables", catalog_tables, methods=["GET"]),
        Route("/v1/sessions/{session_id:str}/catalog/tables/{table:str}/columns", catalog_columns, methods=["GET"]),
        Route("/v1/memory/recipes", memory_recipes, methods=["GET"]),
        Route("/v1/memory/recipes/{recipe_id:int}", memory_delete_recipe, methods=["DELETE"]),
        Route("/v1/memory/terms", memory_terms, methods=["GET", "POST"]),
        Route("/v1/memory/terms/{term_id:int}", memory_delete_term, methods=["DELETE"]),
    ]
    if settings.debug_log_buffer:
        # 임시 디버그(PoC) — 이 블록과 datalens/api/debug_log_buffer.py가 제거 단위다.
        from datalens.api.debug_log_buffer import endpoint as debug_logs_endpoint, install as install_log_buffer

        routes.append(Route("/v1/debug/logs", debug_logs_endpoint(install_log_buffer(settings.debug_log_buffer_size)), methods=["GET"]))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(ApiKeyMiddleware, api_key=settings.api_key.get_secret_value())
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(CorsMiddleware, origins=settings.allowed_cors_origins)
    return app
