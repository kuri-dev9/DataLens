from __future__ import annotations

import json
import re
import time
import uuid
from copy import deepcopy
from typing import Any, Awaitable, Callable

import httpx

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


class OllamaProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        request_timeout_seconds: float,
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
        self._options = {
            "num_ctx": num_ctx,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
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
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LLMTimeout("LLM deadline exhausted before request")
        timeout = min(self._request_timeout, remaining)
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": False,
            "messages": [self._message(message, index == 0) for index, message in enumerate(messages)],
            "tools": [self._tool(tool) for tool in tools],
            "options": dict(self._options),
        }
        if output_policy.max_output_tokens is not None:
            payload["options"]["num_predict"] = output_policy.max_output_tokens
        try:
            response = await self._client.post(
                f"{self._base_url}/api/chat", json=payload, timeout=timeout
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LLMTimeout("Ollama request timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable("Ollama request failed") from exc
        try:
            body = response.json()
            message = body["message"]
            if not isinstance(message, dict):
                raise TypeError("message must be an object")
            calls = tuple(self._tool_call(item) for item in message.get("tool_calls") or [])
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise TypeError("content must be a string")
            if content is not None:
                content = strip_thought_blocks(content)
            if content is None and not calls:
                raise ValueError("response has neither content nor tool calls")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LLMInvalidResponse("Ollama returned an invalid response") from exc
        usage = TokenUsage(
            prompt_tokens=self._optional_int(body.get("prompt_eval_count")),
            completion_tokens=self._optional_int(body.get("eval_count")),
        )
        return AssistantTurn(
            content=content,
            tool_calls=calls,
            stop_reason=str(body.get("done_reason") or ("tool_calls" if calls else "stop")),
            usage=usage,
            provider_request_id=str(body.get("id")) if body.get("id") is not None else None,
        )

    async def complete_stream(
        self,
        messages: list[ProviderMessage],
        tools: tuple[QueryForgeToolDefinition, ...],
        deadline: float,
        output_policy: OutputPolicy,
        on_token: Callable[[str], Awaitable[None]],
    ) -> AssistantTurn:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
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
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=min(self._request_timeout, remaining),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    body = json.loads(line)
                    final = body
                    message = body.get("message") or {}
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
        except httpx.TimeoutException as exc:
            raise LLMTimeout("Ollama request timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailable("Ollama request failed") from exc
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LLMInvalidResponse("Ollama returned an invalid streaming response") from exc
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
