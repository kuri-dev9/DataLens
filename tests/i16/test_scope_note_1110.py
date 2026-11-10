"""조회 결과 「범위 표시」 `scope_note` — `_incident_scope` · `_scope_note` · agent 흐름 (2026-11-10 · 맥스 구현 고정).

graphio 2번 F-253 L3 — 모델이 사건 셀(59개)로 거르지 않고 전 지역 합계를 내어 「影響セル 1,866個」라고 답했다.
사건 맥락(「조회 범위(이 판정 단위): …」 줄)이 있는 질문의 조회가 사건 범위로 걸러지지 않으면 결과 옆에 한 줄 적는다.
값은 그대로 둔다(전체 값이 필요한 질문도 있다). 가짜 프로바이더·QueryForge — 모델 없음.
"""
from __future__ import annotations

import json
import time
from collections import deque

import pytest

from datalens.application import agent as agent_mod
from datalens.application.agent import BoundedAgent, _incident_scope, _scope_note
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.ports.llm import AssistantTurn, TokenUsage, ToolCall
from datalens.ports.queryforge import QueryForgeResult, QueryForgeToolDefinition

WIN = "시각 창 2026-11-10T16:35:00 ~ 2026-11-10T16:44:00(끝 포함)"


def msg(tail, question="影響を受けたセルの数は?"):
    return f"증거팩...\n조회 범위(이 판정 단위): {WIN}{tail}\n[질문] {question}\n"


def cond(col, val, op="eq"):
    return {"column": col, "operator": op, "value": val}


def args(*conds, frm="2026-11-10T16:35:00", to="2026-11-10T16:44:00"):
    return {"source": {"table": "T"}, "filters": {"all": list(conds)} if conds else None,
            "partition_scope": {"kind": "time_range", "column": "EVENT_TIME", "from": frm, "to": to}}


ENB_LIST = msg(" · eNB 123, 456")
ENB_COUNT = msg(" · eNB 45곳(목록이 길어 싣지 않음)")
NODE = msg(" · Core 노드 SGW-8")
NODE_ENB = msg(" · Core 노드 SGW-8 · eNB 123, 456")
WIN_ONLY = msg("")
WHOLE = "filters do not restrict to the incident's cells/eNBs/nodes"
OTHER = "restrict to other cells/eNBs/nodes"


# ── _incident_scope ─────────────────────────────────────────────────────────

def test_scope_reads_window_nodes_and_enb_list():
    s = _incident_scope(NODE_ENB)
    assert s["window"] == ("2026-11-10T16:35:00", "2026-11-10T16:44:00")
    assert s["nodes"] == [("SGW", "8")] and s["enbs"] == ["123", "456"] and s["has_enb"] is True


def test_scope_enb_count_only_gives_none_list_but_has_enb():
    s = _incident_scope(ENB_COUNT)
    assert s["enbs"] is None and s["has_enb"] is True


def test_scope_window_only_and_multiple_nodes_dedup():
    s = _incident_scope(WIN_ONLY)
    assert s["nodes"] == [] and s["enbs"] == [] or s["enbs"] is None
    assert s["has_enb"] is False
    assert _incident_scope(msg(" · Core 노드 SGW-8, MME-2, SGW-8"))["nodes"] == [("SGW", "8"), ("MME", "2")]


def test_scope_none_without_the_line_or_for_none_message():
    assert _incident_scope("그냥 질문") is None
    assert _incident_scope("") is None
    assert _incident_scope(None) is None


def test_scope_uses_the_last_line_when_several():
    text = msg(" · eNB 1, 2") + msg(" · eNB 9, 8")
    assert _incident_scope(text)["enbs"] == ["9", "8"]


# ── _scope_note ─────────────────────────────────────────────────────────────

def test_no_filters_gives_whole_network_note():
    for m in (ENB_LIST, ENB_COUNT, NODE):
        assert _scope_note(m, args()).startswith(WHOLE) and "whole-network value" in _scope_note(m, args())


def test_filter_on_incident_enb_gives_no_note():
    assert _scope_note(ENB_LIST, args(cond("ENB_ID", "123"))) is None
    assert _scope_note(ENB_LIST, args({"column": "ENB_ID", "operator": "in", "values": ["999", "456"]})) is None
    assert _scope_note(ENB_LIST, args(cond("ENB_ID", 123))) is None                     # 정수 값도 문자열로 비교


def test_enb_count_only_any_enb_filter_counts_as_narrowed():
    assert _scope_note(ENB_COUNT, args(cond("ENB_ID", "7"))) is None


def test_filter_on_other_enb_gives_other_note_not_whole():
    n = _scope_note(ENB_LIST, args(cond("ENB_ID", "999")))
    assert OTHER in n and WHOLE not in n


