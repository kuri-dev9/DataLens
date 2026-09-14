from __future__ import annotations

import json
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
    assert captured[0]["messages"][1] == {"role": "tool", "content": '{"ok":true}'}


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
