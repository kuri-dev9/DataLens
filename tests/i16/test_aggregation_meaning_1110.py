"""query 결과에 집계 계산식을 붙인다 — `aggregation_meaning` (2026-11-10 · 맥스 수정 고정).

graphio 23번 「MME-9が原因ですよね?」 — 모델이 `count` 를 별칭 `fail_cnt` 로 받고 행 수 4,257 을
"MME 접속 실패 4,257건"이라 답했다(실제 실패 0). 별칭이 아니라 실제 계산식을 결과 옆에 적는다.

* `_aggregation_meaning` — 함수별 문구 · where · group_by · 별칭 기본값 · 집계 없음.
* agent 흐름 — 집계가 있는 query 결과에만 붙는다(select 만 · 다른 도구는 없음) · where 값 숫자는 근거에 안 섞인다.
가짜 프로바이더·가짜 QueryForge — 네트워크 없음.
"""
from __future__ import annotations

import json
import time
from collections import deque

import pytest

from datalens.application import agent as agent_mod
from datalens.application.agent import BoundedAgent, _aggregation_meaning
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.ports.llm import AssistantTurn, TokenUsage, ToolCall
from datalens.ports.queryforge import QueryForgeResult, QueryForgeToolDefinition

M = _aggregation_meaning


def agg(function, column=None, alias=None, **kw):
    d = {"function": function}
    if column:
        d["column"] = column
    if alias:
        d["alias"] = alias
    d.update(kw)
    return d


# ── 단위 ─────────────────────────────────────────────────────────────────────

def test_count_without_column_says_row_count_not_failure_count():
    (line,) = M({"aggregations": [agg("count", alias="fail_cnt")]})
    assert line.startswith("fail_cnt = COUNT(*)")
    assert "not an event/failure count" in line and "sum the count column" in line


def test_count_with_column_is_non_null_count():
    (line,) = M({"aggregations": [agg("count", "X", "n")]})
    assert line == "n = COUNT(X) — number of rows with non-null X"


def test_count_distinct_sum_mean_phrases():
    lines = M({"aggregations": [agg("count_distinct", "IMSI", "u"), agg("sum", "ATT", "s"), agg("mean", "RT", "m")]})
    assert lines[0] == "u = COUNT(DISTINCT IMSI) — number of distinct IMSI values"
    assert lines[1] == "s = SUM(ATT) — total of column ATT"
    assert lines[2].startswith("m = AVG(RT)") and "not a ratio of sums" in lines[2]


@pytest.mark.parametrize("fn,expr", [("min", "MIN(C)"), ("max", "MAX(C)"), ("stddev", "STDDEV(C)"), ("variance", "VARIANCE(C)")])
def test_other_functions_show_the_expression(fn, expr):
    (line,) = M({"aggregations": [agg(fn, "C", "a")]})
    assert line == f"a = {expr}"


def test_unknown_function_still_gives_expression_not_crash():
    (line,) = M({"aggregations": [agg("median", "C", "a")]})
    assert line == "a = MEDIAN(C)"


def test_where_is_appended():
    (line,) = M({"aggregations": [agg("count_distinct", "IMSI", "u", where={"column": "SUCCESS_FLAG", "operator": "eq", "value": 0})]})
    assert line.endswith("; only rows where SUCCESS_FLAG eq 0")


def test_group_by_adds_final_line():
    lines = M({"aggregations": [agg("sum", "A", "s")], "group_by": ["MME_ID", "EVENT_TIME"]})
    assert lines[-1] == "one value per group of (MME_ID, EVENT_TIME)" and len(lines) == 2
    assert M({"aggregations": [agg("sum", "A", "s")], "group_by": "MME_ID"})[-1] == "one value per group of (MME_ID)"


def test_missing_alias_falls_back_to_function_column():
    assert M({"aggregations": [agg("sum", "A")]})[0].startswith("sum_A = ")
    assert M({"aggregations": [agg("count")]})[0].startswith("count_rows = ")


@pytest.mark.parametrize("args", [{}, {"aggregations": []}, {"aggregations": None}, {"group_by": ["A"]},
                                  {"aggregations": ["bad", {}, {"alias": "x"}]}])
