"""임시 디버그 도구 — 최근 구조화 로그를 메모리에 보관하고 request_id로 조회한다.

PoC 디버깅 전용. DATALENS_DEBUG_LOG_BUFFER=true일 때만 설치·노출되며,
데모 디버그 콘솔이 각 요청 항목에 서버 로그를 덧붙이는 용도다.
운영 전 제거 대상 — 이 모듈과 build_app의 가드 블록, 데모의 fetchServerLogs를
지우면 흔적 없이 사라진다. 디스크·네트워크 비용은 없다(메모리 deque뿐).
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

_STANDARD = set(logging.makeLogRecord({}).__dict__)
_DEFAULT_LIMIT = 300
_MAX_LIMIT = 1000


class RingBufferLogHandler(logging.Handler):
    """스트림 핸들러가 stdout에 쓰는 것과 같은 내용을 deque에 복사한다."""

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self.records: deque[dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload: dict[str, Any] = {
                "ts": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            for key, value in record.__dict__.items():
                if key not in _STANDARD and key not in {"message", "asctime"}:
                    payload[key] = value
            # JSONResponse가 직렬화하지 못하는 값이 남지 않도록 여기서 평탄화한다.
            self.records.append(json.loads(json.dumps(payload, ensure_ascii=False, default=str)))
        except Exception:
            # 로깅 경로가 앱을 깨뜨리면 안 된다.
            pass


def install(capacity: int) -> RingBufferLogHandler:
    handler = RingBufferLogHandler(capacity)
    logging.getLogger().addHandler(handler)
    return handler


def endpoint(handler: RingBufferLogHandler) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def debug_logs(request: Request) -> JSONResponse:
        request_id = request.query_params.get("request_id")
        try:
            limit = int(request.query_params.get("limit", str(_DEFAULT_LIMIT)))
        except ValueError:
            limit = _DEFAULT_LIMIT
        limit = max(1, min(limit, _MAX_LIMIT))
        records = list(handler.records)
        if request_id:
            records = [item for item in records if item.get("request_id") == request_id]
        records = records[-limit:]
        return JSONResponse({"request_id": request_id, "count": len(records), "logs": records})

    return debug_logs