def test_filter_on_non_target_column_is_whole_network():
    n = _scope_note(ENB_LIST, args(cond("CALL_TYPE", 3)))
    assert n.startswith(WHOLE)


def test_node_column_recognised_and_other_node_value_flagged():
    assert _scope_note(NODE, args(cond("SGW_ID", "8"))) is None
    assert _scope_note(NODE, args(cond("NODE_ID", "8"))) is None
    assert OTHER in _scope_note(NODE, args(cond("SGW_ID", "5")))
    assert OTHER in _scope_note(NODE, args(cond("MME_ID", "8")))                       # 노드 종류가 다르다


def test_ne_and_not_in_are_not_filters():
    assert _scope_note(ENB_LIST, args(cond("ENB_ID", "123", "ne"))).startswith(WHOLE)
    assert _scope_note(ENB_LIST, args({"column": "ENB_ID", "operator": "not_in", "values": ["123"]})).startswith(WHOLE)


def test_node_or_enb_filter_each_is_enough_when_both_are_scoped():
    assert _scope_note(NODE_ENB, args(cond("SGW_ID", "8"))) is None
    assert _scope_note(NODE_ENB, args(cond("ENB_ID", "456"))) is None


def test_window_only_scope_has_no_target_note():
    assert _scope_note(WIN_ONLY, args()) is None


def test_question_target_missing_is_appended():
    m = msg(" · Core 노드 SGW-8", question="SGW-8 의 시도 수는?")
    n = _scope_note(m, args(cond("SGW_ID", "5")))
    assert "filters lack the target named in the question: SGW-8" in n and OTHER in n and "; " in n
    assert _scope_note(m, args(cond("SGW_ID", "8"))) is None


def test_window_slack_five_minutes_and_edges():
    ok = lambda frm, to: _scope_note(WIN_ONLY, args(frm=frm, to=to))
    assert ok("2026-11-10T16:30:00", "2026-11-10T16:49:00") is None                    # 정확히 5분 여유
    assert "differs from the incident window" in ok("2026-11-10T16:29:00", "2026-11-10T16:44:00")
    assert "differs" in ok("2026-11-10T16:35:00", "2026-11-10T16:50:00")
    assert "differs" in ok("2026-11-10T10:00:00", "2026-11-10T11:00:00")               # 겹치지 않음
    assert "differs" in ok("2026-11-10T17:00:00", "2026-11-10T17:10:00")


def test_date_only_to_means_end_of_day():
    n = _scope_note(WIN_ONLY, args(frm="2026-11-10T16:35:00", to="2026-11-10"))
    assert "differs" in n                                                               # 그날 23:59 까지 → 사건 창보다 훨씬 길다


@pytest.mark.parametrize("frm,to", [("어제", "오늘"), ("2026-11-10T16:35:00", "x"), ("", ""), (None, None)])
def test_bad_time_strings_do_not_raise(frm, to):
    assert _scope_note(WIN_ONLY, args(frm=frm, to=to)) is None


def test_non_dict_arguments_and_missing_partition_scope():
    assert _scope_note(ENB_LIST, None) is None
    assert _scope_note(ENB_LIST, "x") is None
    a = args(cond("ENB_ID", "123"))
    a.pop("partition_scope")
    assert _scope_note(ENB_LIST, a) is None


def test_no_incident_scope_line_means_no_note_even_with_no_filters():
    assert _scope_note("[질문] 전체 합계는?", args()) is None


TOKYO = "Asia/Tokyo"


def test_offset_times_are_converted_to_local_before_comparing():
    same = lambda frm, to, tz=TOKYO: _scope_note(WIN_ONLY, args(frm=frm, to=to), tz)
    assert same("2026-11-10T16:35:00+09:00", "2026-11-10T16:44:00+09:00") is None          # 같은 창
    assert same("2026-11-10T07:35:00Z", "2026-11-10T07:44:00Z") is None                      # =16:35~16:44 JST
    assert same("2026-11-10T07:35:00+00:00", "2026-11-10T07:44:00+00:00") is None
    assert "differs" in same("2026-11-10T16:35:00Z", "2026-11-10T16:44:00Z")                 # =다음 날 01:35 JST
    assert same("2026-11-10T16:35:00", "2026-11-10T07:44:00Z") is None                       # 오프셋 있음·없음 섞임 → 예외 없이(07:44Z=16:44 JST)


def test_offset_times_without_timezone_argument_do_not_raise():
    _scope_note(WIN_ONLY, args(frm="2026-11-10T16:35:00+09:00", to="2026-11-10T16:44:00+09:00"))  # 프로세스 로컬 기준 — 예외만 없으면 된다
    _scope_note(WIN_ONLY, args(frm="2026-11-10T16:35:00Z", to="2026-11-10T16:44:00Z"))      # 프로세스 로컬 기준 — 예외만 없으면 된다


