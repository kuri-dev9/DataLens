from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Protocol

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
    # 컨텍스트 예산 대조용. ollama는 프롬프트를 잘라도 그 사실을 알려주지 않으므로
    # (응답에 truncated 같은 필드가 없다) 아래 숫자를 맞대어 역산하는 수밖에 없다.
    context_window: int | None = None
    reserved_output_tokens: int | None = None
    prompt_chars: int | None = None

    @property
    def context_status(self) -> str:
        """ok | tight | truncated | unknown.

        `truncated`는 프롬프트 토큰이 컨텍스트 창을 꽉 채운 경우다 — 창보다 큰
        프롬프트는 평가될 수 없으므로, 천장에 닿았다는 건 앞쪽이 잘려 들어갔다는 뜻이다.
        `tight`는 프롬프트는 들어갔지만 생성 예약분을 빼면 자리가 모자라, 생성 도중
        앞쪽이 밀려나는 경우다. 둘 다 "모델이 증거를 못 봤다"로 이어진다.
        """
        if self.prompt_tokens is None or not self.context_window:
            return "unknown"
        if self.prompt_tokens >= self.context_window:
            return "truncated"
        if self.prompt_tokens + (self.reserved_output_tokens or 0) > self.context_window:
            return "tight"
        return "ok"

    def public(self) -> dict[str, int | str | None]:
        return {
            "status": self.context_status,
            "num_ctx": self.context_window,
            "prompt_tokens": self.prompt_tokens,
            "prompt_chars": self.prompt_chars,
            "reserved_output_tokens": self.reserved_output_tokens,
            "headroom_tokens": (
                None
                if self.prompt_tokens is None or not self.context_window
                else self.context_window - self.prompt_tokens
            ),
        }


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

    async def complete_stream(
        self,
        messages: list[ProviderMessage],
        tools: tuple[QueryForgeToolDefinition, ...],
        deadline: float,
        output_policy: OutputPolicy,
        on_token: Callable[[str], Awaitable[None]],
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> AssistantTurn: ...

    async def close(self) -> None: ...
