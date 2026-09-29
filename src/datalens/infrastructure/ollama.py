from __future__ import annotations

import json
import logging
import re
import time
import uuid
from copy import deepcopy
from typing import Any, Awaitable, Callable

import httpx

from datalens.observability import current_request_id

from datalens.ports.llm import (
    AssistantTurn,
    LLMInvalidResponse,
    LLMTimeout,
    LLMUnavailable,
    OutputPolicy,
    ProviderMessage,
    TokenUsage,
    ToolCall,
)
from datalens.ports.queryforge import QueryForgeToolDefinition


LOG = logging.getLogger("datalens.llm")


class OllamaProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        request_timeout_seconds: float,
        idle_timeout_seconds: float = 120.0,
        num_predict: int = 2048,
        progress_interval_seconds: float = 15.0,
        num_ctx: int = 8192,
        temperature: float = 1.0,
        top_p: float = 0.95,
        top_k: int = 64,
        enable_thinking: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._request_timeout = request_timeout_seconds
        self._idle_timeout = idle_timeout_seconds
        self._progress_interval = progress_interval_seconds
        self._options = {
            "num_ctx": num_ctx,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            # 생성 토큰 상한(ADR-033). 유휴 워치독이 못 잡는 "살아있지만 끝나지 않는"
            # 생성(반복 루프)을 끊는 유일한 장치다. OutputPolicy가 있으면 그 값이 우선한다.
            "num_predict": num_predict,
        }
        self._enable_thinking = enable_thinking
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def complete(
        self,
        messages: list[ProviderMessage],
        tools: tuple[QueryForgeToolDefinition, ...],
        deadline: float,
        output_policy: OutputPolicy,
    ) -> AssistantTurn:
        # 비스트리밍 HTTP 호출은 생성이 끝날 때까지 바이트가 오지 않아, read 타임아웃이
        # 유휴 판정이 아니라 "총 시간 상한"으로 오작동한다(정당한 장시간 생성도 죽음).
        # 유휴 기반 생존 판정(ADR-033)을 모든 경로에 동일하게 적용하기 위해
        # 내부 전송은 스트리밍으로 통일한다.
        async def discard(token: str) -> None:
            return None

        return await self.complete_stream(messages, tools, deadline, output_policy, discard)

    async def complete_stream(
        self,
        messages: list[ProviderMessage],
        tools: tuple[QueryForgeToolDefinition, ...],
        deadline: float,
        output_policy: OutputPolicy,
        on_token: Callable[[str], Awaitable[None]],
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AssistantTurn:
        if deadline - time.monotonic() <= 0:
            raise LLMTimeout("LLM deadline exhausted before request")
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": True,
            "messages": [self._message(message, index == 0) for index, message in enumerate(messages)],
            "tools": [self._tool(tool) for tool in tools],
            "options": dict(self._options),
        }
        if output_policy.max_output_tokens is not None:
            payload["options"]["num_predict"] = output_policy.max_output_tokens
        sanitizer = ThoughtStreamSanitizer()
        content_parts: list[str] = []
        calls: tuple[ToolCall, ...] = ()
        final: dict[str, Any] = {}
        started = time.monotonic()
        last_progress = started
        chunk_count = 0
        thinking_chunk_count = 0
        outcome = "completed"
        try:
            # 타임아웃은 유휴 판정(청크 간 간격)이다. 전역 deadline으로 자르지 않는다 —
            # 청크가 흐르고 있는 생성은 deadline이 지나도 죽이지 않는다(ADR-033 WDG-AC-2).
            async with self._client.stream(
                "POST",
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=httpx.Timeout(self._idle_timeout),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    body = json.loads(line)
                    final = body
                    chunk_count += 1
                    message = body.get("message") or {}
                    if message.get("thinking"):
                        thinking_chunk_count += 1
                    chunk = message.get("content") or ""
                    if not isinstance(chunk, str):
                        raise TypeError("content must be a string")
                    visible = sanitizer.feed(chunk)
                    if visible:
                        content_parts.append(visible)
                        await on_token(visible)
                    raw_calls = message.get("tool_calls") or []
                    if raw_calls:
                        calls = tuple(self._tool_call(item) for item in raw_calls)
                    now = time.monotonic()
                    if on_progress is not None and now - last_progress >= self._progress_interval:
                        last_progress = now
                        await on_progress(
                            {
                                "stage": "llm",
                                "elapsed_ms": int((now - started) * 1000),
                                "chunks": chunk_count,
                                "thinking": thinking_chunk_count > 0,
                            }
                        )
        except httpx.TimeoutException as exc:
            outcome = "timeout"
            raise LLMTimeout("Ollama request timed out") from exc
        except httpx.HTTPError as exc:
            outcome = "unavailable"
            raise LLMUnavailable("Ollama request failed") from exc
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            outcome = "invalid_response"
            raise LLMInvalidResponse("Ollama returned an invalid streaming response") from exc
        finally:
            # 스트림이 어떻게 끝났든 "그동안 무엇이 흐르고 있었는지"를 남긴다.
            # 화면에 ping만 보이는 턴이 죽은 행인지 비가시 생성(thinking·사고 블록)인지는
            # 이 카운터 없이는 사후에 구분할 수 없다(실측 트레이스 2026-09-29).
            LOG.info(
                "llm_stream",
                extra={
                    "request_id": current_request_id(),
                    "mode": "stream",
                    "model": self._model,
                    "outcome": outcome,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "chunks": chunk_count,
                    "thinking_chunks": thinking_chunk_count,
                    "visible_chars": sum(len(part) for part in content_parts),
                    "tool_call_count": len(calls),
                    "prompt_eval_count": self._optional_int(final.get("prompt_eval_count")),
                    "eval_count": self._optional_int(final.get("eval_count")),
                    "done_reason": final.get("done_reason"),
                },
            )
        tail = sanitizer.finish()
        if tail:
            content_parts.append(tail)
            await on_token(tail)
        content = "".join(content_parts)
        if not content and not calls:
            raise LLMInvalidResponse("Ollama returned neither content nor tool calls")
        return AssistantTurn(
            content=content,
            tool_calls=calls,
            stop_reason=str(final.get("done_reason") or ("tool_calls" if calls else "stop")),
            usage=TokenUsage(
                prompt_tokens=self._optional_int(final.get("prompt_eval_count")),
                completion_tokens=self._optional_int(final.get("eval_count")),
            ),
            provider_request_id=str(final.get("id")) if final.get("id") is not None else None,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def ready(self, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            return False
        try:
            response = await self._client.get(
                f"{self._base_url}/api/tags",
                timeout=min(timeout_seconds, self._request_timeout),
            )
            response.raise_for_status()
            models = response.json().get("models", [])
            return any(
                isinstance(item, dict)
                and self._model in {item.get("name"), item.get("model")}
                for item in models
            )
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return False

    @staticmethod
    def _tool(tool: QueryForgeToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": deepcopy(tool.input_schema),
            },
        }

    def _message(self, message: ProviderMessage, first: bool = False) -> dict[str, Any]:
        content = message.content
        if first and message.role == "system" and self._enable_thinking:
            content = f"<|think|>{content}"
        converted: dict[str, Any] = {"role": message.role, "content": content}
        if message.role == "tool" and message.tool_name:
            converted["tool_name"] = message.tool_name
        if message.tool_calls:
            converted["tool_calls"] = [
                {"function": {"name": call.name, "arguments": deepcopy(call.arguments)}}
                for call in message.tool_calls
            ]
        return converted

    @staticmethod
    def _tool_call(value: Any) -> ToolCall:
        if not isinstance(value, dict) or not isinstance(value.get("function"), dict):
            raise TypeError("tool call must contain a function")
        function = value["function"]
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise TypeError("tool call name must be a non-empty string")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass
        return ToolCall(
            call_id=str(value.get("id") or f"call_{uuid.uuid4().hex}"),
            name=name,
            arguments=arguments,
        )

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None


_THOUGHT_BLOCK = re.compile(r"<\|channel>thought\b.*?<channel\|>", re.DOTALL)


def strip_thought_blocks(content: str) -> str:
    if not _THOUGHT_BLOCK.search(content):
        return content
    return _THOUGHT_BLOCK.sub("", content).strip()


class ThoughtStreamSanitizer:
    _OPEN = "<|channel>thought"
    _CLOSE = "<channel|>"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        visible: list[str] = []
        while True:
            marker = self._CLOSE if self._inside else self._OPEN
            index = self._buffer.find(marker)
            if index >= 0:
                if not self._inside:
                    visible.append(self._buffer[:index])
                self._buffer = self._buffer[index + len(marker) :]
                self._inside = not self._inside
                continue
            keep = next(
                (
                    size
                    for size in range(min(len(self._buffer), len(marker) - 1), 0, -1)
                    if marker.startswith(self._buffer[-size:])
                ),
                0,
            )
            ready = self._buffer[:-keep] if keep else self._buffer
            self._buffer = self._buffer[-keep:] if keep else ""
            if not self._inside:
                visible.append(ready)
            break
        return "".join(visible)

    def finish(self) -> str:
        visible = "" if self._inside else self._buffer
        self._buffer = ""
        return visible
