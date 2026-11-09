"""query 인자 정규화(column 채움 · select 정리)와 조회 없는 "확인 못 함" 되묻기 — 2026-11-10 (graphio 16·25번, 맥스 수정 고정).

모델 없이 — `normalize` 직접 호출, 되묻기는 가짜 프로바이더와 QueryForge(test_unbacked_retry_1109 와 같은 틀).
"""
from __future__ import annotations

import time
from collections import deque
from copy import deepcopy

import pytest

from datalens.application import query_args
from datalens.application.agent import BoundedAgent, _GAVE_UP_PHRASES, _NO_QUERY_RETRY, _gave_up_without_query
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.ports.llm import AssistantTurn, ToolCall
from datalens.ports.queryforge import QueryForgeResult, QueryForgeToolDefinition

T = "PM_COR_LTE_CALL_1M"


def args(scope=None, **kw):
    base = {"source": {"table": T}, "aggregations": [{"function": "sum", "column": "ATTEMPT", "alias": "A"}],
            "partition_scope": {"kind": "time_range", "from": "2026-11-10 05:30:00", "to": "2026-11-10 05:35:00"}}
    if scope is not None:
        base["partition_scope"] = scope
    base.update(kw)
    return base


# ── column 채움 ──────────────────────────────────────────────────────────────

def test_single_partition_key_fills_the_missing_column_and_notes_it():
    out, notes = query_args.normalize(args(), {T: ["EVENT_TIME"]})
    assert out["partition_scope"]["column"] == "EVENT_TIME"
    assert any("partition_scope/column" in n and "EVENT_TIME" in n and T in n for n in notes)


def test_several_keys_or_unknown_table_or_no_keys_leave_it_alone():
    for keys in ({T: ["EVENT_TIME", "DAY"]}, {"OTHER": ["EVENT_TIME"]}, {}, None, {T: []}, {T: "EVENT_TIME"}):
        out, notes = query_args.normalize(args(), keys)
        assert "column" not in out["partition_scope"] and not notes, keys


def test_existing_column_is_not_overwritten_and_other_kinds_are_not_touched():
    a = args(scope={"kind": "time_range", "column": "REG_TIME", "from": "a", "to": "b"})
    assert query_args.normalize(a, {T: ["EVENT_TIME"]})[0]["partition_scope"]["column"] == "REG_TIME"
    b = args(scope={"kind": "latest"})
    out, notes = query_args.normalize(b, {T: ["EVENT_TIME"]})
    assert out["partition_scope"] == {"kind": "latest"} and not notes


def test_string_source_or_missing_scope_does_not_crash():
    for a in ({"source": T, "partition_scope": {"kind": "time_range"}}, {"source": {"table": T}}, {}):
        query_args.normalize(a, {T: ["EVENT_TIME"]})


def test_original_arguments_are_untouched():
    a = args()
    before = deepcopy(a)
    query_args.normalize(a, {T: ["EVENT_TIME"]})
    assert a == before


# ── select 정리 ──────────────────────────────────────────────────────────────

def _sel(*cols):
    return [{"column": c} for c in cols]


def _eq(col, val):
    return {"all": [{"column": col, "operator": "eq", "value": val}]}


def test_eq_pinned_select_column_is_dropped_with_aggregation():
    out, notes = query_args.normalize(args(select=_sel("SGW_ID"), filters=_eq("SGW_ID", "5")), None)
    assert "select" not in out and any("SGW_ID" in n and "select" in n for n in notes)


def test_unpinned_and_non_eq_columns_stay():
    out, _ = query_args.normalize(args(select=_sel("SGW_ID", "EVENT_TIME"), filters=_eq("SGW_ID", "5")), None)
    assert out["select"] == _sel("EVENT_TIME")
    f = {"all": [{"column": "SGW_ID", "operator": "gte", "value": "5"}]}
    out, notes = query_args.normalize(args(select=_sel("SGW_ID"), filters=f), None)
    assert out["select"] == _sel("SGW_ID") and not notes


def test_grouped_column_stays_even_if_pinned():
    out, _ = query_args.normalize(args(select=_sel("SGW_ID"), group_by=["SGW_ID"], filters=_eq("SGW_ID", "5")), None)
    assert out["select"] == _sel("SGW_ID")


def test_without_aggregations_select_is_not_touched():
    a = args(select=_sel("SGW_ID"), filters=_eq("SGW_ID", "5"))
    a.pop("aggregations")
    assert query_args.normalize(a, None)[0]["select"] == _sel("SGW_ID")


def test_garbage_select_items_and_filters_survive():
    for sel in (["SGW_ID"], [None], [{"alias": "x"}], "SGW_ID"):
        query_args.normalize(args(select=sel, filters=_eq("SGW_ID", "5")), None)
    query_args.normalize(args(select=_sel("X"), filters="bad"), None)


def test_column_fill_and_select_drop_happen_together():
    out, notes = query_args.normalize(args(select=_sel("SGW_ID"), filters=_eq("SGW_ID", "5")), {T: ["EVENT_TIME"]})
    assert out["partition_scope"]["column"] == "EVENT_TIME" and "select" not in out and len(notes) == 2


