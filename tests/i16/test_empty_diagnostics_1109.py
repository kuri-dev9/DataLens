"""빈 응답 진단 — `LLMEmptyResponse.diagnostics` · trace `response.empty_outputs` (2026-11-09 · graphio 1번 F-227).

토큰 145개를 내고도 본문·도구 호출이 0 이었는데 무엇을 냈는지 알 길이 없었다. 프로바이더가 받은 원문·사고 앞 500자와
청크 수 같은 숫자를 예외에 싣고, 에이전트가 trace 에만 쌓는다. API 응답 본문(AgentResult.answer · warnings)에는 싣지 않는다.
모델·QueryForge 없이 — 가짜 프로바이더와 httpx MockTransport.
"""
from __future__ import annotations

import json
import time
from collections import deque

import httpx
import pytest

from datalens.application.agent import BoundedAgent
from datalens.application.chat import ChatApplicationService
from datalens.application.trace import TurnLog
from datalens.infrastructure import ollama as ollama_mod
from datalens.infrastructure.ollama import EMPTY_HEAD_CHARS, OllamaProvider
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.ports.llm import AssistantTurn, LLMEmptyResponse, OutputPolicy


class FakeProvider:
    def __init__(self, turns):
        self.turns = deque(turns)

    async def complete(self, messages, tools, deadline, output_policy):
        value = self.turns.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        return None


class NoQueryForge:
    async def discover_tools(self, timeout_seconds=None):
        return ()


def _run(provider):
    agent = BoundedAgent(provider, NoQueryForge(), max_tool_calls=4, recovery_budget=2)
    return agent.run(InMemorySessionStore(60).create(), "질문", time.monotonic() + 5)


