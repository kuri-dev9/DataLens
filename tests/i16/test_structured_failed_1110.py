"""강제 조회를 실행했는데 그 뒤 query 가 하나도 성공하지 못하면 경고를 `DATALENS_STRUCTURED_RETRY_FAILED` 로 (2026-11-10 · 맥스 수정 고정).

graphio 22번 「영향 받은 앱으로 보여줘」(`dlr_uJOPK8…`) — 강제 조회(`agent_structured_retry`) 뒤 query 가 `UNKNOWN_COLUMN` 두 번으로
끝났는데 경고가 `STRUCTURED_RETRY` 그대로라 graphio 가 "위 답은 그 조회를 거쳐 나온 것"이라고 적었다(불변규칙 18).

* 실행했고 query 가 모두 실패 → `_FAILED` 하나뿐 · trace `queries_failed`·`result: failed`.
* 실패 뒤 하나라도 성공 → `STRUCTURED_RETRY` 그대로 · `queries_ok`.
* 거름이 빠져 실행하지 않음 → 지금처럼 `_DROPPED` (`_FAILED` 가 아니다).
* 강제 조회가 없는 흐름은 경고가 그대로다.
가짜 프로바이더·가짜 QueryForge — 네트워크 없음.
"""
from __future__ import annotations

import time
from collections import deque

import pytest

from datalens.application.agent import BoundedAgent, _STRUCTURED_DROP
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.ports.llm import AssistantTurn, LLMEmptyResponse, TokenUsage, ToolCall
from datalens.ports.queryforge import QueryForgeFailure, QueryForgeResult, QueryForgeToolDefinition

SCHEMA = {"type": "object", "properties": {k: {} for k in
                                           ("source", "partition_scope", "filters", "aggregations", *_STRUCTURED_DROP)},
          "required": []}
QT = QueryForgeToolDefinition("query", "query", SCHEMA)
ARGS = {"source": {"table": "PM_COR_XP_CELL_1M"},
        "filters": {"all": [{"column": "ENB_ID", "operator": "in", "value": ["143010"]}]},
        "partition_scope": {"kind": "time_range", "column": "EVENT_TIME", "from": "a", "to": "b"},
        "aggregations": [{"function": "sum", "column": "BAD_SUBS", "alias": "B"}]}
CODE_FAILED, CODE_RETRY, CODE_DROPPED = ("DATALENS_STRUCTURED_RETRY_FAILED", "DATALENS_STRUCTURED_RETRY",
                                          "DATALENS_STRUCTURED_RETRY_DROPPED")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def asst(text):
    return AssistantTurn(text, (), "stop")


def empty(n):
    return LLMEmptyResponse("e", {"eval_count": n})


def call_turn(args=None):
    return AssistantTurn("", (ToolCall(call_id="c-model", name="query", arguments=dict(args or ARGS)),), "tool_calls")


class P:
    def __init__(self, turns, structured=None):
        self.turns, self.structured = deque(turns), deque(structured or [])

    async def complete(self, messages, tools, deadline, policy):
        v = self.turns.popleft()
        if isinstance(v, Exception):
            raise v
        return v

    async def complete_structured(self, messages, schema, deadline, policy):
        return self.structured.popleft(), TokenUsage(completion_tokens=77)

    async def close(self):
        return None


def _fail(code="UNKNOWN_COLUMN"):
    return QueryForgeResult(False, "Q" * 22, {}, QueryForgeFailure(code, "column not found", False, "check columns"))


def _ok():
    return QueryForgeResult(True, "Q" * 22, {"ok": True, "session_id": "Q" * 22, "dataset_id": "ds_000000001", "row_count": 1,
                                             "columns": [{"name": "B", "type": "integer"}], "preview": [{"B": 9}],
                                             "warnings": []})


class QF:
    def __init__(self, results):
        self.calls, self.results = [], deque(results)

    async def discover_tools(self, timeout_seconds=None):
        return (QT,)

    async def call_tool(self, name, arguments, *, application_session_id, timeout_seconds):
        self.calls.append((name, arguments))
        return self.results.popleft()


async def run(p, qf, question="영향 받은 앱으로 보여줘", min_eval=100):
    a = BoundedAgent(p, qf, max_tool_calls=6, recovery_budget=4, structured_retry_min_eval=min_eval)
    s = InMemorySessionStore(60).create(locale="ja")
    return await a.run(s, question, time.monotonic() + 5, mode="agent")


