"""근거 없는 수치 되묻기 — 조회 모드 · 도구 0회 · 맥락·이전 답에 없는 수치 → 안내를 붙여 1회 재생성 (2026-11-09 · graphio 5·18번).

이어진 대화에서 모델이 도구를 한 번도 부르지 않고 2,358/1,460 · 1,800/1,632 를 지어냈다(21~28초). 모델 없이 — 가짜 프로바이더와
QueryForge. 첫 답은 대화에 넣지 않고, 되묻기는 한 턴에 한 번이다.
"""
from __future__ import annotations

import time
from collections import deque

import pytest

from datalens.application.agent import BoundedAgent, _UNBACKED_RETRY, _unbacked_numbers
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.ports.llm import AssistantTurn, ToolCall
from datalens.ports.queryforge import QueryForgeResult, QueryForgeToolDefinition

QUERY_TOOL = QueryForgeToolDefinition("query", "query", {"type": "object", "additionalProperties": True})
GOOD = {"source": {"table": "T"}, "select": [{"column": "ENB_ID"}],
        "partition_scope": {"kind": "time_range", "column": "EVENT_TIME", "from": "a", "to": "b"}}
FAKE = "試行数 2,358件、失敗数 1,460件"


def assistant(t):
    return AssistantTurn(t, (), "stop")


def calling(args=GOOD):
    return AssistantTurn("", (ToolCall("c", "query", args),), "tool_calls")


class Provider:
    def __init__(self, turns):
        self.turns, self.messages, self.tools = deque(turns), [], []

    async def complete(self, messages, tools, deadline, output_policy):
        self.messages.append(list(messages))
        self.tools.append(tuple(tools))
        value = self.turns.popleft()
        if isinstance(value, Exception):
            raise value
        return value

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


def _ok(qf="Q" * 22):
    return QueryForgeResult(True, qf, {"ok": True, "session_id": qf, "dataset_id": "ds_000000001", "row_count": 1,
                                       "columns": [{"name": "ATT_CNT", "type": "integer"}],
                                       "preview": [{"ATT_CNT": 2092}], "warnings": []})


async def _run(provider, qf, message="SGW-10 試行数は？", mode="agent", locale="ja"):
    agent = BoundedAgent(provider, qf, max_tool_calls=4, recovery_budget=2)
    sess = InMemorySessionStore(60).create(locale=locale)
    return await agent.run(sess, message, time.monotonic() + 5, mode=mode)


# ── 되묻는다 ─────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_fabricated_numbers_without_a_tool_call_are_asked_once_more_with_the_notice():
    p = Provider([assistant(FAKE), calling(), assistant("2,092件でした")])
    qf = QF([_ok()])
    result = await _run(p, qf)
    assert result.answer == "2,092件でした" and len(qf.calls) == 1 and len(p.messages) == 3
    notice = p.messages[1][-1]
    assert notice.role == "user" and notice.content.startswith("[DataLens 案内]")
    assert "2,358" in notice.content and "query ツールを呼んで" in notice.content
    assert "0 ではなく「確認できなかった」" in notice.content


@pytest.mark.anyio
async def test_the_first_answer_is_not_kept_in_the_conversation():
    p = Provider([assistant(FAKE), calling(), assistant("答")])
    await _run(p, QF([_ok()]))
    assert all(FAKE not in (m.content or "") for m in p.messages[1] + p.messages[2])


@pytest.mark.anyio
async def test_warning_code_and_trace_record_the_retry():
    result = await _run(Provider([assistant(FAKE), calling(), assistant("答")]), QF([_ok()]))
    w = [x for x in result.warnings if isinstance(x, dict) and x.get("code") == "DATALENS_UNBACKED_NUMBERS_RETRY"]
    assert len(w) == 1 and {"2,358", "1,460"} <= set(w[0]["values"])
    t = result.trace["unbacked_retry"]
    assert t["step"] == 1 and "2,358" in t["values"] and t["first_answer_head"].startswith("試行数")


@pytest.mark.anyio
async def test_ko_locale_uses_the_korean_notice():
    p = Provider([assistant("시도 2,358건"), calling(), assistant("답")])
    await _run(p, QF([_ok()]), locale="ko")
    assert p.messages[1][-1].content.startswith("[DataLens 안내]")


@pytest.mark.anyio
async def test_second_answer_with_numbers_and_no_tool_goes_out_as_is_with_no_third_try():
    p = Provider([assistant(FAKE), assistant("やはり 2,358件")])
    result = await _run(p, QF())
    assert result.answer == "やはり 2,358件" and len(p.messages) == 2


# ── 되묻지 않는다 ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_numbers_that_the_user_message_gave_are_not_fabricated():
    p = Provider([assistant("影響は 665人 です")])
    result = await _run(p, QF(), message="[맥락] 영향 가입자 665명\n影響人数は？")
    assert result.answer == "影響は 665人 です" and len(p.messages) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("answer", ["2026-11-09 の件です", "手順は 1 と 2 と 3 です", "原因は MME 過負荷です"])
async def test_dates_small_ints_and_number_free_answers_are_not_asked_again(answer):
    p = Provider([assistant(answer)])
    await _run(p, QF())
    assert len(p.messages) == 1


@pytest.mark.anyio
async def test_a_turn_that_called_a_tool_is_not_asked_again():
    p = Provider([calling(), assistant("試行数は 2,092件")])
    result = await _run(p, QF([_ok()]))
    assert len(p.messages) == 2 and not any(
        isinstance(x, dict) and x.get("code") == "DATALENS_UNBACKED_NUMBERS_RETRY" for x in result.warnings)