def test_no_aggregation_gives_empty_list(args):
    assert M(args) == []          # group_by 만 있어도 줄이 없다


def test_text_has_no_table_or_event_specifics():
    text = " ".join(M({"aggregations": [agg("count")]}))
    for w in ("MME", "PM_", "EVT", "IX2"):
        assert w not in text


# ── agent 흐름 ───────────────────────────────────────────────────────────────

SCHEMA = {"type": "object", "properties": {k: {} for k in ("source", "partition_scope", "filters", "aggregations", "select")},
          "required": []}
TOOLS = tuple(QueryForgeToolDefinition(n, n, SCHEMA) for n in ("query", "schema"))
SCOPE = {"kind": "time_range", "column": "EVENT_TIME", "from": "a", "to": "b"}


def _res(preview):
    return QueryForgeResult(True, "Q" * 22, {"ok": True, "session_id": "Q" * 22, "dataset_id": "ds_000000001", "row_count": 1,
                                             "columns": [{"name": k, "type": "integer"} for k in preview[0]],
                                             "preview": preview, "warnings": []})


def _call(name, **args):
    return AssistantTurn("", (ToolCall(call_id="c1", name=name, arguments=args),), "tool_calls")


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
    def __init__(self, preview):
        self.preview = preview

    async def discover_tools(self, timeout_seconds=None):
        return TOOLS

    async def call_tool(self, name, arguments, *, application_session_id, timeout_seconds):
        return _res(self.preview)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def run(p, qf, q="질문"):
    a = BoundedAgent(p, qf, max_tool_calls=6, recovery_budget=4)
    s = InMemorySessionStore(60).create(locale="ko")
    return await a.run(s, q, time.monotonic() + 5, mode="agent")


def tool_msgs(p):
    return [json.loads(m.content) for m in p.seen[-1] if m.role == "tool"]


@pytest.mark.anyio
async def test_query_with_aggregation_carries_meaning():
    p = P([_call("query", source={"table": "T"}, partition_scope=SCOPE, aggregations=[agg("count", alias="fail_cnt")]),
           AssistantTurn("끝", (), "stop")])
    await run(p, QF([{"fail_cnt": 4257}]))
    (m,) = tool_msgs(p)
    assert m["preview"] == [{"fail_cnt": 4257}]
    assert m["aggregation_meaning"][0].startswith("fail_cnt = COUNT(*)") and "not an event/failure count" in m["aggregation_meaning"][0]


@pytest.mark.anyio
async def test_query_without_aggregation_has_no_meaning():
    p = P([_call("query", source={"table": "T"}, partition_scope=SCOPE, select=["A"]), AssistantTurn("끝", (), "stop")])
    await run(p, QF([{"A": 1}]))
    (m,) = tool_msgs(p)
    assert "aggregation_meaning" not in m


@pytest.mark.anyio
async def test_other_tools_never_get_meaning_even_with_aggregations_argument():
    p = P([_call("schema", source={"table": "T"}, aggregations=[agg("count")]), AssistantTurn("끝", (), "stop")])
    await run(p, QF([{"A": 1}]))
    (m,) = tool_msgs(p)
    assert "aggregation_meaning" not in m


@pytest.mark.anyio
async def test_where_value_numbers_are_not_collected_as_facts(monkeypatch):
    seen = []
    real = agent_mod.collect_facts

    def spy(node, sink):
        seen.append(json.dumps(node, ensure_ascii=False, default=str))
        return real(node, sink)
    monkeypatch.setattr(agent_mod, "collect_facts", spy)
    where = {"column": "CODE", "operator": "eq", "value": 777123}
    p = P([_call("query", source={"table": "T"}, partition_scope=SCOPE,
                 aggregations=[agg("count_distinct", "IMSI", "u", where=where)]), AssistantTurn("끝", (), "stop")])
    await run(p, QF([{"u": 5}]))
    (m,) = tool_msgs(p)
    assert "777123" in json.dumps(m["aggregation_meaning"])          # 모델은 본다
    assert not any("aggregation_meaning" in s or "777123" in s for s in seen)   # 근거 수집은 안 본다