# ── 포기 표현 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "条件に合致するデータを確認できませんでした", "試行数は確認することができません", "照会できません", "取得できません",
    "조건에 맞는 데이터를 확인하지 못했다", "확인할 수 없다"])
def test_gave_up_phrases_are_detected(text):
    assert _gave_up_without_query(text)


@pytest.mark.parametrize("text", ["2026-11-10 05:30〜05:35の試行数は48件でした", "試行数は 0件（D4 無呼判定）です", "48건이다", ""])
def test_normal_answers_are_not_gave_up(text):
    assert not _gave_up_without_query(text)


def test_both_locales_have_the_notice():
    assert set(_NO_QUERY_RETRY) == {"ko", "ja"} and _GAVE_UP_PHRASES


# ── 되묻기 ───────────────────────────────────────────────────────────────────

QUERY_TOOL = QueryForgeToolDefinition("query", "query", {"type": "object", "additionalProperties": True})
GOOD = {"source": {"table": "T"}, "select": [{"column": "ENB_ID"}],
        "partition_scope": {"kind": "time_range", "column": "EVENT_TIME", "from": "a", "to": "b"}}
GIVEUP = "条件に合致するデータを確認できませんでした。"


def assistant(t):
    return AssistantTurn(t, (), "stop")


def calling(a=GOOD):
    return AssistantTurn("", (ToolCall("c", "query", a),), "tool_calls")


class Provider:
    def __init__(self, turns):
        self.turns, self.messages = deque(turns), []

    async def complete(self, messages, tools, deadline, output_policy):
        self.messages.append(list(messages))
        return self.turns.popleft()

    async def close(self):
        return None


class QF:
    def __init__(self, results=()):
        self.results, self.calls = deque(results), []

    async def discover_tools(self, timeout_seconds=None):
        return (QUERY_TOOL,)

    async def call_tool(self, name, arguments, *, application_session_id, timeout_seconds):
        self.calls.append((name, arguments))
        return self.results.popleft()


def _ok():
    return QueryForgeResult(True, "Q" * 22, {"ok": True, "session_id": "Q" * 22, "dataset_id": "ds_000000001", "row_count": 1,
                                             "columns": [{"name": "ATT_CNT", "type": "integer"}],
                                             "preview": [{"ATT_CNT": 39}], "warnings": []})


async def _run(provider, qf, mode="agent", locale="ja"):
    agent = BoundedAgent(provider, qf, max_tool_calls=4, recovery_budget=2)
    sess = InMemorySessionStore(60).create(locale=locale)
    return await agent.run(sess, "セル181234-4の試行数は？", time.monotonic() + 5, mode=mode)


def _warn(result):
    return [x for x in result.warnings if isinstance(x, dict) and x.get("code") == "DATALENS_NO_QUERY_RETRY"]


@pytest.mark.anyio
async def test_giving_up_without_a_query_is_asked_once_and_the_second_answer_is_used():
    p = Provider([assistant(GIVEUP), calling(), assistant("39件でした")])
    qf = QF([_ok()])
    result = await _run(p, qf)
    assert result.answer == "39件でした" and len(qf.calls) == 1 and len(p.messages) == 3
    assert p.messages[1][-1].content == _NO_QUERY_RETRY["ja"]
    assert len(_warn(result)) == 1 and result.trace["no_query_retry"]["first_answer_head"].startswith("条件に")


@pytest.mark.anyio
async def test_the_first_giveup_answer_is_not_kept_in_the_conversation():
    p = Provider([assistant(GIVEUP), calling(), assistant("39件")])
    await _run(p, QF([_ok()]))
    assert not any(GIVEUP in (m.content or "") for m in p.messages[2] if m.role == "assistant")


@pytest.mark.anyio
async def test_second_giveup_without_a_query_goes_out_as_is_with_no_third_try():
    p = Provider([assistant(GIVEUP), assistant("やはり確認できませんでした")])
    result = await _run(p, QF())
    assert result.answer == "やはり確認できませんでした" and len(p.messages) == 2 and len(_warn(result)) == 1


@pytest.mark.anyio
async def test_a_turn_with_a_query_is_not_asked_again_even_if_it_gives_up_after():
    p = Provider([calling(), assistant("条件に一致する行がなく確認できませんでした")])
    result = await _run(p, QF([_ok()]))
    assert len(p.messages) == 2 and not _warn(result)


@pytest.mark.anyio
async def test_evidence_mode_is_never_asked():
    p = Provider([assistant(GIVEUP)])
    await _run(p, QF(), mode="evidence")
    assert len(p.messages) == 1


@pytest.mark.anyio
async def test_normal_answer_without_a_query_is_not_asked():
    p = Provider([assistant("ご質問の範囲では 0件（D4 無呼判定）です")])
    result = await _run(p, QF())
    assert len(p.messages) == 1 and not _warn(result)


@pytest.mark.anyio
async def test_ko_locale_uses_the_korean_notice():
    p = Provider([assistant("데이터를 확인하지 못했다"), calling(), assistant("39건")])
    await _run(p, QF([_ok()]), locale="ko")
    assert p.messages[1][-1].content == _NO_QUERY_RETRY["ko"]
