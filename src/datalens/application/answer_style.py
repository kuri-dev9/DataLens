"""답변 스타일 — graphio 질의의 `[답변 형식]` 구역만 바꿔 끼운다 (2026-11-04).

graphio 는 AI 질의 지시 안에서 형식·길이 규칙(규칙 8)을 `[답변 형식]`…`[/답변 형식]` 으로 감싸
보낸다(graphio `rca/llm.py` `_answer_format`). 구역 밖(증거 규칙)은 graphio 검증기와 짝이라
건드리지 않는다 — 여기서 고르는 것은 형식과 길이뿐이다.

* DataLens 에는 system 역할이 없어 graphio 가 지시와 질문을 **사용자 메시지 하나**로 합쳐 보낸다.
  그래서 바꿔 끼우는 자리는 이번 메시지 본문이다.
* **RCA 응답의 답변 형식은 여기 값이 정본이다**(2026-11-04 사용자 "꺼짐인데 실제로는 들어가 있다").
  예전의 `enabled`(바꿔 끼울지) 스위치는 없앴다 — "꺼짐"이 "형식 없음"이 아니라 "graphio 기본값
  그대로"라는 뜻이라 화면과 실제가 다르게 읽혔다. 기본값은 graphio 기본값(6~15문장·마크다운)과 같고,
  graphio 의 구역은 DataLens 없이 모델에 바로 붙을 때만 쓰이는 비상값이다.
* 표지가 한 쌍이 아니면 **아무것도 바꾸지 않는다.** 이어 묻기는 질문만 보내므로 표지가 없고,
  첫 질문의 형식을 따라간다.
* 표지 문자열은 graphio `ANSWER_FORMAT_OPEN/CLOSE` 와 같아야 한다. 저쪽을 바꾸면 여기도 바꾼다.
"""

from __future__ import annotations

from typing import Any

OPEN, CLOSE = "[답변 형식]", "[/답변 형식]"

DEFAULTS: dict[str, Any] = {
    "min_sentences": 6,
    "max_sentences": 15,
    "max_chars": None,
    "markdown": True,
    "extra": "",
}

RETIRED = frozenset({"enabled"})


class AnswerStyleError(ValueError):
    pass


def check(values: dict[str, Any]) -> dict[str, Any]:
    """부분 갱신 값을 검사해 전체 설정으로 돌려준다(빠진 항목은 기본값)."""
    # 없앤 스위치 — runtime.json 에 남았거나 옛 화면이 보내도 거부하지 않고 버린다
    values = {k: v for k, v in values.items() if k not in RETIRED}
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise AnswerStyleError(f"unknown answer_style field: {sorted(unknown)[0]}")
    style = {**DEFAULTS, **values}
    if not isinstance(style["markdown"], bool):
        raise AnswerStyleError("markdown must be boolean")
    lo, hi = style["min_sentences"], style["max_sentences"]
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (lo, hi)) or not 1 <= lo <= hi <= 50:
        raise AnswerStyleError("sentences must satisfy 1 <= min <= max <= 50")
    cap = style["max_chars"]
    if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or not 100 <= cap <= 10000):
        raise AnswerStyleError("max_chars must be empty or 100..10000")
    extra = style["extra"]
    if not isinstance(extra, str) or len(extra) > 300 or "\n" in extra:
        raise AnswerStyleError("extra must be one line of at most 300 chars")
    # 추가 지시가 표지를 담으면 다음 요청에서 구역이 둘로 보인다
    if OPEN in extra or CLOSE in extra:
        raise AnswerStyleError("extra must not contain the format markers")
    style["extra"] = extra.strip()
    return style


def _lang(message: str) -> str:
    # graphio 는 ja 요청이면 규칙 5 를 "**일본어**로" 로 바꾼다
    return "ja" if "**일본어**로" in message else "ko"


def block(style: dict[str, Any], lang: str = "ko") -> str:
    lo, hi = style["min_sentences"], style["max_sentences"]
    lines = [OPEN]
    if style["markdown"]:
        # graphio `llm._answer_format` 과 같은 문구 — 순서만 주고 틀은 주지 않는다(2026-11-07)
        lines += [
            f"8. 답은 {lo}~{hi}문장으로 쓴다. 첫 문장에서 결론을 말하고, 왜 그렇게 판단했는지, 영향이 "
            "어디까지인지, 무엇을 확인·조치할지를 운영자에게 말하듯 이어 쓴다.",
            "   마크다운은 읽기 좋을 때 쓴다. 제목·목록·굵게·표를 쓸 수 있고 정해진 틀은 없다. "
            "HTML 과 링크는 쓰지 않는다.",
        ]
    else:
        lines += [
            f"8. 답은 {lo}~{hi}문장의 평문 문단으로 쓴다. 결론 → 근거 → 영향 범위 → 확인·조치 순서로 쓰고 "
            "문단은 줄바꿈으로 나눈다.",
            "   마크다운 기호(#, -, *, |, `)와 HTML·링크는 쓰지 않는다. 문장마다 근거 증거 ID 를 괄호로 붙인다.",
        ]
    if style["max_chars"]:
        lines.append(f"   답 전체는 {style['max_chars']}자를 넘기지 않는다.")
    if style["extra"]:
        lines.append(f"   {style['extra']}")
    lines.append(CLOSE)
    return "\n".join(lines)


def apply(message: str, style: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """(보낼 메시지, 기록용 style) — 바꾸지 않으면 메시지를 그대로 돌려준다."""
    style = {**DEFAULTS, **{k: v for k, v in (style or {}).items() if k not in RETIRED}}
    record: dict[str, Any] = {"applied": False, "settings": style}
    start, end = message.find(OPEN), message.find(CLOSE)
    if start < 0 or end < start or message.count(OPEN) != 1 or message.count(CLOSE) != 1:
        record["reason"] = "표지 없음"
        return message, record
    lang = _lang(message)
    replaced = block(style, lang)
    original = message[start:end + len(CLOSE)]
    record.update({"applied": True, "lang": lang, "original": original, "block": replaced, "same": original == replaced})
    return message[:start] + replaced + message[end + len(CLOSE):], record
