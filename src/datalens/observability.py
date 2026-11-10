from __future__ import annotations

import json
import logging
import re
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


# 트레이스백은 키가 붙은 구조가 아니라 문자열이라 `safe_log_value` 가 닿지 않는다. 같은 키(`_SECRET_KEYS`)를
# 글자로 찾는다 — `key=값` · `key: 값` · `"key": "값"` · `'key': '값'` 꼴. 값은 따옴표 안 전체, 아니면 공백·쉼표·괄호
# 앞까지. `Authorization: Bearer …` 처럼 인증 방식 낱말이 앞에 오면 그 뒤 토큰까지 가린다.
# 키는 `_`·`-` 로 이어진 이름 안에 있어도 걸린다(`GP_MYSQL_PASSWORD`·`x-api-key`·`apikey`). 낱말 중간은
# 아니다 — `mysql://` 의 `sql` 을 키로 읽으면 주소 전체가 가려진다
_SECRET_TEXT = re.compile(
    r"""(?P<k>["']?(?<![A-Za-z0-9])(?:[\w-]*[_-])?(?:%s)[\w-]*["']?\s*[:=]\s*)"""
    r"""(?P<v>"[^"]*"|'[^']*'|(?:(?:Bearer|Basic|Token)\s+)?[^\s,;)}\]]+)""" % "|".join(k.replace("_", "[_-]?") for k in sorted(_SECRET_KEYS)),
    re.IGNORECASE,
)
# 접속 주소 안의 계정·비밀번호(`scheme://user:pass@host`) — 키 없이 DSN 이 통째로 예외 문구에 실릴 때
_URL_CREDENTIAL = re.compile(r"(?P<s>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")


def safe_log_text(text: str) -> str:
    """문자열 속 비밀을 `***` 로 — 트레이스백처럼 구조가 없는 글에 `_SECRET_KEYS` 규칙을 건다."""
    def _mask(m: re.Match[str]) -> str:
        v = m.group("v")
        q = v[0] if v[:1] in "\"'" and len(v) >= 2 else ""
        return f"{m.group('k')}{q}***{q}"
    return _URL_CREDENTIAL.sub(r"\g<s>***@", _SECRET_TEXT.sub(_mask, text))


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
        # `exc_info` 는 표준 속성이라 위에서 걸러진다 — 따로 싣지 않으면 `exc_info=True` 를 줘도 트레이스백이
        # 남지 않는다(2026-11-10 graphio 4번 F-260 500 이 원인 없이 한 줄로만 남았다). 예외 문구에 접속 정보·키가
        # 섞일 수 있어 비밀 가리기를 거친다(비밀은 `.env` 밖으로 내보내지 않는다)
        if record.exc_info:
            payload["exc"] = safe_log_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def configure_logging(level: int | str = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