def _stream(lines):
    body = "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n"

    async def handler(request):
        return httpx.Response(200, content=body.encode(), headers={"content-type": "application/x-ndjson"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider("http://ollama.test", "fixture-model", request_timeout_seconds=5, client=client), client


async def _empty_exc(adapter, client):
    try:
        with pytest.raises(LLMEmptyResponse) as info:
            await adapter.complete([], (), time.monotonic() + 2, OutputPolicy())
    finally:
        await client.aclose()
    return info.value


# ── 예외 ─────────────────────────────────────────────────────────────────────

def test_empty_response_with_one_argument_still_works():
    exc = LLMEmptyResponse("a")
    assert str(exc) == "a" and exc.diagnostics == {}
    assert LLMEmptyResponse("a", None).diagnostics == {}
    assert LLMEmptyResponse("a", {"chunks": 1}).diagnostics == {"chunks": 1}


# ── Ollama ───────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_silent_stream_fills_the_numbers_and_empty_heads():
    adapter, client = _stream([{"message": {"role": "assistant", "content": ""}, "done": True,
                                "done_reason": "stop", "eval_count": 145}])
    d = (await _empty_exc(adapter, client)).diagnostics
    assert d["raw_head"] == "" and d["thinking_head"] == ""
    assert d["eval_count"] == 145 and d["done_reason"] == "stop" and d["chunks"] >= 1


@pytest.mark.anyio
async def test_thinking_only_stream_keeps_the_thinking_head_cut_to_500_chars():
    long = "생각" * 400
    adapter, client = _stream([
        {"message": {"role": "assistant", "content": "", "thinking": long[:300]}, "done": False},
        {"message": {"role": "assistant", "content": "", "thinking": long[300:]}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "length", "eval_count": 150},
    ])
    d = (await _empty_exc(adapter, client)).diagnostics
    assert 0 < len(d["thinking_head"]) <= EMPTY_HEAD_CHARS == 500
    assert d["thinking_head"].startswith("생각") and d["thinking_chunks"] == 2 and d["done_reason"] == "length"


@pytest.mark.anyio
async def test_raw_head_keeps_text_that_the_sanitizer_hides():
    # 사고 블록 태그 안쪽은 화면용 본문에서 걸러져 비게 된다 — 걸러지기 전 원문이 남아야 한다
    hidden = "<think>비밀 아닌 추론</think>"
    adapter, client = _stream([{"message": {"role": "assistant", "content": hidden}, "done": True, "eval_count": 9}])
    try:
        turn = await adapter.complete([], (), time.monotonic() + 2, OutputPolicy())
    except LLMEmptyResponse as exc:
        assert "추론" in exc.diagnostics["raw_head"] and len(exc.diagnostics["raw_head"]) <= 500
    else:
        assert turn.content is not None                              # 태그를 거르지 않는 구현이면 본문이 남는다
    finally:
        await client.aclose()


def test_head_limit_constant_is_500():
    assert ollama_mod.EMPTY_HEAD_CHARS == 500


# ── 에이전트 trace ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_two_empties_then_final_answer_leave_two_entries_with_step():
    provider = FakeProvider([LLMEmptyResponse("a", {"eval_count": 145, "chunks": 1}),
                             LLMEmptyResponse("b", {"eval_count": 150, "chunks": 1}),
                             AssistantTurn("답", (), "stop")])
    result = await _run(provider)
    outs = result.trace["empty_outputs"]
    assert [o["eval_count"] for o in outs] == [145, 150] and all("step" in o for o in outs)


@pytest.mark.anyio
async def test_empty_without_diagnostics_still_records_the_step():
    result = await _run(FakeProvider([LLMEmptyResponse("a"), AssistantTurn("답", (), "stop")]))
    assert result.trace["empty_outputs"] == [{"step": 1, "retry": "nudged"}]


@pytest.mark.anyio
async def test_no_empty_response_means_no_key():
    result = await _run(FakeProvider([AssistantTurn("답", (), "stop")]))
    assert "empty_outputs" not in result.trace


@pytest.mark.anyio
async def test_the_api_result_does_not_carry_the_diagnostics():
    provider = FakeProvider([LLMEmptyResponse("a", {"raw_head": "SECRET-RAW"}), AssistantTurn("답", (), "stop")])
    result = await _run(provider)
    assert "SECRET-RAW" not in result.answer
    assert all("SECRET-RAW" not in w for w in map(str, result.warnings))
    assert "SECRET-RAW" not in repr(result.grounding)


# ── chat 기록 → /v1/settings/traces ──────────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("with_empty", [True, False])
async def test_chat_record_puts_empty_outputs_in_the_trace_response_only_when_present(with_empty):
    turns = ([LLMEmptyResponse("a", {"eval_count": 145}), LLMEmptyResponse("b", {"eval_count": 150})] if with_empty else [])
    result = await _run(FakeProvider([*turns, AssistantTurn("답", (), "stop")]))
    log = TurnLog(5)
    chat = ChatApplicationService(None, None, None, None, log)
    chat._record("evidence", result, time.monotonic(), session_id="s", message="질문")
    (trace,) = log.traces("evidence")
    if with_empty:
        assert [o["eval_count"] for o in trace["response"]["empty_outputs"]] == [145, 150]
    else:
        assert "empty_outputs" not in trace["response"]


# ── 재시도 안내(2026-11-09 · 5번 데이터 질문: 같은 입력으로 다시 불러 같은 빈 응답이 났다) ─────────────

from datalens.application.agent import _EMPTY_RETRY_NUDGE, _FINAL_WITHOUT_TOOLS  # noqa: E402
from datalens.ports.llm import ToolCall  # noqa: E402


class _MsgProvider(FakeProvider):
    def __init__(self, turns):
        super().__init__(turns)
        self.calls = []

    async def complete(self, messages, tools, deadline, output_policy):
        self.calls.append(list(messages))
        return await super().complete(messages, tools, deadline, output_policy)


@pytest.mark.anyio
@pytest.mark.parametrize("locale", ["ko", "ja"])
async def test_first_empty_is_retried_with_the_localised_nudge(locale):
    p = _MsgProvider([LLMEmptyResponse("a"), AssistantTurn("答", (), "stop")])
    agent = BoundedAgent(p, NoQueryForge(), max_tool_calls=4, recovery_budget=2)
    await agent.run(InMemorySessionStore(60).create(locale=locale), "質問", time.monotonic() + 5)
    last = p.calls[1][-1]
    assert last.role == "user" and last.content == _EMPTY_RETRY_NUDGE[locale]
    assert len(p.calls[1]) == len(p.calls[0]) + 1


@pytest.mark.anyio
async def test_two_empties_end_with_the_final_instruction_forbidding_a_fake_execution():
    p = _MsgProvider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), AssistantTurn("答", (), "stop")])
    agent = BoundedAgent(p, NoQueryForge(), max_tool_calls=4, recovery_budget=2)
    result = await agent.run(InMemorySessionStore(60).create(locale="ja"), "質問", time.monotonic() + 5)
    assert p.calls[2][-1].content == _FINAL_WITHOUT_TOOLS["ja"]
    assert "実行できなかった照会を実行したと書かないでください" in p.calls[2][-1].content
    assert "실행하지 못한 조회를 실행했다고 쓰지 마세요" in _FINAL_WITHOUT_TOOLS["ko"]
    assert [o.get("retry") for o in result.trace["empty_outputs"]] == ["nudged", None]   # 두 번째 항목에는 retry 가 없다


@pytest.mark.anyio
async def test_three_empties_in_a_row_still_fail_as_invalid_response():
    from datalens.application.agent import AgentInvalidUpstreamResponse
    p = _MsgProvider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), LLMEmptyResponse("c")])
    agent = BoundedAgent(p, NoQueryForge(), max_tool_calls=4, recovery_budget=2)
    with pytest.raises(AgentInvalidUpstreamResponse):
        await agent.run(InMemorySessionStore(60).create(), "質問", time.monotonic() + 5)
    assert len(p.calls) == 3


@pytest.mark.anyio
async def test_no_empty_response_means_no_nudge_and_no_empty_outputs():
    p = _MsgProvider([AssistantTurn("答", (), "stop")])
    agent = BoundedAgent(p, NoQueryForge(), max_tool_calls=4, recovery_budget=2)
    result = await agent.run(InMemorySessionStore(60).create(), "質問", time.monotonic() + 5)
    assert len(p.calls) == 1 and "empty_outputs" not in result.trace
    assert not any(m.content in _EMPTY_RETRY_NUDGE.values() for m in p.calls[0])