def test_bad_timezone_skips_only_the_window_note_and_keeps_the_target_note():
    n = _scope_note(ENB_LIST, args(frm="2026-11-10T16:35:00+09:00", to="2026-11-10T16:44:00+09:00"), "Nowhere/X")
    assert n is not None and n.startswith(WHOLE) and "differs" not in n


def test_inner_exception_gives_none_and_does_not_raise(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("터졌다")
    monkeypatch.setattr(agent_mod, "_scope_note_inner", boom)
    with caplog.at_level("WARNING"):
        assert _scope_note(ENB_LIST, args()) is None


def test_note_text_is_generic():
    n = _scope_note(ENB_LIST, args())
    for w in ("PM_", "EVT", "F-2", "SGW-8", "1,866"):
        assert w not in n


# ── agent 흐름 ───────────────────────────────────────────────────────────────

SCHEMA = {"type": "object", "properties": {k: {} for k in ("source", "filters", "partition_scope", "aggregations", "select")},
          "required": []}
TOOLS = tuple(QueryForgeToolDefinition(n, n, SCHEMA) for n in ("query", "schema"))


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
                                                 "columns": [{"name": "n", "type": "integer"}], "preview": [{"n": 1866}], "warnings": []})


def call(name, **a):
    return AssistantTurn("", (ToolCall(call_id="c1", name=name, arguments=a),), "tool_calls")


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def run(turns, message):
    p = P(turns)
    a = BoundedAgent(p, QF(), max_tool_calls=6, recovery_budget=4)
    s = InMemorySessionStore(60).create(locale="ja")
    await a.run(s, message, time.monotonic() + 5, mode="agent")
    return [json.loads(m.content) for m in p.seen[-1] if m.role == "tool"]


def qargs(*conds):
    a = args(*conds)
    a["aggregations"] = [{"function": "count", "alias": "n"}]
    return {k: v for k, v in a.items() if v is not None}


@pytest.mark.anyio
async def test_unfiltered_query_gets_scope_note_but_value_is_kept():
    (m,) = await run([call("query", **qargs()), AssistantTurn("끝", (), "stop")], ENB_LIST)
    assert m["preview"] == [{"n": 1866}] and m["scope_note"].startswith(WHOLE)


@pytest.mark.anyio
async def test_filtered_query_and_schema_have_no_scope_note():
    (m,) = await run([call("query", **qargs(cond("ENB_ID", "123"))), AssistantTurn("끝", (), "stop")], ENB_LIST)
    assert "scope_note" not in m
    (s,) = await run([call("schema", source={"table": "T"}), AssistantTurn("끝", (), "stop")], ENB_LIST)
    assert "scope_note" not in s


@pytest.mark.anyio
async def test_no_incident_context_means_no_note_in_flow():
    (m,) = await run([call("query", **qargs()), AssistantTurn("끝", (), "stop")], "[질문] 전체는?")
    assert "scope_note" not in m


@pytest.mark.anyio
async def test_scope_note_numbers_are_not_collected_as_facts(monkeypatch):
    seen = []
    real = agent_mod.collect_facts
    monkeypatch.setattr(agent_mod, "collect_facts", lambda node, sink: (seen.append(json.dumps(node, ensure_ascii=False, default=str)), real(node, sink))[1])
    a = qargs()
    a["partition_scope"]["from"], a["partition_scope"]["to"] = "2026-11-10T09:12:00", "2026-11-10T10:34:00"
    (m,) = await run([call("query", **a), AssistantTurn("끝", (), "stop")], ENB_LIST)
    assert "09:12" in m["scope_note"]                                                  # 모델은 본다
    assert not any("scope_note" in s for s in seen)                                    # 근거 수집은 안 본다


@pytest.mark.anyio
async def test_tz_aware_window_in_a_real_turn_does_not_break_the_turn():
    # 같은 창(JST 로 환산) — 표시 없이 값이 그대로 나온다(에이전트 시간대 기본값이 Asia/Tokyo 가 아니면 창 표시가 붙을 수 있다)
    a = qargs()
    a["partition_scope"]["from"], a["partition_scope"]["to"] = "2026-11-10T16:35:00+09:00", "2026-11-10T16:44:00+09:00"
    (m,) = await run([call("query", **a), AssistantTurn("끝", (), "stop")], ENB_LIST)
    assert m["preview"] == [{"n": 1866}]


@pytest.mark.anyio
async def test_inner_exception_in_a_real_turn_keeps_the_query_result(monkeypatch):
    monkeypatch.setattr(agent_mod, "_scope_note_inner", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    (m,) = await run([call("query", **qargs()), AssistantTurn("끝", (), "stop")], ENB_LIST)
    assert m["preview"] == [{"n": 1866}] and "scope_note" not in m