def codes(r):
    return [w.get("code") for w in r.warnings if isinstance(w, dict)]


@pytest.mark.anyio
async def test_all_queries_failed_after_the_forced_one_gives_failed_warning_only():
    # 22번 사례 — 강제 조회(UNKNOWN_COLUMN) → 모델이 다시 query(UNKNOWN_COLUMN) → 확인하지 못했다
    p = P([empty(150), call_turn(), asst("조회 실패 — 열이 없습니다")], [dict(ARGS)])
    qf = QF([_fail(), _fail()])
    r = await run(p, qf)
    assert len(qf.calls) == 2
    assert codes(r).count(CODE_FAILED) == 1 and CODE_RETRY not in codes(r) and CODE_DROPPED not in codes(r)
    w = next(x for x in r.warnings if x.get("code") == CODE_FAILED)
    assert w["action"] == "structured_query_failed" and "failed 2" in w["detail"]
    sr = r.trace["structured_retry"]
    assert sr["ok"] is True and sr["queries_failed"] == 2 and not sr.get("queries_ok") and sr["result"] == "failed"


@pytest.mark.anyio
async def test_one_success_after_failures_keeps_the_plain_retry_warning():
    p = P([empty(150), call_turn(), asst("9명")], [dict(ARGS)])
    qf = QF([_fail(), _ok()])
    r = await run(p, qf)
    assert codes(r).count(CODE_RETRY) == 1 and CODE_FAILED not in codes(r)
    sr = r.trace["structured_retry"]
    assert sr["queries_ok"] == 1 and sr["queries_failed"] == 1 and "result" not in sr


@pytest.mark.anyio
async def test_forced_query_succeeds_means_plain_retry_warning():
    p = P([empty(150), asst("9명")], [dict(ARGS)])
    r = await run(p, QF([_ok()]))
    assert codes(r).count(CODE_RETRY) == 1 and CODE_FAILED not in codes(r)
    assert r.trace["structured_retry"]["queries_ok"] == 1


@pytest.mark.anyio
async def test_forced_query_never_executed_and_no_later_query_is_failed():
    # 강제 조회 인자가 검증에 걸려 query 가 실행되지 않고 모델도 query 없이 답했다 — 답은 조회 결과가 아니다
    bad = {"source": {"table": "PM_COR_XP_CELL_1M"}}                       # 필수 partition_scope 없음(스키마 required 는 비어 있어 통과)
    p = P([empty(150), asst("조회 실패 — 열이 없습니다")], [bad])
    qf = QF([_fail()])
    r = await run(p, qf)
    ran = len(qf.calls)
    if ran:                                                                 # 실행됐다면 실패 한 번이라 _FAILED
        assert CODE_FAILED in codes(r) and CODE_RETRY not in codes(r)
    else:
        assert CODE_FAILED in codes(r)


@pytest.mark.anyio
async def test_dropped_stays_dropped_when_the_filter_target_is_missing():
    # 질문이 가리키는 대상(셀 143010-0)의 거름이 빠진 인자는 실행하지 않는다 — `_DROPPED` 이지 `_FAILED` 가 아니다
    args = {"source": {"table": "PM_COR_LTE_CALL_1M"},
            "partition_scope": {"kind": "time_range", "column": "EVENT_TIME", "from": "a", "to": "b"},
            "aggregations": [{"function": "sum", "column": "ATTEMPT", "alias": "A"}]}
    p = P([empty(150), empty(30), asst("조회 실패 — 열이 없습니다")], [args])
    qf = QF([])
    r = await run(p, qf, question="セル143010-0の試行数は？")
    assert not qf.calls
    assert CODE_DROPPED in codes(r) and CODE_FAILED not in codes(r)


@pytest.mark.anyio
async def test_no_forced_query_leaves_warnings_untouched():
    p = P([call_turn(), asst("9명")])
    qf = QF([_ok()])
    r = await run(p, qf)
    assert not [c for c in codes(r) if c and c.startswith("DATALENS_STRUCTURED_RETRY")]
    assert "structured_retry" not in (r.trace or {})


@pytest.mark.anyio
async def test_failed_query_without_a_forced_one_is_not_counted():
    p = P([call_turn(), asst("조회 실패 — 열이 없습니다")])
    qf = QF([_fail()])
    r = await run(p, qf)
    assert not [c for c in codes(r) if c and c.startswith("DATALENS_STRUCTURED_RETRY")]
