"""graphio 검증기 재현 — evidence 모드 답의 어느 줄을 graphio 가 버렸는지 (2026-11-04).

graphio 는 모델 답(`answer_ko`)을 줄 단위로 나눠 두 가지를 검사하고 걸린 줄을 **버린다**.
  1. 증거팩에 없는 수치가 있는가
  2. 미연동 데이터를 단정하는 표현("때문입니다" 등)이 있는가
DataLens 는 그 판정 결과를 돌려받지 않는다. 그래서 시연 화면(/ui 최근 전송 원문)에서
"무엇이 버려졌는가"를 보이려고, 보낸 프롬프트의 증거팩과 받은 원문으로 같은 검사를 다시 돌린다.

**정본은 graphio `src/graphio/rca/llm.py`** (`_ASSERTIVE` · `_number_ok` · `_pack_numbers` ·
`validate` 의 ask 경로). 규칙을 여기 옮겨 적었으므로 두 곳이 갈라질 수 있다 —
**시연 뒤 graphio 가 판정을 DataLens 에 보내도록 바꾸고 이 모듈은 그 값으로 교체한다.**

재현이 graphio 와 다를 수 있는 자리:
  * **수치는 확정할 수 없다.** graphio 는 프롬프트에 실은 부분이 아니라 **전체 증거팩**으로
    허용 수치를 만든다(예산에 밀려 생략된 증거·행도 포함). DataLens 는 보낸 부분만 안다.
    그래서 보낸 부분에 없는 수치는 `suspect`(graphio 는 통과시켰을 수 있음)로 두고 버림에 세지 않는다.
    실측: 11-04 15:19 APN 사건 답의 `13109` 는 보낸 증거에 없었지만 graphio 는 그 줄을 남겼다.
  * 단정 표현은 증거팩과 무관하므로 graphio 와 같다 — `dropped` 는 이것만 확정이다.
  * 증거팩 시간창(pack.window)은 프롬프트에 없어 허용 시각에서 빠진다.
  * 이어 묻기(compact)는 증거팩이 앞 메시지에 있다 — 보낸 메시지 전체에서 증거팩을 찾는다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

# ── graphio llm.py 에서 옮긴 규칙 (정본: llm.py:574~600 · 661~688 · 1344) ──────────
_DT = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"
    r"|\d{1,2}:\d{2}(?::\d{2})?"
)
_NUM = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")
_ASSERTIVE = (
    "변경 때문", "작업 때문", "때문입니다", "확실합니다", "틀림없", "임이 분명",
    "파라미터 변경으로 인해", "구성 변경으로 인해",
)
# 식별자 하이픈 예외 — `EV-01` · `C-02` · `SGW-108은` 의 하이픈을 음수 부호로 읽지 않는다
_CODE = re.compile(r"(?<![\w-])[A-Za-z][A-Za-z0-9_]*-\d+(?!\d)")
_FORMAT_TOKEN = re.compile(r"\s*[\(\[]?\s*unanswerable\s*[=:]\s*(true|false)\s*[\)\]]?", re.I)

PACK_MARK = "증거팩:\n"


def _numbers_in(text: str) -> set[str]:
    masked = _CODE.sub(" ", _DT.sub(" ", text))
    return {m.group().replace(",", "") for m in _NUM.finditer(masked)}


def _datetimes_in(text: str) -> set[str]:
    return {re.sub(r"\D", "", m.group()) for m in _DT.finditer(text)}


def _is_num(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _walk(node: Any, numbers: set[str], datetimes: set[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _walk(value, numbers, datetimes)
    elif isinstance(node, list):
        for value in node:
            _walk(value, numbers, datetimes)
    elif isinstance(node, bool):
        return
    elif isinstance(node, (int, float)):
        f = float(node)
        numbers.update({f"{f:g}", f"{round(f):g}", f"{f:.1f}", f"{f:.2f}", f"{abs(f):g}", f"{abs(f):.1f}"})
    elif isinstance(node, str):
        numbers.update(_numbers_in(node))
        datetimes.update(_datetimes_in(node))


def _number_ok(text: str, allowed: set[str], allowed_dt: set[str]) -> list[str]:
    bad = []
    for d in _datetimes_in(text) if allowed_dt else ():
        if len(d) >= 4 and not any(d in a for a in allowed_dt):
            bad.append(d)
    floats = [float(a) for a in allowed if _is_num(a)]
    for n in _numbers_in(text):
        if n in allowed:
            continue
        try:
            f = float(n)
        except ValueError:
            continue
        if f.is_integer() and abs(f) <= 12:
            continue
        if any(abs(f - a) < 0.06 for a in floats):
            continue
        bad.append(n)
    return bad


# ── DataLens 쪽 — 프롬프트·원문에서 재료를 꺼낸다 ───────────────────────────────

def _first_json(text: str, start: int) -> Any:
    try:
        value, _ = json.JSONDecoder().raw_decode(text, start)
        return value
    except ValueError:
        return None


def pack_items(contents: Iterable[str]) -> list[dict[str, Any]]:
    """보낸 메시지들에서 증거팩의 evidence 항목을 모은다 (graphio `_assemble` 모양)."""
    items: dict[str, dict[str, Any]] = {}
    for content in contents:
        at = content.find(PACK_MARK)
        while at >= 0:
            body = _first_json(content, at + len(PACK_MARK))
            if isinstance(body, dict):
                for item in body.get("evidence") or []:
                    if isinstance(item, dict) and "id" in item:
                        items[str(item["id"])] = item
            at = content.find(PACK_MARK, at + 1)
    return list(items.values())


def answer_text(raw: str) -> tuple[str, bool]:
    """받은 원문에서 `answer_ko` 를 꺼낸다. 못 꺼내면 (원문, False)."""
    start = raw.find("{")
    while start >= 0:
        data = _first_json(raw, start)
        if isinstance(data, dict) and isinstance(data.get("answer_ko"), str):
            return data["answer_ko"], True
        start = raw.find("{", start + 1)
    return raw, False


def check(contents: Iterable[str], raw_answer: str) -> dict[str, Any]:
    """줄마다 kept/dropped 와 이유. graphio `validate`(ask 경로)와 같은 순서로 본다."""
    items = pack_items(contents)
    numbers: set[str] = set()
    datetimes: set[str] = set()
    _walk({item["id"]: item.get("data") for item in items}, numbers, datetimes)
    answer, parsed = answer_text(raw_answer)
    answer = _FORMAT_TOKEN.sub("", answer)
    lines = []
    for line in [ln.strip() for ln in answer.split("\n") if ln.strip()]:
        # graphio 는 수치 검사를 먼저 하지만, 여기서는 확정 판정(단정 표현)을 먼저 본다 —
        # 수치는 재현이 불확실해(머리말) 단정 표현이 같은 줄에 있으면 그쪽이 버린 이유다.
        hit = next((tok for tok in _ASSERTIVE if tok in line), None)
        bad = _number_ok(line, numbers, datetimes)
        if hit:
            lines.append({"text": line, "status": "dropped", "reason": f"단정 표현: {hit}"})
        elif bad:
            lines.append({"text": line, "status": "suspect",
                          "reason": f"보낸 증거팩에 없는 수치: {', '.join(bad)} — graphio 는 전체 증거팩으로 보므로 통과했을 수 있다"})
        else:
            lines.append({"text": line, "status": "kept", "reason": None})
    return {
        "source": "graphio 검증 재현",
        "parsed": parsed,
        "pack_items": len(items),
        "lines": lines,
        "dropped": sum(1 for line in lines if line["status"] == "dropped"),
        "suspect": sum(1 for line in lines if line["status"] == "suspect"),
    }


def split(result: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """검사 결과를 (버린 줄 목록, 최종 답) 으로 — evidence_turns 의 dropped · final 칸.

    `sure` 가 거짓인 줄(보낸 증거팩에 없는 수치)은 graphio 가 남겼을 수 있어 최종 답에 둔다.
    """
    dropped, final = [], []
    for line in result.get("lines") or []:
        if line["status"] == "kept":
            final.append(line["text"])
            continue
        sure = line["status"] == "dropped"
        dropped.append({"line": line["text"], "reason": line["reason"], "sure": sure})
        if not sure:
            final.append(line["text"])
    return dropped, "\n".join(final)
