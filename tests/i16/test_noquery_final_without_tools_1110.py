"""빈 출력 두 번 뒤 도구 없는 답이 '조회 없이 포기'일 때의 되묻기 — 2026-11-10 (graphio 16번 · 맥스 수정 고정).

`llm_final_without_tools` 경로(`final_without_tools`)로 끝난 포기 답도 한 번 되묻고, 그 요청에 도구를 되돌린다(`tools_restored`).
"""
from __future__ import annotations

import time
from collections import deque

import pytest

from datalens.application.agent import BoundedAgent, _GAVE_UP_PHRASES, _gave_up_without_query
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.ports.llm import AssistantTurn, LLMEmptyResponse, ToolCall
from datalens.ports.queryforge import QueryForgeResult, QueryForgeToolDefinition

QUERY_TOOL = QueryForgeToolDefinition("query", "query", {"type": "object", "additionalProperties": True})
GOOD = {"source": {"table": "T"}, "select": [{"column": "ENB_ID"}],
        "partition_scope": {"kind": "time_range", "column": "EVENT_TIME", "from": "a", "to": "b"}}
GIVEUP = "ツールによる照会結果が取得できていないため、確認することができませんでした。"


def assistant(t):
    return AssistantTurn(t, (), "stop")


def calling(a=GOOD):
    return AssistantTurn("", (ToolCall("c", "query", a),), "tool_calls")


class Provider:
    def __init__(self, turns):
        self.turns, self.messages, self.tools = deque(turns), [], []

    async def complete(self, messages, tools, deadline, output_policy):
        self.messages.append(list(messages))
        self.tools.append(tuple(tools))
        v = self.turns.popleft()
        if isinstance(v, Exception):
            raise v
        return v

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
                                             "preview": [{"ATT_CNT": 48}], "warnings": []})


async def _run(provider, qf, mode="agent", locale="ja"):
    agent = BoundedAgent(provider, qf, max_tool_calls=4, recovery_budget=2)
    sess = InMemorySessionStore(60).create(locale=locale)
    return await agent.run(sess, "セル143010-0の試行数は？", time.monotonic() + 5, mode=mode)


def _w(result, code):
    return [x for x in result.warnings if isinstance(x, dict) and x.get("code") == code]


E1, E2 = LLMEmptyResponse("a"), LLMEmptyResponse("b")


@pytest.mark.anyio
async def test_giveup_after_two_empty_outputs_is_asked_once_with_tools_restored():
    p = Provider([E1, E2, assistant(GIVEUP), calling(), assistant("48件でした")])
    qf = QF([_ok()])
    result = await _run(p, qf)
    assert result.answer == "48件でした" and len(qf.calls) == 1
    assert p.tools[2] == () and p.tools[3] != ()                              # 포기 답을 낸 요청은 도구 없음, 되묻는 요청은 도구가 돌아옴
    assert result.trace["no_query_retry"]["tools_restored"] is True
    assert len(_w(result, "DATALENS_NO_QUERY_RETRY")) == 1 and len(_w(result, "DATALENS_FINAL_WITHOUT_TOOLS")) == 1


@pytest.mark.anyio
async def test_a_plain_giveup_has_no_tools_restored_flag():
    p = Provider([assistant("条件に合致するデータを確認できませんでした"), calling(), assistant("48件")])
    result = await _run(p, QF([_ok()]))
    assert "tools_restored" not in result.trace["no_query_retry"] and result.answer == "48件"


@pytest.mark.anyio
async def test_empty_again_after_restore_goes_tool_free_once_more_and_then_fails():
    """도구를 되돌린 뒤 또 비면 다시 도구 없는 답으로 간다(추가 호출 최대 2번). 거기서도 비면 실패, 세 번째 되묻기는 없다."""
    from datalens.application.agent import AgentInvalidUpstreamResponse
    p = Provider([E1, E2, assistant(GIVEUP), E1, assistant(GIVEUP)])
    result = await _run(p, QF())
    assert result.answer == GIVEUP and len(p.messages) == 5 and len(_w(result, "DATALENS_NO_QUERY_RETRY")) == 1
    p2 = Provider([E1, E2, assistant(GIVEUP), E1, E2])
    with pytest.raises(AgentInvalidUpstreamResponse):
        await _run(p2, QF())
    assert len(p2.messages) == 5


@pytest.mark.anyio
async def test_giveup_again_after_restore_without_a_query_goes_out_as_is():
    p = Provider([E1, E2, assistant(GIVEUP), assistant("やはり確認できませんでした")])
    result = await _run(p, QF())
    assert result.answer == "やはり確認できませんでした" and len(p.messages) == 4
    assert len(_w(result, "DATALENS_NO_QUERY_RETRY")) == 1


@pytest.mark.anyio
async def test_non_giveup_tool_free_answer_is_not_asked():
    p = Provider([E1, E2, assistant("48件でした")])
    result = await _run(p, QF())
    assert result.answer == "48件でした" and len(p.messages) == 3 and not _w(result, "DATALENS_NO_QUERY_RETRY")


@pytest.mark.anyio
async def test_a_query_already_made_means_no_ask_even_on_the_tool_free_path():
    p = Provider([calling(), E1, E2, assistant(GIVEUP)])
    result = await _run(p, QF([_ok()]))
    assert not _w(result, "DATALENS_NO_QUERY_RETRY") and len(p.messages) == 4


@pytest.mark.anyio
async def test_evidence_mode_is_never_asked_even_after_empty_outputs():
    p = Provider([E1, E2, assistant(GIVEUP)])
    result = await _run(p, QF(), mode="evidence")
    assert not _w(result, "DATALENS_NO_QUERY_RETRY") and len(p.messages) == 3


@pytest.mark.parametrize("text", ["ツールによる照会結果が取得できていない", "データが取得できていません", "照会結果が取得されず", "照会結果が取得できなかった"])
def test_new_giveup_phrases_are_caught(text):
    assert _gave_up_without_query(text)


def test_new_phrases_are_in_the_list():
    for p in ("取得できていない", "取得できていません", "照会結果が取得"):
        assert p in _GAVE_UP_PHRASES


def test_normal_answers_that_mention_results_are_not_giveups():
    assert not _gave_up_without_query("照会結果は48件でした")
