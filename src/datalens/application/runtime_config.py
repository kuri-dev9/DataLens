"""요청 모드별 런타임 설정 — 무엇을 모델에 싣고, 어떤 파라미터로 부르나 (2026-11-04).

## 왜 생겼나

DataLens 는 데이터 에이전트다. 모든 요청에 QueryForge 조회 지침(`prompts/<locale>/system.txt`)과
도구 정의를 싣는다. graphio 의 AI 질의는 **이미 증거팩을 들고 와서** "이것으로 설명하라"고
묻는데, 그 요청에도 같은 것이 실려 모델이 받은 43,590자 중 증거는 약 18%였다(graphio 룸
조사 2026-11-04). 두 지침("DB 도구로만 사실을 말하라" ↔ "증거팩만 근거로 하라")이 한
프롬프트 안에서 충돌했고, 모델은 도구를 한 번도 부르지 않았다.

그래서 **요청이 모드를 고른다.**

  agent     지금까지의 동작. QueryForge 지침·세션 맥락·도구 정의를 싣는다
  evidence  증거 전용. 보낸 맥락만으로 답한다 — QueryForge 지침·데이터셋 목록·도구를 싣지 않는다

모드마다 "무엇을 실을지"(`context`)와 모델 파라미터(`params`)를 화면(`/ui`)에서 바꾼다.

## 값의 출처

    기본값(config.py)  <  .env(DATALENS_*)  <  화면(이 파일이 저장)

화면 값은 `DATALENS_RUNTIME_CONFIG_PATH`(기본 `/var/lib/datalens/runtime.json`, 메모리와 같은
볼륨)에 저장되어 재시작해도 남는다. **화면 값을 지우면 `.env` 값으로 돌아간다** — 화면이
`.env` 를 고쳐 쓰지 않는다. 비밀값은 여기 없다.

## 하지 않는 것

* 모델 이름은 바꾸지 않는다. `/v1/ready` 가 그 모델이 설치됐는지 보고, 그 판정이 기동 설정에
  묶여 있다. 화면은 보여 주기만 한다.
* 메시지 상한(`MESSAGE_MAX_CHARS`)은 요청 검증 단계의 상수라 보여 주기만 한다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

from datalens.application import answer_style
from datalens.config import Settings


LOG = logging.getLogger("datalens.runtime_config")

Mode = Literal["agent", "evidence"]
MODES: tuple[Mode, ...] = ("agent", "evidence")
ToolsLevel = Literal["off", "lazy", "summary", "full"]
TOOLS_LEVELS = ("off", "lazy", "summary", "full")

# 세션에 남기는 문답 항목의 상한. history_items 는 이 안에서만 고를 수 있다.
HISTORY_STORE_LIMIT = 24

# 모드별 "무엇을 싣나" 기본값. agent 는 지금까지의 동작과 같아야 한다(역호환).
_CONTEXT_DEFAULTS: dict[Mode, dict[str, Any]] = {
    "agent": {
        "system_prompt": True,
        "session_context": True,
        "memory": True,
        # lazy — 이름·한 줄 설명·최상위 인자만 싣고, 모델이 원하면 get_tool_spec 으로 전체를 준다(2026-11-04).
        # full 은 도구 5개가 32,058자로 입력의 89% · num_ctx 대비 114%(잘림)였다(사용자 미리보기 실측).
        "tools": "lazy",
        "history_items": 6,
    },
    "evidence": {
        # 이 모드의 시스템 프롬프트는 QueryForge 지침이 아니라 prompts/<locale>/evidence.txt 다.
        "system_prompt": True,
        # now/today 는 쓸모가 있지만 데이터셋 목록은 없다 — 켜도 시각만 싣는다.
        "session_context": False,
        # 회상 힌트는 "예전에 성공한 조회 계획"이다. 도구가 없는 모드에서는 쓸 데가 없다.
        "memory": False,
        "tools": "off",
        # graphio 는 같은 판정 단위를 3턴까지 증거 없이 이어 묻는다(rca.llm.ask.compact_turns).
        # 첫 턴의 증거가 창에서 빠지지 않으려면 3턴 = 6항목이 들어가야 한다.
        "history_items": 6,
    },
}

# 화면에서 바꿀 수 있는 파라미터 → Settings 필드. 범위는 Settings 의 Field 제약과 맞춘다.
_PARAM_FIELDS: dict[str, str] = {
    "temperature": "ollama_temperature",
    "top_p": "ollama_top_p",
    "top_k": "ollama_top_k",
    "repeat_penalty": "ollama_repeat_penalty",
    "num_ctx": "ollama_num_ctx",
    "num_predict": "ollama_num_predict",
    "think": "enable_thinking",
    # 스트림 청크 사이의 최대 공백(ADR-033). 총 응답 시간 상한이 아니다.
    "idle_timeout_seconds": "ollama_idle_timeout_seconds",
    "keep_alive": "ollama_keep_alive",
}
# 모드별 파라미터 기본값 — .env 가 그 필드를 주지 않았을 때만 쓴다(기본값 < 모드 기본값 < .env < 화면).
# agent repeat_penalty 1.1 은 **초기값**이다(2026-11-08). graphio 일반 질의에서 정렬·limit 없이 받은
# 긴 표를 옮기다 답 끝이 같은 행(`134373 | 3`)을 되풀이하며 무너졌다. evidence 는 증거 수치를 그대로
# 옮겨야 하므로 1.0(끔)을 둔다 — 높이면 수치·이름을 다르게 옮길 위험이 있다(설정 화면 설명).
_MODE_PARAM_DEFAULTS: dict[Mode, dict[str, Any]] = {
    "agent": {"repeat_penalty": 1.1},
    "evidence": {},
}
_KEEP_ALIVE = re.compile(r"^-?\d+(\.\d+)?(ms|s|m|h)?$")
_PARAM_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (0.0, 2.0),
    "top_p": (0.0, 1.0),
    "top_k": (1, 500),
    "repeat_penalty": (1.0, 2.0),
    "num_ctx": (1024, 131072),
    "num_predict": (16, 32768),
    "idle_timeout_seconds": (5.0, 1800.0),
}


# 추가 지시(시스템 지시문 끝에 붙는 문장) — 모드×언어 한 칸의 상한과 이력 보관 수. 둘 다 초기값(2026-11-10)
PROMPT_EXTRA_MAX_CHARS = 2000
PROMPT_EXTRA_HISTORY_LIMIT = 20
LOCALES = ("ko", "ja")


class RuntimeConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Profile:
    """한 턴에 적용할 모드의 실효 설정."""

    mode: Mode
    system_prompt: bool
    session_context: bool
    memory: bool
    tools: ToolsLevel
    history_items: int
    params: dict[str, Any]
    # 시스템 지시문 끝에 붙는 추가 지시 — 언어별(ko·ja)(2026-11-10 프롬프트 실험). 비면 지금 동작과 같다
    prompt_extra: dict[str, str] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            **({"prompt_extra": dict(self.prompt_extra)} if any(self.prompt_extra.values()) else {}),
            "context": {
                "system_prompt": self.system_prompt,
                "session_context": self.session_context,
                "memory": self.memory,
                "tools": self.tools,
                "history_items": self.history_items,
            },
            "params": dict(self.params),
        }


class RuntimeConfig:
    def __init__(self, settings: Settings, path: str | None = None) -> None:
        self._settings = settings
        self._path = path
        self._lock = threading.Lock()
        self._overrides: dict[str, dict[str, dict[str, Any]]] = {
            mode: {"context": {}, "params": {}} for mode in MODES
        }
        # 모드별 마지막 턴의 분량. 화면이 "지금 무엇이 얼마나 실리나"를 보여 주는 근거다.
        self._last_turn: dict[str, dict[str, Any]] = {}
        # evidence 답의 형식·길이 — graphio `[답변 형식]` 구역을 바꿔 끼울 값(answer_style.py)
        self._answer_style: dict[str, Any] = dict(answer_style.DEFAULTS)
        # 추가 지시 — 프롬프트 실험에서 고른 문장을 반영하는 자리(2026-11-10 ②). 기본 지시문 파일은 고치지 않는다.
        # 언어별로 따로 둔다(사용자 결정) — 일본 PoC 는 ja 가 운영이라 ko 실험 문장이 넘어가면 안 된다
        self._prompt_extra: dict[str, dict[str, str]] = {mode: {loc: "" for loc in LOCALES} for mode in MODES}
        # 반영·되돌리기 이력(최신이 뒤). 되돌리기도 한 항목으로 남는다
        self._prompt_extra_history: list[dict[str, Any]] = []
        self._load()

    # ── 조회 ──────────────────────────────────────────────────────────

    def profile(self, mode: Mode = "agent") -> Profile:
        if mode not in MODES:
            raise RuntimeConfigError(f"unknown mode: {mode}")
        with self._lock:
            override = deepcopy(self._overrides[mode])
        context = {**_CONTEXT_DEFAULTS[mode], **override["context"]}
        params = {key: override["params"].get(key, self._base_param(key, mode)) for key in _PARAM_FIELDS}
        with self._lock:
            extra = {loc: text for loc, text in self._prompt_extra[mode].items() if text}
        return Profile(mode=mode, params=params, prompt_extra=extra, **context)

    def describe(self) -> dict[str, Any]:
        """화면용 — 값마다 실효값과 출처(default | env | ui)를 단다."""
        with self._lock:
            overrides = deepcopy(self._overrides)
            last_turn = deepcopy(self._last_turn)
        fields_set = self._settings.model_fields_set
        modes: dict[str, Any] = {}
        for mode in MODES:
            context = {}
            for key, default in _CONTEXT_DEFAULTS[mode].items():
                ui = overrides[mode]["context"].get(key)
                context[key] = {
                    "value": default if ui is None else ui,
                    "source": "default" if ui is None else "ui",
                    "default": default,
                }
            params = {}
            for key, field in _PARAM_FIELDS.items():
                ui = overrides[mode]["params"].get(key)
                base = self._base_param(key, mode)
                params[key] = {
                    "value": base if ui is None else ui,
                    "source": "ui" if ui is not None else ("env" if field in fields_set else "default"),
                    "env_or_default": base,
                    "env_var": f"DATALENS_{field.upper()}",
                    "range": _PARAM_RANGES.get(key),
                }
            modes[mode] = {"context": context, "params": params, "last_turn": last_turn.get(mode)}
        return {
            "modes": modes,
            "model": self._settings.ollama_model,
            "path": self._path,
            "persisted": bool(self._path),
            "history_store_limit": HISTORY_STORE_LIMIT,
            "prompt_extra": self.prompt_extra()["current"],
        }

    # ── 변경 ──────────────────────────────────────────────────────────

    def update(self, mode: Mode, context: dict[str, Any] | None, params: dict[str, Any] | None) -> None:
        """부분 갱신. 값으로 None 을 주면 그 항목의 화면 값을 지운다(→ .env·기본값)."""
        if mode not in MODES:
            raise RuntimeConfigError(f"unknown mode: {mode}")
        checked_context = {key: self._check_context(key, value) for key, value in (context or {}).items()}
        checked_params = {key: self._check_param(key, value) for key, value in (params or {}).items()}
        with self._lock:
            target = self._overrides[mode]
            for section, values in (("context", checked_context), ("params", checked_params)):
                for key, value in values.items():
                    if value is None:
                        target[section].pop(key, None)
                    else:
                        target[section][key] = value
            snapshot = self._snapshot()
        self._save(snapshot)

    def reset(self, mode: Mode) -> None:
        if mode not in MODES:
            raise RuntimeConfigError(f"unknown mode: {mode}")
        with self._lock:
            self._overrides[mode] = {"context": {}, "params": {}}
            snapshot = self._snapshot()
        self._save(snapshot)

    def answer_style(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._answer_style)

    def update_answer_style(self, values: dict[str, Any]) -> dict[str, Any]:
        """부분 갱신. 빠진 항목은 지금 값을 둔다."""
        with self._lock:
            current = deepcopy(self._answer_style)
        try:
            style = answer_style.check({**current, **values})
        except answer_style.AnswerStyleError as exc:
            raise RuntimeConfigError(str(exc)) from exc
        with self._lock:
            self._answer_style = style
            snapshot = self._snapshot()
        self._save(snapshot)
        return deepcopy(style)

    # ── 추가 지시(프롬프트 실험 ②) ────────────────────────────────────────

    def prompt_extra(self) -> dict[str, Any]:
        with self._lock:
            return {
                "current": deepcopy(self._prompt_extra),
                "history": deepcopy(list(reversed(self._prompt_extra_history))),
                "max_chars": PROMPT_EXTRA_MAX_CHARS,
                "history_limit": PROMPT_EXTRA_HISTORY_LIMIT,
            }

    @staticmethod
    def _check_prompt_extra(mode: Any, locale: Any, text: Any) -> str:
        if mode not in MODES:
            raise RuntimeConfigError(f"unknown mode: {mode}")
        if locale not in LOCALES:
            raise RuntimeConfigError("locale must be ko | ja")
        if not isinstance(text, str):
            raise RuntimeConfigError("text must be a string")
        text = text.strip()
        if len(text) > PROMPT_EXTRA_MAX_CHARS:
            raise RuntimeConfigError(f"text must be at most {PROMPT_EXTRA_MAX_CHARS} chars")
        return text

    def set_prompt_extra(
        self, mode: Mode, locale: str, text: str, *, source_run: int | None = None, at: str = "",
        action: str = "set", reverted_from: int | None = None,
    ) -> dict[str, Any]:
        """그 모드×언어 칸을 바꾸고 이력 한 항목을 남긴다. 값이 같으면 아무것도 남기지 않는다(None 반환)."""
        text = self._check_prompt_extra(mode, locale, text)
        with self._lock:
            before = self._prompt_extra[mode][locale]
            if before == text:
                return None  # type: ignore[return-value]
            entry = {
                "id": max((item["id"] for item in self._prompt_extra_history), default=0) + 1,
                "at": at, "mode": mode, "locale": locale, "action": action,
                "before": before, "after": text, "source_run": source_run,
                **({"reverted_from": reverted_from} if reverted_from is not None else {}),
            }
            self._prompt_extra[mode][locale] = text
            self._prompt_extra_history = [*self._prompt_extra_history, entry][-PROMPT_EXTRA_HISTORY_LIMIT:]
            snapshot = self._snapshot()
        self._save(snapshot)
        return deepcopy(entry)

    def revert_prompt_extra(self, history_id: int, at: str = "") -> dict[str, Any] | None:
        """이력 한 항목을 그 반영 전 값으로 되돌린다. 되돌린 것도 이력에 남는다. 값이 이미 같으면 None."""
        with self._lock:
            found = next((item for item in self._prompt_extra_history if item["id"] == history_id), None)
        if found is None:
            raise RuntimeConfigError("unknown history id")
        return self.set_prompt_extra(found["mode"], found["locale"], found["before"], at=at,
                                     action="revert", reverted_from=history_id)

    def _snapshot(self) -> dict[str, Any]:
        # 호출자가 잠금을 쥐고 부른다. 모드 키 옆에 answer_style·추가 지시를 함께 저장한다
        return {**deepcopy(self._overrides), "answer_style": deepcopy(self._answer_style),
                "prompt_extra": deepcopy(self._prompt_extra),
                "prompt_extra_history": deepcopy(self._prompt_extra_history)}

    def record_turn(self, mode: Mode, summary: dict[str, Any]) -> None:
        with self._lock:
            self._last_turn[mode] = deepcopy(summary)

    # ── 내부 ──────────────────────────────────────────────────────────

    def _base_param(self, key: str, mode: Mode | None = None) -> Any:
        field = _PARAM_FIELDS[key]
        if mode is not None and field not in self._settings.model_fields_set and key in _MODE_PARAM_DEFAULTS[mode]:
            return _MODE_PARAM_DEFAULTS[mode][key]
        return getattr(self._settings, field)

    @staticmethod
    def _check_context(key: str, value: Any) -> Any:
        if key not in _CONTEXT_DEFAULTS["agent"]:
            raise RuntimeConfigError(f"unknown context key: {key}")
        if value is None:
            return None
        if key == "tools":
            if value not in TOOLS_LEVELS:
                raise RuntimeConfigError("tools must be off | lazy | summary | full")
            return value
        if key == "history_items":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= HISTORY_STORE_LIMIT:
                raise RuntimeConfigError(f"history_items must be an integer 0..{HISTORY_STORE_LIMIT}")
            return value
        if not isinstance(value, bool):
            raise RuntimeConfigError(f"{key} must be true | false")
        return value

    @staticmethod
    def _check_param(key: str, value: Any) -> Any:
        if key not in _PARAM_FIELDS:
            raise RuntimeConfigError(f"unknown parameter: {key}")
        if value is None:
            return None
        if key == "think":
            if not isinstance(value, bool):
                raise RuntimeConfigError("think must be true | false")
            return value
        if key == "keep_alive":
            text = str(value).strip()
            if text == "":
                return None
            if isinstance(value, bool) or not _KEEP_ALIVE.fullmatch(text):
                raise RuntimeConfigError("keep_alive must look like 5m · 30m · 2h · 300 · -1 · 0")
            return text
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeConfigError(f"{key} must be a number")
        low, high = _PARAM_RANGES[key]
        if not low <= value <= high:
            raise RuntimeConfigError(f"{key} must be within {low}..{high}")
        if key in {"top_k", "num_ctx", "num_predict"}:
            if int(value) != value:
                raise RuntimeConfigError(f"{key} must be an integer")
            return int(value)
        return float(value)

    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                stored = json.load(handle)
            for mode in MODES:
                entry = stored.get(mode) or {}
                context = {k: self._check_context(k, v) for k, v in (entry.get("context") or {}).items()}
                params = {k: self._check_param(k, v) for k, v in (entry.get("params") or {}).items()}
                self._overrides[mode] = {
                    "context": {k: v for k, v in context.items() if v is not None},
                    "params": {k: v for k, v in params.items() if v is not None},
                }
            if isinstance(stored.get("prompt_extra"), dict):
                for mode in MODES:
                    for loc in LOCALES:
                        value = (stored["prompt_extra"].get(mode) or {}).get(loc, "")
                        try:
                            self._prompt_extra[mode][loc] = self._check_prompt_extra(mode, loc, value)
                        except RuntimeConfigError:
                            LOG.warning("prompt_extra_load_failed", extra={"status": "failed", "error_code": "invalid"})
            if isinstance(stored.get("prompt_extra_history"), list):
                self._prompt_extra_history = [
                    item for item in stored["prompt_extra_history"]
                    if isinstance(item, dict) and isinstance(item.get("id"), int)
                ][-PROMPT_EXTRA_HISTORY_LIMIT:]
            if isinstance(stored.get("answer_style"), dict):
                try:
                    self._answer_style = answer_style.check(stored["answer_style"])
                except answer_style.AnswerStyleError as exc:
                    LOG.warning("answer_style_load_failed", extra={"status": "failed", "error_code": type(exc).__name__})
        except (OSError, ValueError, AttributeError) as exc:
            # 깨진 파일 때문에 기동이 멈추면 안 된다. 다만 조용히 넘기지 않고 남긴다 —
            # 화면 값이 사라진 이유를 찾을 수 있어야 한다.
            LOG.warning("runtime_config_load_failed", extra={"status": "failed", "error_code": type(exc).__name__})
            self._overrides = {mode: {"context": {}, "params": {}} for mode in MODES}

    def _save(self, snapshot: dict[str, Any]) -> None:
        if not self._path:
            return
        directory = os.path.dirname(self._path) or "."
        temp = os.path.join(directory, f".{os.path.basename(self._path)}.tmp")
        try:
            os.makedirs(directory, exist_ok=True)
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            os.replace(temp, self._path)
        except OSError as exc:
            # 저장에 실패해도 이번 기동 동안은 메모리 값이 적용된다. 화면이 persisted 로 그 사실을 본다.
            LOG.warning("runtime_config_save_failed", extra={"status": "failed", "error_code": type(exc).__name__})
            raise RuntimeConfigError("failed to persist runtime config") from exc
