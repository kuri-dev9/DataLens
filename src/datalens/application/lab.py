"""프롬프트 실험 — 실제로 보낸 원문을 고쳐 같은 흐름으로 다시 돌리고 원래 결과와 나란히 본다 (2026-11-10).

사용자 요청: "최근 전송 원문이나 RCA 응답 기록에서 특정 부분을 지우거나 더하면서 실제로 정상 동작하는지
확인하고, 마음에 들면 그대로 추가할 수 있게" — 프롬프트 수정을 자연스럽게.

## 단계
  ① (이 파일) 원문 고르기 · 고쳐 다시 보내기 · 원래 결과와 비교
  ② 추가 지시를 모드×언어 설정에 반영 · 이력 · 되돌리기
  ③ 여러 건 같이 돌리기 · 대기 줄

## 지키는 것
* **실제 흐름 그대로.** `agent.run` 을 그대로 부른다 — 도구 호출·강제 조회·거름 확인·경고까지 화면 질의와
  같다. 원문 1회 전송으로는 "어느 표를 골랐나"가 재현되지 않는다.
* **운영 기록·학습에 섞지 않는다.** `ChatApplicationService.handle` 을 거치지 않으므로 메모리 학습(레시피)·
  TurnLog·evidence_turns·graphio 판정 수신이 일어나지 않는다. 결과는 `lab_runs` 에만 남는다.
* **사용자 질의가 먼저다.** 진행 중인 실제 턴이 있으면 시작하지 않는다(`LabBusy`). 실험은 한 번에 하나다.
  ollama 는 한 번에 하나만 처리하므로, 실험이 돌면 화면 질의가 뒤에 밀린다(2026-11-10 오전 504).
* 임시 세션은 끝나면 지운다 — QueryForge 세션도 함께 정리된다(`SessionService.delete`).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import replace
from typing import Any

from datalens.application import answer_style, ask_check
from datalens.application.agent import AgentError
from datalens.application.runtime_config import HISTORY_STORE_LIMIT, MODES, RuntimeConfig
from datalens.application.trace import blocks
from datalens.prompts import load_system_prompt

# 추가 지시 한 칸의 상한 — 초기값. 지시문 실험이 맥락 창을 잡아먹지 않게 한다
PROMPT_EXTRA_MAX_CHARS = 2000
# 도구 결과 미리보기 행 수 — 비교 화면에서 "무엇을 받았나"만 보이면 된다
STEP_PREVIEW_ROWS = 3
_KANA = re.compile(r"[぀-ヿ]")


class LabError(ValueError):
    pass


class LabBusy(RuntimeError):
    def __init__(self, active: list[dict[str, Any]], lab_running: bool) -> None:
        super().__init__("model is busy")
        self.active = active
        self.lab_running = lab_running


def _guess_locale(text: str) -> str:
    # 언어 칸이 생기기 전(2026-11-10 이전) 기록 — 가나가 있으면 ja
    return "ja" if _KANA.search(text or "") else "ko"


def _head(text: str, n: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def _question(message: str) -> str:
    """graphio 원문은 지시·맥락 뒤에 질문이 온다 — 목록에는 끝부분을 보인다."""
    for marker in ("[질문]", "[質問]", "질문:", "質問:"):
        at = message.rfind(marker)
        if at >= 0:
            return _head(message[at:], 160)
    return _head(message[-160:], 160)


def steps_from_blocks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """blocks() 의 메시지 목록에서 도구 단계(호출 → 결과)만 뽑는다. 원래 결과와 새 결과를 같은 모양으로 본다."""
    steps: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for item in items:
        if item.get("kind") == "agent_step":
            calls = item.get("tool_calls") or []
            if not calls and item.get("content"):
                # 도구 없는 중간 단계 — DataLens 안내(되묻기)나 모델의 중간 답
                steps.append({"kind": "note", "role": item.get("role"), "text": _head(item["content"], 400)})
            for call in calls:
                step = {"kind": "call", "tool": call.get("name"), "arguments": call.get("arguments")}
                steps.append(step)
                pending.append(step)
        elif item.get("kind") == "tool_result":
            step = pending.pop(0) if pending else None
            if step is None:
                step = {"kind": "call", "tool": None, "arguments": None}
                steps.append(step)
            step.update(_result_summary(item.get("content") or ""))
    return steps


def _result_summary(content: str) -> dict[str, Any]:
    try:
        body = json.loads(content)
    except ValueError:
        return {"ok": None, "raw": _head(content, 400)}
    if not isinstance(body, dict):
        return {"ok": None, "raw": _head(content, 400)}
    error = body.get("error") if isinstance(body.get("error"), dict) else None
    out: dict[str, Any] = {"ok": body.get("ok")}
    if error:
        out["error"] = {k: error.get(k) for k in ("code", "message", "detail", "hint", "fix") if error.get(k)}
    for key in ("row_count", "notice", "aggregation_meaning", "arguments_normalized", "table"):
        if body.get(key) is not None:
            out[key] = body[key]
    if isinstance(body.get("preview"), list):
        out["preview"] = body["preview"][:STEP_PREVIEW_ROWS]
    if body.get("warnings"):
        out["warnings"] = [w.get("code") if isinstance(w, dict) else w for w in body["warnings"]]
    return out


def _warning_codes(warnings: Any) -> list[str]:
    return [str(w.get("code")) if isinstance(w, dict) else str(w) for w in warnings or ()]


class LabService:
    def __init__(self, agent: Any, sessions: Any, runtime: RuntimeConfig | None, turn_log: Any, chat: Any, settings: Any) -> None:
        self._agent = agent
        self._sessions = sessions
        self._runtime = runtime
        self._turn_log = turn_log
        self._chat = chat
        self._settings = settings
        self._lock = asyncio.Lock()
        self._running: dict[str, Any] | None = None

    @property
    def _store(self) -> Any:
        return getattr(self._turn_log, "store", None)

    # ── 사용 중 표시 ─────────────────────────────────────────────────────
    def busy(self) -> dict[str, Any]:
        active = self._chat.active_turns() if self._chat is not None else []
        running = None
        if self._running is not None:
            running = {**self._running, "elapsed_s": round(time.monotonic() - self._running["started"], 1)}
            running.pop("started", None)
        return {"active": active, "lab": running, "busy": bool(active) or running is not None}

    # ── 기록 고르기 ──────────────────────────────────────────────────────
    def sources(self) -> list[dict[str, Any]]:
        store = self._store
        if store is None:
            return []
        rows: list[dict[str, Any]] = []
        for trace in store.recent_agent():
            message = self._agent_message(trace)
            response = trace.get("response") or {}
            rows.append({
                "kind": "agent", "id": trace["id"], "at": trace.get("at", ""), "mode": "agent",
                "session_id": trace.get("session_id", ""), "locale": trace.get("locale") or _guess_locale(message),
                "question": _question(message), "tool_calls": response.get("tool_calls"),
                "warnings": _warning_codes(response.get("warnings")),
            })
        for row in store.recent():
            rows.append({
                "kind": "evidence", "id": row["id"], "at": row["at"], "mode": "evidence",
                "session_id": row["session_id"], "seq": row["seq"], "locale": _guess_locale(row["received"]),
                "question": _question(row["received"]), "dropped": len(row.get("dropped") or []),
            })
        rows.sort(key=lambda r: r["at"], reverse=True)
        return rows

    @staticmethod
    def _agent_message(trace: dict[str, Any]) -> str:
        items = (trace.get("sent") or {}).get("messages") or []
        return next((m.get("content") or "" for m in reversed(items) if m.get("kind") == "current_message"), "")

    def source(self, kind: str, source_id: int) -> dict[str, Any]:
        """고칠 수 있는 칸으로 펼친 원문과 원래 결과."""
        store = self._store
        if store is None:
            raise LabError("기록 보관소가 없다")
        if kind == "agent":
            trace = store.agent_trace(source_id)
            if trace is None:
                raise LabError("그 기록이 없다(보관 200건 밖으로 밀렸을 수 있다)")
            items = (trace.get("sent") or {}).get("messages") or []
            message = self._agent_message(trace)
            locale = trace.get("locale") or _guess_locale(message)
            system = next((m.get("content") or "" for m in items if m.get("kind") == "system_prompt"), "")
            history = [{"role": m["role"], "content": m.get("content") or ""} for m in items if m.get("kind") == "history"]
            response = trace.get("response") or {}
            original = {
                "answer": response.get("answer") or "",
                "steps": steps_from_blocks(items),
                "warnings": _warning_codes(response.get("warnings")),
                "warnings_recorded": "warnings" in response,
                "tool_calls": response.get("tool_calls"),
                "duration_ms": response.get("duration_ms"),
                "prompt_tokens": (trace.get("sent") or {}).get("prompt_tokens"),
                "completion_tokens": response.get("completion_tokens"),
                "stop_reason": response.get("stop_reason"),
            }
            return {"kind": kind, "id": source_id, "at": trace.get("at"), "mode": "agent", "locale": locale,
                    "session_id": trace.get("session_id"), "system_prompt": system, "history": history,
                    "message": message, "settings": trace.get("settings"), "original": original,
                    "note": "세션 맥락(데이터셋 계보)은 새 임시 세션 기준으로 다시 만든다 — 원래 세션과 다를 수 있다"}
        if kind == "evidence":
            row = store.evidence_turn(source_id)
            if row is None:
                raise LabError("그 기록이 없다(보관 200건 밖으로 밀렸을 수 있다)")
            locale = _guess_locale(row["received"])
            history: list[dict[str, str]] = []
            for prior in store.evidence_before(row["session_id"], row["seq"]):
                history += [{"role": "user", "content": prior["received"]}, {"role": "assistant", "content": prior["llm_raw"]}]
            original = {
                "answer": row["llm_raw"], "final": row["final"], "dropped": row.get("dropped") or [],
                "verdict_from": row.get("verdict_from"), "steps": [], "warnings": [], "warnings_recorded": False,
            }
            return {"kind": kind, "id": source_id, "at": row["at"], "mode": "evidence", "locale": locale,
                    "session_id": row["session_id"], "system_prompt": load_system_prompt(locale, "evidence"),
                    "history": history[-HISTORY_STORE_LIMIT:], "message": row["received"], "original": original,
                    "note": "받은 원문은 [답변 형식] 을 바꿔 끼우기 전 것이다 — 다시 보낼 때 지금 답변 형식 설정으로 바꿔 끼운다. "
                            "이전 문답은 같은 세션의 앞 기록(받은 원문·모델 원답)으로 다시 채운다"}
        raise LabError("kind 는 agent | evidence")

    # ── 다시 보내기 ──────────────────────────────────────────────────────
    async def run(self, request: dict[str, Any], deadline: float) -> dict[str, Any]:
        mode = request.get("mode")
        if mode not in MODES:
            raise LabError("unknown mode")
        locale = request.get("locale") or self._settings.default_locale
        if locale not in ("ko", "ja"):
            raise LabError("locale 은 ko | ja")
        message = request.get("message") or ""
        if not message.strip():
            raise LabError("message 가 비었다")
        extra = request.get("prompt_extra") or ""
        if not isinstance(extra, str) or len(extra) > PROMPT_EXTRA_MAX_CHARS:
            raise LabError(f"prompt_extra 는 {PROMPT_EXTRA_MAX_CHARS}자 이하 문자열")
        history = request.get("history") or []
        if not isinstance(history, list) or len(history) > HISTORY_STORE_LIMIT or not all(
            isinstance(h, dict) and h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str) for h in history
        ):
            raise LabError(f"history 는 {{role: user|assistant, content}} {HISTORY_STORE_LIMIT}개 이하")
        profile = self._profile(mode, locale, extra, request.get("context"), request.get("params"))

        active = self._chat.active_turns() if self._chat is not None else []
        if active or self._lock.locked():
            raise LabBusy(active, self._lock.locked())
        async with self._lock:
            # 잠금을 잡는 사이 실제 질의가 들어왔을 수 있다 — 한 번 더 본다
            active = self._chat.active_turns() if self._chat is not None else []
            if active:
                raise LabBusy(active, False)
            self._running = {"started": time.monotonic(), "source": request.get("source")}
            try:
                result = await self._run(mode, locale, message, history, profile, deadline)
            finally:
                self._running = None
        at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        source = request.get("source") or {}
        saved = dict(request)
        run_id = None
        if self._store is not None:
            run_id = self._store.save_lab(str(source.get("kind") or "custom"), int(source.get("id") or 0), saved, result, at)
        return {"id": run_id, "at": at, "result": result}

    def _profile(self, mode: str, locale: str, extra: str, context: Any, params: Any) -> Any:
        if self._runtime is not None:
            base = self._runtime.profile(mode)
            try:
                checked_context = {k: RuntimeConfig._check_context(k, v) for k, v in (context or {}).items()}
                checked_params = {k: RuntimeConfig._check_param(k, v) for k, v in (params or {}).items()}
            except ValueError as exc:
                raise LabError(str(exc)) from exc
        else:
            base = self._agent.profile(mode)
            checked_context, checked_params = {}, {}
        # 실험은 저장된 추가 지시 대신 이번 값을 쓴다(빈 값이면 추가 지시 없이) — 그 언어 칸만 바꾼다
        prompt_extra = {**(base.prompt_extra or {}), locale: extra}
        return replace(
            base,
            **{k: v for k, v in checked_context.items() if v is not None},
            params={**base.params, **{k: v for k, v in checked_params.items() if v is not None}},
            prompt_extra=prompt_extra,
        )

    async def _run(self, mode: str, locale: str, message: str, history: list[dict[str, str]], profile: Any,
                   deadline: float) -> dict[str, Any]:
        style_record = None
        if mode == "evidence" and self._runtime is not None:
            # 실제 전송(chat.handle)과 같은 함수로 답변 형식을 바꿔 끼운다
            message, style_record = answer_style.apply(message, self._runtime.answer_style())
        session = self._sessions.create(locale=locale)
        session = self._sessions.commit(replace(session, turn_state=tuple({"role": h["role"], "content": h["content"]} for h in history)))
        started = time.monotonic()
        out: dict[str, Any] = {"mode": mode, "locale": locale, "profile": profile.public(), "style": style_record}
        try:
            try:
                result = await self._agent.run(session, message, deadline, mode=mode, profile=profile)
            except AgentError as exc:
                out.update({"error": {"type": type(exc).__name__, "message": str(exc)}, "tool_calls": exc.tool_calls,
                            "warnings": _warning_codes(exc.warnings), "duration_ms": int((time.monotonic() - started) * 1000)})
                return out
            # QueryForge 세션 정리를 위해 바인딩된 세션을 저장한 뒤 지운다
            self._sessions.commit(result.updated_session)
            raw = result.trace or {}
            usage = raw.get("usage")
            sent = blocks(raw["messages"], raw["tools"], raw["base_len"], getattr(usage, "prompt_tokens", None)) if raw else None
            out.update({
                "answer": result.answer,
                "steps": steps_from_blocks(sent["messages"]) if sent else [],
                "warnings": _warning_codes(result.warnings),
                "warning_details": list(result.warnings),
                "tool_calls": result.tool_calls,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "stop_reason": raw.get("stop_reason"),
                "context": result.context,
                "prompt_chars": sent["total_chars"] if sent else None,
                **({"retries": {k: raw[k] for k in ("no_query_retry", "structured_retry", "unbacked_retry") if raw.get(k)}}
                   if any(raw.get(k) for k in ("no_query_retry", "structured_retry", "unbacked_retry")) else {}),
            })
            if mode == "evidence" and sent:
                # graphio 검증 재현 — 이 답이면 어느 줄이 버려지나
                check = ask_check.check([m.get("content") or "" for m in sent["messages"]], result.answer)
                dropped, final = ask_check.split(check)
                out.update({"dropped": dropped, "final": final})
            return out
        finally:
            try:
                await self._sessions.delete(session.session_id)
            except Exception:  # noqa: BLE001 — 정리 실패가 실험 결과를 막지 않는다(TTL 로 지워진다)
                pass

    # ── 결과 조회 ────────────────────────────────────────────────────────
    def runs(self) -> list[dict[str, Any]]:
        store = self._store
        if store is None:
            return []
        out = []
        for row in store.lab_runs():
            result = row["result"]
            out.append({
                "id": row["id"], "at": row["at"], "source_kind": row["source_kind"], "source_id": row["source_id"],
                "mode": result.get("mode"), "locale": result.get("locale"),
                "question": _question(row["request"].get("message") or ""),
                "prompt_extra": _head(row["request"].get("prompt_extra") or "", 80),
                "tool_calls": result.get("tool_calls"), "warnings": result.get("warnings"),
                "error": (result.get("error") or {}).get("type"), "duration_ms": result.get("duration_ms"),
            })
        return out

    def run_detail(self, run_id: int) -> dict[str, Any] | None:
        store = self._store
        return None if store is None else store.lab_run(run_id)
