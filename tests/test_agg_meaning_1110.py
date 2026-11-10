"""`group_by` 가 시간 버킷 사전이어도 턴이 500 이 되지 않는다 · 로그에 트레이스백(비밀 가림) (2026-11-10 · graphio 4번 F-260 DataLens 500).

`_aggregation_meaning` 이 `", ".join(group_by)` 로 사전 원소를 이어 붙이다 `TypeError` 가 났고, 조회는 성공했는데 턴 전체가 500 이었다.
고친 것: ① `_group_key`(문자열·사전·버킷) ② 설명 단계가 실패해도 조회 결과·`scope_note` 를 유지(`aggregation_meaning_failed` 경고)
③ `message_handler_failed` 에 `exc_info` ④ `JsonFormatter` 가 `exc` 칸에 트레이스백을 싣되 비밀을 가린다(`safe_log_text`).
가짜 프로바이더·가짜 QueryForge — 네트워크 없음.
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from collections import deque

import pytest

from datalens.api import app as app_mod
from datalens.application import agent as agent_mod
from datalens.application.agent import BoundedAgent, _aggregation_meaning, _group_key
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.observability import JsonFormatter, safe_log_text
from datalens.ports.llm import AssistantTurn, TokenUsage, ToolCall
from datalens.ports.queryforge import QueryForgeResult, QueryForgeToolDefinition

M = _aggregation_meaning
BUCKET = {"alias": "time", "bucket": "minute", "column": "EVENT_TIME"}


def agg(function="sum", column="ATTEMPT", alias="s"):
    return {"function": function, "column": column, "alias": alias}


def group_line(group_by):
    return M({"aggregations": [agg()], "group_by": group_by})[-1]


# ── 1 · 버킷 사전 ────────────────────────────────────────────────────────────

def test_f260_arguments_no_longer_raise_and_name_the_bucket():
    # 4번 F-260 에서 500 을 낸 실제 인자 모양
    args = {"aggregations": [agg("sum", "ATTEMPT", "attempt"), agg("sum", "SUCCESS", "success")], "group_by": [BUCKET]}
    lines = M(args)
    assert lines[-1] == "one value per group of (time = time(EVENT_TIME, minute))"
    assert len(lines) == 3


def test_bucket_without_alias_uses_the_column_name():
    assert group_line([{"bucket": "hour", "column": "EVENT_TIME"}]) == \
        "one value per group of (EVENT_TIME = time(EVENT_TIME, hour))"


def test_dict_without_bucket_is_alias_or_column():
    assert group_line([{"alias": "mme", "column": "MME_ID"}]) == "one value per group of (mme)"
    assert group_line([{"column": "MME_ID"}]) == "one value per group of (MME_ID)"


# ── 2 · 섞임 · 문자열만 · 리스트 아님 ───────────────────────────────────────

def test_mixed_strings_and_dicts_keep_order():
    assert group_line(["NODE_ID", {"column": "MME_ID"}, {"bucket": "hour", "column": "EVENT_TIME"}]) == \
        "one value per group of (NODE_ID, MME_ID, EVENT_TIME = time(EVENT_TIME, hour))"


def test_strings_only_are_as_before():
    assert group_line(["MME_ID", "EVENT_TIME"]) == "one value per group of (MME_ID, EVENT_TIME)"
    assert group_line(["NODE_ID"]) == "one value per group of (NODE_ID)"


def test_non_list_values_are_as_before_or_wrapped():
    assert group_line("MME_ID") == "one value per group of (MME_ID)"
    assert group_line(BUCKET) == "one value per group of (time = time(EVENT_TIME, minute))"      # 한 원소로 본다


@pytest.mark.parametrize("gb", [[None], [123], [{}], [[]], {"bucket": None}, 5])
def test_odd_group_by_never_raises(gb):
    M({"aggregations": [agg()], "group_by": gb})


def test_no_group_by_or_no_aggregation_unchanged():
    assert M({"aggregations": [agg()]}) == ["s = SUM(ATTEMPT) — total of column ATTEMPT"]
    assert M({"group_by": [BUCKET]}) == []


def test_group_key_function():
    assert _group_key("A") == "A"
    assert _group_key(BUCKET) == "time = time(EVENT_TIME, minute)"


# ── 3 · 설명 단계가 예외를 내도 조회 결과는 남는다 ─────────────────────────

SCHEMA = {"type": "object", "properties": {k: {} for k in ("source", "partition_scope", "filters", "aggregations", "select", "group_by")},
          "required": []}
TOOLS = tuple(QueryForgeToolDefinition(n, n, SCHEMA) for n in ("query", "schema"))
SCOPE = {"kind": "time_range", "column": "EVENT_TIME", "from": "2026-11-10T16:35:00", "to": "2026-11-10T16:44:00"}
WIN = "시각 창 2026-11-10T16:35:00 ~ 2026-11-10T16:44:00(끝 포함)"
INCIDENT_Q = f"증거팩...\n조회 범위(이 판정 단위): {WIN} · eNB 123, 456\n[질문] 影響を受けたセルの数は?\n"


class P:
    def __init__(self, turns):
        self.turns, self.seen = deque(turns), []

    async def complete(self, messages, tools, deadline, policy):
        self.seen.append(list(messages))
        return self.turns.popleft()

    async def complete_structured(self, *a):
        return {}, TokenUsage()

    async def close(self):
        return None


class QF:
    async def discover_tools(self, timeout_seconds=None):
        return TOOLS

    async def call_tool(self, name, arguments, *, application_session_id, timeout_seconds):
        return QueryForgeResult(True, "Q" * 22, {"ok": True, "session_id": "Q" * 22, "dataset_id": "ds_000000001", "row_count": 1,
                                                 "columns": [{"name": "n", "type": "integer"}], "preview": [{"n": 7212}], "warnings": []})


def _call(**a):
    return AssistantTurn("", (ToolCall(call_id="c1", name="query", arguments=a),), "tool_calls")


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _run(turns, message):
    p = P(turns)
    a = BoundedAgent(p, QF(), max_tool_calls=6, recovery_budget=4)
    s = InMemorySessionStore(60).create(locale="ja")
    out = await a.run(s, message, time.monotonic() + 5, mode="agent")
    return [json.loads(m.content) for m in p.seen[-1] if m.role == "tool"], out


def _query(**extra):
    return _call(source={"table": "T"}, partition_scope=SCOPE, aggregations=[{"function": "count", "alias": "n"}], **extra)


@pytest.mark.anyio
async def test_bucket_group_by_flows_through_with_the_meaning():
    (m,), _ = await _run([_query(group_by=[BUCKET]), AssistantTurn("끝", (), "stop")], "질문")
    assert m["preview"] == [{"n": 7212}]
    assert m["aggregation_meaning"][-1] == "one value per group of (time = time(EVENT_TIME, minute))"


@pytest.mark.anyio
async def test_meaning_failure_keeps_rows_and_scope_note_and_logs_a_warning(monkeypatch, caplog):
    def boom(arguments):
        raise ValueError("설명 실패")
    monkeypatch.setattr(agent_mod, "_aggregation_meaning", boom)
    with caplog.at_level(logging.WARNING):
        (m,), out = await _run([_query(), AssistantTurn("끝", (), "stop")], INCIDENT_Q)
    assert m["preview"] == [{"n": 7212}], "조회 결과가 버려졌다"
    assert "aggregation_meaning" not in m
    assert m.get("scope_note"), "scope_note 는 그대로 붙어야 한다"
    rec = [r for r in caplog.records if r.getMessage() == "aggregation_meaning_failed"]
    assert rec and rec[0].levelno == logging.WARNING and rec[0].exc_info


@pytest.mark.anyio
async def test_meaning_failure_does_not_fail_the_turn(monkeypatch):
    monkeypatch.setattr(agent_mod, "_aggregation_meaning", lambda a: 1 / 0)
    _, out = await _run([_query(), AssistantTurn("끝", (), "stop")], "질문")
    assert out is not None


# ── 4 · message_handler_failed 의 exc ───────────────────────────────────────

def test_app_logs_the_traceback_for_message_handler_failed():
    src = inspect.getsource(app_mod)
    i = src.index('"message_handler_failed"')
    assert "exc_info=True" in src[i:i + 200]


def _record(exc, **extra):
    try:
        raise exc
    except Exception:  # noqa: BLE001
        log = logging.getLogger("datalens.test_exc")
        rec = log.makeRecord("datalens.test_exc", logging.ERROR, __file__, 1, "message_handler_failed", (), __import__("sys").exc_info(),
                             extra=extra)
    return rec


def test_formatter_puts_the_traceback_in_exc_and_keeps_other_fields():
    rec = _record(TypeError("sequence item 0: expected str instance, dict found"), request_id="dlr_x", error_code="DL_INTERNAL_ERROR")
    p = json.loads(JsonFormatter().format(rec))
    assert p["message"] == "message_handler_failed" and p["request_id"] == "dlr_x" and p["error_code"] == "DL_INTERNAL_ERROR"
    assert "Traceback" in p["exc"] and "TypeError: sequence item 0" in p["exc"] and "test_agg_meaning_1110.py" in p["exc"]


def test_no_exc_key_without_an_exception():
    rec = logging.makeLogRecord({"name": "x", "levelno": 20, "levelname": "INFO", "msg": "ok"})
    assert "exc" not in json.loads(JsonFormatter().format(rec))


# ── 7 · 트레이스백 속 비밀 ───────────────────────────────────────────────────

SECRET = "sk-test123"


@pytest.mark.parametrize("text", [
    f"api_key={SECRET}", f"api_key: {SECRET}", f'"api_key": "{SECRET}"', f"'api_key': '{SECRET}'",
    f"password={SECRET}", f"secret={SECRET}",
    f"Authorization: Bearer {SECRET}", f"authorization=Basic {SECRET}",
    f"GP_MYSQL_PASSWORD={SECRET}", f"GP_LLM_API_KEY={SECRET}", f"x-api-key: {SECRET}", f"apikey={SECRET}",
    f"dsn={SECRET}", f"connection_string={SECRET}", f"confirmation_token={SECRET}",
])
def test_secret_forms_are_masked(text):
    out = safe_log_text(f"RuntimeError: boom {text} tail")
    assert SECRET not in out and "***" in out and "boom" in out and "tail" in out


def test_url_credentials_are_masked_but_host_port_db_stay():
    out = safe_log_text("connect failed mysql://pem:S3cret@db:3306/pemdb6 and http://k:sk-test123@llm:11434/v1")
    assert "S3cret" not in out and "sk-test123" not in out
    assert "mysql://***@db:3306/pemdb6" in out and "http://***@llm:11434/v1" in out


def test_quoted_value_is_masked_whole_including_spaces_and_quote_kept():
    out = safe_log_text('{"password": "two words", "model": "gemma"}')
    assert "two words" not in out and '"password": "***"' in out and '"model": "gemma"' in out


@pytest.mark.parametrize("text", [
    "File \"/app/src/datalens/api/app.py\", line 562, in handler",
    "TypeError: sequence item 0: expected str instance, dict found",
    "mysql://db:3306/pemdb6", "postgresql://db/x",
    "HTTP 500 at /api/chat request_id=dlr_abc123 count=42",
])
def test_non_secret_text_is_unchanged(text):
    assert safe_log_text(text) == text


def test_names_starting_with_a_key_are_over_masked_but_never_leak():
    # 맥스가 알린 범위 — `sqlstate=42000` 은 키(`sql`)로 시작하는 이름이라 가려진다(새는 쪽이 아니다)
    assert safe_log_text("sqlstate=42000") == "sqlstate=***"


def test_formatter_masks_secrets_in_the_traceback_and_nowhere_else_leaks():
    rec = _record(RuntimeError(f"fail api_key={SECRET} at http://k:{SECRET}@llm:11434 password=hunter2"), request_id="dlr_y")
    out = JsonFormatter().format(rec)
    assert SECRET not in out and "hunter2" not in out
    p = json.loads(out)
    assert "api_key=***" in p["exc"] and "http://***@llm:11434" in p["exc"] and p["request_id"] == "dlr_y"
    assert "RuntimeError" in p["exc"]