@pytest.mark.anyio
async def test_evidence_mode_is_never_asked_again():
    p = Provider([assistant(FAKE)])
    await _run(p, QF(), mode="evidence")
    assert len(p.messages) == 1


def test_helper_returns_only_unbacked_numbers_and_survives_garbage():
    got = _unbacked_numbers("試行数 2,358件、失敗数 1,460件、影響 665人、3件", set(), ["影響 665人"])
    assert got == ["2,358", "1,460"]
    assert _unbacked_numbers("", set(), []) == []
    assert set(_UNBACKED_RETRY) == {"ko", "ja"}


@pytest.mark.anyio
async def test_tool_free_final_step_after_two_empty_generations_is_asked_once_with_tools_restored():
    """빈 출력 두 번 뒤 도구를 뺀 답도 query 0회라 대상이다(2026-11-12) — 도구를 되돌려 한 번 다시 묻는다."""
    from datalens.ports.llm import LLMEmptyResponse
    p = Provider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), assistant(FAKE), calling(), assistant("2,092件でした")])
    qf = QF([_ok()])
    result = await _run(p, qf)
    assert result.answer == "2,092件でした" and len(qf.calls) == 1 and len(p.messages) == 5
    assert p.tools[2] == () and p.tools[3] != ()                              # 지어낸 답을 낸 요청은 도구 없음, 되묻는 요청은 도구가 돌아옴
    assert result.trace["unbacked_retry"]["tools_restored"] is True


# ── query 0회 기준(schema 만 부른 답) · 두 되묻기는 합쳐 한 번 (2026-11-12 · graphio 11번 재질의) ─────────────

SCHEMA_TOOL = QueryForgeToolDefinition("schema", "schema", {"type": "object", "additionalProperties": True})
SCHEMA_CALL = {"action": "describe", "table": "T"}
GIVEUP = "照会結果が取得できていないため、確認することができませんでした。"


def schema_calling():
    return AssistantTurn("", (ToolCall("s", "schema", SCHEMA_CALL),), "tool_calls")


class QFS(QF):
    """query 와 schema 를 다 내는 가짜 — schema 결과는 데이터 값이 없다."""

    async def discover_tools(self, timeout_seconds=None):
        return (QUERY_TOOL, SCHEMA_TOOL)

    async def call_tool(self, name, arguments, *, application_session_id, timeout_seconds):
        self.calls.append((name, arguments))
        if name == "schema":
            return QueryForgeResult(True, "S" * 22, {"ok": True, "tables": [{"name": "T", "columns": ["ENB_ID"]}]})
        return self.results.popleft()


def _w(result, code):
    return [x for x in result.warnings if isinstance(x, dict) and x.get("code") == code]


@pytest.mark.anyio
async def test_schema_only_turn_with_fabricated_numbers_is_asked_once_more():
    """schema 1회는 도구 1회지만 query 0회다 — 전에는 `tool_count` 1 이라 빠져나갔다."""
    p = Provider([schema_calling(), assistant(FAKE), calling(), assistant("2,092件でした")])
    qf = QFS([_ok()])
    result = await _run(p, qf)
    assert result.answer == "2,092件でした" and [c[0] for c in qf.calls] == ["schema", "query"]
    assert len(_w(result, "DATALENS_UNBACKED_NUMBERS_RETRY")) == 1 and {"2,358", "1,460"} <= set(result.trace["unbacked_retry"]["values"])
    assert "tools_restored" not in result.trace["unbacked_retry"]            # 도구는 빠진 적이 없다


@pytest.mark.anyio
async def test_giveup_with_numbers_runs_only_the_giveup_retry():
    """"확인하지 못했다"로 끝낸 답에 수치가 섞여도 수치 되묻기는 건너뛰고 기존 포기 되묻기만 돈다."""
    p = Provider([assistant(GIVEUP + " 参考: 2,358件"), calling(), assistant("2,092件でした")])
    result = await _run(p, QF([_ok()]))
    assert result.answer == "2,092件でした" and len(p.messages) == 3
    assert len(_w(result, "DATALENS_NO_QUERY_RETRY")) == 1 and not _w(result, "DATALENS_UNBACKED_NUMBERS_RETRY")
    assert "unbacked_retry" not in result.trace


@pytest.mark.anyio
async def test_no_third_call_after_the_retry_even_if_the_second_answer_gives_up_or_fabricates():
    p = Provider([assistant(FAKE), assistant(GIVEUP)])                         # 수치 되묻기 뒤 두 번째 답이 포기 문구
    result = await _run(p, QF())
    assert result.answer == GIVEUP and len(p.messages) == 2
    assert not _w(result, "DATALENS_NO_QUERY_RETRY")                          # 두 되묻기를 합쳐 한 번
    p2 = Provider([assistant(GIVEUP), assistant(FAKE)])                        # 포기 되묻기 뒤 두 번째 답이 지어낸 수치
    result2 = await _run(p2, QF())
    assert result2.answer == FAKE and len(p2.messages) == 2
    assert not _w(result2, "DATALENS_UNBACKED_NUMBERS_RETRY")


@pytest.mark.anyio
async def test_answer_using_only_the_context_total_line_is_not_asked_again():
    p = Provider([assistant("SGW-10 の試行は 2,358件、失敗は 1,460件です")])
    result = await _run(p, QF(), message="[서버 합계] SGW-10 시도 2,358건 실패 1,460건\nSGW-10 の試行数は？")
    assert len(p.messages) == 1 and not _w(result, "DATALENS_UNBACKED_NUMBERS_RETRY")
