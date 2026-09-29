from __future__ import annotations

import json
import logging
import time
import asyncio

import httpx
import pytest

from datalens.infrastructure.ollama import OllamaProvider, strip_thought_blocks
from datalens.ports.llm import LLMInvalidResponse, LLMTimeout, OutputPolicy, ProviderMessage, ToolCall
from datalens.ports.queryforge import QueryForgeToolDefinition


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


TOOL = QueryForgeToolDefinition(
    "schema",
    "List catalog tables",
    {"type": "object", "required": ["action"], "properties": {"action": {"enum": ["list_tables"]}}},
)


def provider(response: dict, captured: list[dict] | None = None) -> tuple[OllamaProvider, httpx.AsyncClient]:
    async def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider("http://ollama.test", "fixture-model", request_timeout_seconds=5, client=client), client


@pytest.mark.anyio
async def test_normal_answer_and_tool_schema_conversion() -> None:
    captured: list[dict] = []
    adapter, client = provider(
        {"message": {"role": "assistant", "content": "완료"}, "done_reason": "stop", "eval_count": 3},
        captured,
    )
    try:
        turn = await adapter.complete(
            [ProviderMessage("user", "테이블을 알려줘")], (TOOL,), time.monotonic() + 1, OutputPolicy(64)
        )
    finally:
        await client.aclose()
    assert turn.content == "완료"
    assert turn.tool_calls == ()
    assert turn.usage.completion_tokens == 3
    function = captured[0]["tools"][0]["function"]
    assert function == {"name": "schema", "description": "List catalog tables", "parameters": TOOL.input_schema}
    assert captured[0]["options"] == {
        "num_ctx": 8192,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 64,
        "num_predict": 64,
    }


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<|channel>thought\n비공개 추론\n<channel|>최종 답변", "최종 답변"),
        ("<|channel>thought<channel|>최종 답변", "최종 답변"),
        ("태그 없는 원문", "태그 없는 원문"),
    ],
    ids=["thk-ac-1", "thk-ac-2", "thk-ac-3"],
)
def test_thk_ac1_ac2_ac3_thought_block_sanitization(content: str, expected: str) -> None:
    assert strip_thought_blocks(content) == expected


@pytest.mark.anyio
async def test_opt_ac1_sampling_options_and_thinking_control_are_sent() -> None:
    captured: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "ok"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaProvider(
        "http://ollama.test",
        "model",
        request_timeout_seconds=1,
        num_ctx=16384,
        temperature=0.8,
        top_p=0.9,
        top_k=32,
        enable_thinking=True,
        client=client,
    )
    try:
        await adapter.complete(
            [ProviderMessage("system", "지침")], (), time.monotonic() + 1, OutputPolicy()
        )
    finally:
        await client.aclose()
    assert captured[0]["options"] == {
        "num_ctx": 16384,
        "temperature": 0.8,
        "top_p": 0.9,
        "top_k": 32,
        # OutputPolicy가 비어 있으면 생성 상한 기본값이 항상 실린다(ADR-033 WDG-AC-3).
        "num_predict": 2048,
    }
    assert captured[0]["messages"][0]["content"] == "<|think|>지침"


@pytest.mark.anyio
async def test_tool_call_is_normalized() -> None:
    adapter, client = provider(
        {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "one", "function": {"name": "schema", "arguments": {"action": "list_tables"}}}]}}
    )
    try:
        turn = await adapter.complete([], (TOOL,), time.monotonic() + 1, OutputPolicy())
    finally:
        await client.aclose()
    assert turn.tool_calls[0].call_id == "one"
    assert turn.tool_calls[0].name == "schema"
    assert turn.tool_calls[0].arguments == {"action": "list_tables"}


@pytest.mark.anyio
async def test_strm_ac3_ac4_stream_forwards_early_tokens_and_hides_split_thought_blocks() -> None:
    chunks = [
        b'{"message":{"content":"<|channel>tho"},"done":false}\n',
        '{"message":{"content":"ught hidden<channel|>안녕"},"done":false}\n'.encode(),
        '{"message":{"content":"하세요"},"done":true,"done_reason":"stop"}\n'.encode(),
    ]
    release_final = asyncio.Event()

    class DelayedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunks[0]
            yield chunks[1]
            await release_final.wait()
            yield chunks[2]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=DelayedStream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaProvider(
        "http://ollama.test", "fixture-model", request_timeout_seconds=5, client=client
    )
    tokens = []
    first_token = asyncio.Event()

    async def capture(token: str) -> None:
        tokens.append(token)
        first_token.set()

    pending = asyncio.create_task(
        adapter.complete_stream(
            [ProviderMessage("system", "system"), ProviderMessage("user", "hello")],
            (),
            time.monotonic() + 2,
            OutputPolicy(),
            capture,
        )
    )
    await asyncio.wait_for(first_token.wait(), 1)
    assert not pending.done()
    release_final.set()
    turn = await pending
    await client.aclose()
    assert "".join(tokens) == "안녕하세요"
    assert turn.content == "안녕하세요"
    assert "thought" not in "".join(tokens)


