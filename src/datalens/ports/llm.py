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
    # Ollama 가 알려 주는 단계별 시간(ms). load 는 모델을 메모리에 올린 시간이다 —
    # 같은 입력의 응답 시간이 28~105초로 흔들린 원인(재적재 추정)을 가리는 값(2026-11-04).
    load_ms: int | None = None
    prompt_eval_ms: int | None = None
    eval_ms: int | None = None

    def timings(self) -> dict[str, int | None]:
        return {"load_ms": self.load_ms, "prompt_eval_ms": self.prompt_eval_ms, "eval_ms": self.eval_ms}

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
    # 요청 모드별 런타임 덮어쓰기(2026-11-04 · 설정 화면 /ui). None 이면 기동 설정(.env)을 쓴다.
    # 프로바이더 옵션(num_ctx·temperature·top_p·top_k·repeat_penalty·num_predict)만 담는다.
    options: dict[str, Any] | None = None
    think: bool | None = None
    idle_timeout_seconds: float | None = None
    # Ollama keep_alive — 응답 뒤 모델을 메모리에 붙잡아 둘 시간("5m"·"30m"·"-1"=무기한·"0"=즉시 내림).
    # None 이면 보내지 않는다(Ollama 기본 5분).
    keep_alive: str | None = None


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


class LLMEmptyResponse(LLMInvalidResponse):
    """본문도 도구 호출도 없이 끝난 생성 (2026-11-08).

    gemma4 가 도구 호출을 Ollama 파서가 읽지 못하는 모양으로 내면 이렇게 끝난다. 화면에
    흘린 토큰이 없으므로 같은 요청을 한 번 더 보내도 출력이 겹치지 않는다 — 에이전트가
    이 경우만 한 번 재시도한다. 스트림 파싱 실패처럼 토큰이 이미 나간 경우는 상위 클래스로 남는다.

    ``diagnostics`` — 무엇이 비었는지 사후에 보는 값(2026-11-09 · graphio 1번 F-227). 토큰 145개를 내고도
    본문·호출이 0 이었는데 무엇을 냈는지 알 길이 없었다. 프로바이더가 받은 원문 앞부분만 싣는다.
    """

    def __init__(self, message: str, diagnostics: dict | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or {}


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
