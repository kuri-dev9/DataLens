"""실제로 모델에 나간 것 — 최근 전송 원문 · 효과 비교 기록 (2026-11-04).

설정 화면(/ui)이 값만 보여 줘서는 "그 값으로 무엇이 나가는지, 바꾸면 무엇이 달라지는지"를
알 수 없었다(사용자 지적). 그래서 턴마다 두 가지를 남긴다.

  trace    모델에 보낸 메시지 배열 전체 + 도구 정의 + 응답 원문. 모드별 최근 N건
  history  그때의 설정 요약과 결과(토큰·시간·종료 사유) 한 줄. 최근 50건

**기본은 메모리에만 둔다.** 원문에는 호출부가 보낸 증거(graphio 증거팩 등)가 그대로 들어 있다.
예외는 evidence 모드다(2026-11-04 사용자 결정) — graphio 검증기가 버린 줄을 시연에서 다시
보이려고 store(memory.sqlite3 의 evidence_turns — 받은 원문·LLM 결과·버린 줄·최종 답 4칸,
최근 200건)에도 쓴다. 로그에는 쓰지 않는다.
조회는 /v1/settings 와 같이 API 키가 필요하다.

블록별 토큰은 **추정**이다. Ollama 는 프롬프트 전체의 토큰 수(prompt_eval_count)만 알려 주므로
글자 수 비율로 나눠 붙인다.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from copy import deepcopy
from typing import Any, Iterable

from datalens.application import ask_check
from datalens.ports.llm import ProviderMessage
from datalens.ports.queryforge import QueryForgeToolDefinition

HISTORY_LIMIT = 50
# 실측 기록이 없을 때 쓰는 글자/토큰 비. 한국어 2.15자/토큰(ADR-017 개정 실측)
DEFAULT_CHARS_PER_TOKEN = 2.15


def tool_payload(tool: QueryForgeToolDefinition) -> dict[str, Any]:
    # OllamaProvider._tool 과 같은 모양 — 글자 수를 같은 기준으로 세기 위해서다.
    return {"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.input_schema}}


def _kind(message: ProviderMessage, index: int, base_len: int) -> str:
    if index >= base_len:
        return "tool_result" if message.role == "tool" else "agent_step"
    if message.role == "system":
        if message.content.startswith("DataLens session context"):
            return "session_context"
        if message.content.startswith("DataLens memory"):
            return "memory"
        return "system_prompt"
    if index == base_len - 1:
        return "current_message"
    return "history"


KIND_LABEL = {
    "system_prompt": "시스템 프롬프트",
    "session_context": "세션 맥락",
    "memory": "메모리(회상 힌트)",
    "history": "이전 문답",
    "current_message": "사용자 메시지(이번 질문)",
    "agent_step": "모델 도구 호출",
    "tool_result": "도구 결과",
    "tools": "도구 정의",
}


def blocks(
    messages: Iterable[ProviderMessage],
    tools: Iterable[QueryForgeToolDefinition],
    base_len: int,
    prompt_tokens: int | None,
    chars_per_token: float | None = None,
) -> dict[str, Any]:
    """메시지 배열을 블록으로 나누고 블록마다 글자 수와 (추정) 토큰을 붙인다."""
    items = []
    for index, message in enumerate(messages):
        body = {"role": message.role, "content": message.content}
        if message.tool_calls:
            body["tool_calls"] = [{"name": call.name, "arguments": call.arguments} for call in message.tool_calls]
        chars = len(json.dumps(body, ensure_ascii=False))
        items.append({"kind": _kind(message, index, base_len), "role": message.role, "chars": chars, "content": message.content,
                      "tool_calls": body.get("tool_calls")})
    tool_list = [tool_payload(tool) for tool in tools]
    tool_chars = len(json.dumps(tool_list, ensure_ascii=False)) if tool_list else 0
    total_chars = sum(item["chars"] for item in items) + tool_chars
    if prompt_tokens and total_chars:
        ratio, measured = total_chars / prompt_tokens, True
    else:
        ratio, measured = chars_per_token or DEFAULT_CHARS_PER_TOKEN, False
    for item in items:
        item["tokens_est"] = round(item["chars"] / ratio)
    # 같은 종류가 이어지면 화면에서 한 묶음으로 접는다(예: 이전 문답 4항목).
    summary: dict[str, dict[str, int]] = {}
    for item in items:
        entry = summary.setdefault(item["kind"], {"count": 0, "chars": 0, "tokens_est": 0})
        entry["count"] += 1
        entry["chars"] += item["chars"]
        entry["tokens_est"] += item["tokens_est"]
    if tool_list:
        summary["tools"] = {"count": len(tool_list), "chars": tool_chars, "tokens_est": round(tool_chars / ratio)}
    return {
        "messages": items,
        "tools": [{"name": t["function"]["name"], "chars": len(json.dumps(t, ensure_ascii=False)), "definition": t} for t in tool_list],
        "summary": summary,
        "total_chars": total_chars,
        "prompt_tokens": prompt_tokens,
        "tokens_est_total": round(total_chars / ratio),
        "chars_per_token": round(ratio, 3),
        # measured=False 면 비율 자체가 실측이 아니다 — 블록 토큰은 전부 추정이다.
        "ratio_measured": measured,
    }


def with_check(trace: dict[str, Any]) -> dict[str, Any]:
    """evidence 모드 trace 에 graphio 검증 재현 결과를 붙인다."""
    contents = [m.get("content") or "" for m in (trace.get("sent") or {}).get("messages") or []]
    answer = (trace.get("response") or {}).get("answer") or ""
    return {**trace, "check": ask_check.check(contents, answer)}


class TurnLog:
    def __init__(self, keep: int = 5, store: Any = None) -> None:
        self._lock = threading.Lock()
        self._keep = keep
        self._store = store
        self._traces: dict[str, deque[dict[str, Any]]] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)

    @property
    def keep(self) -> int:
        return self._keep

    def add(self, mode: str, trace: dict[str, Any] | None, row: dict[str, Any]) -> None:
        if trace is not None and mode == "evidence":
            trace = with_check(trace)
            if self._store is not None:
                try:
                    self._store.save(trace)
                except Exception:  # noqa: BLE001 — 보관 실패가 답을 막으면 안 된다
                    pass
        with self._lock:
            if trace is not None and self._keep > 0:
                self._traces.setdefault(mode, deque(maxlen=self._keep)).append(deepcopy(trace))
            self._history.append(deepcopy(row))

    def traces(self, mode: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            modes = [mode] if mode else list(self._traces)
            found = [deepcopy(item) for name in modes for item in self._traces.get(name, ())]
        return sorted(found, key=lambda item: item.get("at", ""), reverse=True)

    def last_request(self, mode: str, marker: str | None = None) -> dict[str, Any] | None:
        """그 모드의 가장 최근 실제 요청 원문 — 설정 화면 미리보기의 바탕 (2026-11-04).

        `marker` 를 주면 그것을 담은 요청을 먼저 찾는다(RCA 응답은 `[답변 형식]` 이 있는 첫 질문 —
        이어 묻기는 질문만 보내 스타일 효과가 보이지 않는다). 메모리에 없으면 sqlite 기록에서 찾는다.
        """
        found: list[dict[str, Any]] = []
        for trace in self.traces(mode):
            text = trace.get("received")
            if text is None:
                messages = (trace.get("sent") or {}).get("messages") or []
                text = next((m.get("content", "") for m in reversed(messages) if m.get("kind") == "current_message"), "")
            found.append({"message": text, "at": trace.get("at", ""), "session_id": trace.get("session_id", ""), "source": "memory"})
        if mode == "evidence" and self._store is not None:
            try:
                found += [
                    {"message": r["received"], "at": r["at"], "session_id": r["session_id"], "source": "sqlite"}
                    for r in self._store.recent()
                ]
            except Exception:  # noqa: BLE001 — 미리보기 바탕을 못 찾는 것으로 끝낸다
                pass
        found = [item for item in found if item["message"]]
        found.sort(key=lambda item: item["at"], reverse=True)
        if marker:
            marked = [item for item in found if marker in item["message"]]
            if marked:
                return marked[0]
        return found[0] if found else None

    def evidence_turns(self) -> list[dict[str, Any]] | None:
        """sqlite 의 evidence 호출 기록(최근 200건). store 가 없으면 None."""
        return None if self._store is None else self._store.recent()

    def attach_verdict(self, session_id: str, verdict: dict[str, Any], at: str) -> dict[str, Any] | None:
        """graphio 정본 판정을 sqlite 기록에 붙인다. 붙일 행이 없으면 None."""
        return None if self._store is None else self._store.attach_verdict(session_id, verdict, at)

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(row) for row in self._history]

    def chars_per_token(self, mode: str) -> float | None:
        """그 모드의 최근 실측 글자/토큰 비 — 미리보기의 토큰 추정에 쓴다."""
        with self._lock:
            for item in reversed(self._traces.get(mode, ())):
                sent = item.get("sent") or {}
                ratio = sent.get("chars_per_token")
                # 비정상 비율(빈 응답·흉내 값)은 쓰지 않는다 — 실측 범위는 JSON 포함 약 1.5~4 자/토큰
                if sent.get("ratio_measured") and ratio and 0.8 <= ratio <= 8:
                    return ratio
        return None
