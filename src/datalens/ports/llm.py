from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from datalens.ports.queryforge import QueryForgeToolDefinition


MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Any


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    role: MessageRole
    content: str
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class OutputPolicy:
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    usage: TokenUsage | None = None
    provider_request_id: str | None = None


class LLMProviderError(RuntimeError):
    pass


class LLMTimeout(LLMProviderError):
    pass


class LLMInvalidResponse(LLMProviderError):
    pass


class LLMUnavailable(LLMProviderError):
    pass


class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[ProviderMessage],
        tools: tuple[QueryForgeToolDefinition, ...],
        deadline: float,
        output_policy: OutputPolicy,
    ) -> AssistantTurn: ...

    async def close(self) -> None: ...
