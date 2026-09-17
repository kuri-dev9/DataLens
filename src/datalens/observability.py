from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any


_STANDARD = set(logging.makeLogRecord({}).__dict__)
_REQUEST_ID: ContextVar[str | None] = ContextVar("datalens_request_id", default=None)
_VALUE_KEYS = frozenset({"value", "values", "value_from", "value_to", "from", "to", "literal"})
_SECRET_KEYS = frozenset({"password", "secret", "api_key", "authorization", "dsn", "connection_string", "confirmation_token", "sql"})


def bind_request_id(request_id: str) -> Token:
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token) -> None:
    _REQUEST_ID.reset(token)


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


def safe_log_value(value: Any, key: str | None = None) -> Any:
    if key in _VALUE_KEYS or key in _SECRET_KEYS:
        return "***"
    if isinstance(value, dict):
        return {str(name): safe_log_value(nested, str(name).lower()) for name, nested in value.items() if str(name).lower() not in {"preview", "rows"}}
    if isinstance(value, (list, tuple)):
        return [safe_log_value(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and key not in {"message", "asctime"}:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def configure_logging(level: int | str = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