@pytest.mark.anyio
async def test_malformed_argument_string_is_preserved_for_agent_validation() -> None:
    adapter, client = provider(
        {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "schema", "arguments": "{"}}]}}
    )
    try:
        turn = await adapter.complete([], (TOOL,), time.monotonic() + 1, OutputPolicy())
    finally:
        await client.aclose()
    assert turn.tool_calls[0].arguments == "{"


@pytest.mark.anyio
async def test_invalid_response_is_normalized() -> None:
    adapter, client = provider({"unexpected": True})
    try:
        with pytest.raises(LLMInvalidResponse):
            await adapter.complete([], (), time.monotonic() + 1, OutputPolicy())
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_expired_deadline_does_not_send_request() -> None:
    calls: list[dict] = []
    adapter, client = provider({"message": {"content": "never"}}, calls)
    try:
        with pytest.raises(LLMTimeout, match="before request"):
            await adapter.complete([], (), time.monotonic() - 1, OutputPolicy())
    finally:
        await client.aclose()
    assert calls == []


@pytest.mark.anyio
async def test_http_timeout_is_normalized() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("late", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaProvider("http://ollama.test", "model", request_timeout_seconds=1, client=client)
    try:
        with pytest.raises(LLMTimeout):
            await adapter.complete([], (), time.monotonic() + 1, OutputPolicy())
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_owned_client_is_closed() -> None:
    adapter = OllamaProvider("http://ollama.test", "model", request_timeout_seconds=1)
    assert not adapter._client.is_closed
    await adapter.close()
    await adapter.close()
    assert adapter._client.is_closed


@pytest.mark.anyio
async def test_tool_identity_is_preserved_in_followup_payload() -> None:
    captured: list[dict] = []
    adapter, client = provider({"message": {"content": "완료"}}, captured)
    messages = [
        ProviderMessage("assistant", "", (ToolCall("one", "schema", {"action": "list_tables"}),)),
        ProviderMessage("tool", '{"ok":true}', tool_name="schema"),
    ]
    try:
        await adapter.complete(messages, (TOOL,), time.monotonic() + 1, OutputPolicy())
    finally:
        await client.aclose()
    assert captured[0]["messages"][0]["tool_calls"][0]["function"]["name"] == "schema"
    assert captured[0]["messages"][1] == {"role": "tool", "content": '{"ok":true}', "tool_name": "schema"}


@pytest.mark.anyio
async def test_obs_ac1_completion_logs_token_usage(caplog: pytest.LogCaptureFixture) -> None:
    # complete()도 내부적으로 스트리밍 경로를 타므로 같은 llm_stream 로그로 남는다(ADR-033).
    adapter, client = provider(
        {"message": {"content": "완료"}, "done_reason": "stop", "prompt_eval_count": 7168, "eval_count": 42}
    )
    with caplog.at_level(logging.INFO, logger="datalens.llm"):
        try:
            await adapter.complete([], (), time.monotonic() + 1, OutputPolicy())
        finally:
            await client.aclose()
    records = [item for item in caplog.records if item.getMessage() == "llm_stream"]
    assert records, "LLM 완료 응답은 반드시 로깅되어야 한다"
    record = records[0]
    assert record.prompt_eval_count == 7168
    assert record.eval_count == 42
    assert record.done_reason == "stop"
    assert record.tool_call_count == 0
    assert record.elapsed_ms >= 0


def _stream_provider(stream: httpx.AsyncByteStream) -> tuple[OllamaProvider, httpx.AsyncClient]:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider("http://ollama.test", "fixture-model", request_timeout_seconds=5, client=client), client


async def _drop_token(token: str) -> None:
    pass


@pytest.mark.anyio
async def test_obs_ac2_stream_logs_liveness_stats_with_invisible_thinking(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # thinking 청크는 content가 비어 사용자에게 안 보인다. 계측이 이를 활동으로 계수해야
    # "ping만 보이는 턴"이 죽은 행이 아니라 생성 중이었음을 사후에 판별할 수 있다.
    class ThinkingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"message":{"thinking":"...","content":""},"done":false}\n'
            yield b'{"message":{"thinking":"...","content":""},"done":false}\n'
            yield '{"message":{"content":"답변"},"done":true,"done_reason":"stop","prompt_eval_count":7168,"eval_count":9}\n'.encode()

    adapter, client = _stream_provider(ThinkingStream())
    with caplog.at_level(logging.INFO, logger="datalens.llm"):
        try:
            turn = await adapter.complete_stream([], (), time.monotonic() + 2, OutputPolicy(), _drop_token)
        finally:
            await client.aclose()
    assert turn.content == "답변"
    record = next(item for item in caplog.records if item.getMessage() == "llm_stream")
    assert record.outcome == "completed"
    assert record.chunks == 3
    assert record.thinking_chunks == 2
    assert record.visible_chars == 2
    assert record.prompt_eval_count == 7168
    assert record.eval_count == 9
    assert record.done_reason == "stop"


@pytest.mark.anyio
async def test_wdg_ac2_stream_survives_deadline_while_chunks_flow() -> None:
    # 전역 deadline이 스트림 도중 지나가도, 청크가 흐르는 생성은 중단되지 않는다.
    # 요청 타임아웃은 deadline 잔여 시간이 아니라 유휴 기준으로 설정되어야 한다.
    captured_timeouts: list[dict] = []

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"message":{"content":"\xeb\x8a\x90\xeb\xa6\xac"},"done":false}\n'
            await asyncio.sleep(0.15)
            yield '{"message":{"content":"지만 진행"},"done":true,"done_reason":"stop"}\n'.encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_timeouts.append(dict(request.extensions.get("timeout") or {}))
        return httpx.Response(200, stream=SlowStream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaProvider(
        "http://ollama.test", "m", request_timeout_seconds=5, idle_timeout_seconds=60, client=client
    )
    try:
        turn = await adapter.complete_stream([], (), time.monotonic() + 0.05, OutputPolicy(), _drop_token)
    finally:
        await client.aclose()
    assert turn.content == "느리지만 진행"
    assert captured_timeouts[0]["read"] == 60


@pytest.mark.anyio
async def test_wdg_ac3_length_stop_is_normal_completion() -> None:
    class LengthStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield '{"message":{"content":"부분 답변"},"done":true,"done_reason":"length"}\n'.encode()

    adapter, client = _stream_provider(LengthStream())
    try:
        turn = await adapter.complete_stream([], (), time.monotonic() + 2, OutputPolicy(), _drop_token)
    finally:
        await client.aclose()
    assert turn.content == "부분 답변"
    assert turn.stop_reason == "length"


@pytest.mark.anyio
async def test_wdg_ac5_progress_is_emitted_while_generating() -> None:
    class ThreeChunkStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"message":{"thinking":"...","content":""},"done":false}\n'
            yield b'{"message":{"thinking":"...","content":""},"done":false}\n'
            yield '{"message":{"content":"답"},"done":true,"done_reason":"stop"}\n'.encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ThreeChunkStream())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaProvider(
        "http://ollama.test", "m", request_timeout_seconds=5, progress_interval_seconds=0.0, client=client
    )
    progress: list[dict] = []

    async def capture_progress(data: dict) -> None:
        progress.append(data)

    try:
        await adapter.complete_stream([], (), time.monotonic() + 2, OutputPolicy(), _drop_token, capture_progress)
    finally:
        await client.aclose()
    assert progress, "생성 중 progress가 발행되어야 한다"
    assert progress[0]["stage"] == "llm"
    assert progress[0]["chunks"] >= 1
    assert progress[0]["thinking"] is True
    assert progress[0]["elapsed_ms"] >= 0


@pytest.mark.anyio
async def test_obs_ac3_stream_failure_still_logs_liveness_stats(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class DyingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"message":{"thinking":"...","content":""},"done":false}\n'
            raise httpx.ReadTimeout("idle stream")

    adapter, client = _stream_provider(DyingStream())
    with caplog.at_level(logging.INFO, logger="datalens.llm"):
        try:
            with pytest.raises(LLMTimeout):
                await adapter.complete_stream([], (), time.monotonic() + 2, OutputPolicy(), _drop_token)
        finally:
            await client.aclose()
    record = next(item for item in caplog.records if item.getMessage() == "llm_stream")
    assert record.outcome == "timeout"
    assert record.chunks == 1
    assert record.thinking_chunks == 1


@pytest.mark.anyio
async def test_readiness_uses_model_inventory_without_completion() -> None:
    paths = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"models": [{"name": "fixture-model"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OllamaProvider("http://ollama.test", "fixture-model", request_timeout_seconds=1, client=client)
    try:
        assert await adapter.ready(1) is True
    finally:
        await client.aclose()
    assert paths == ["/api/tags"]
