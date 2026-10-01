"""답변 근거 검증기 1단계 — 재기만 하고 자르지 않는다 (ADR-034).

최종 answer의 수치가 이번 턴에 모델이 실제로 받은 사실에 있었는지 대조해 지표만 남긴다.
폐기는 2단계이며, 임계값은 여기서 모은 분포를 보고 정한다.
"""

from __future__ import annotations

import logging
import re
from typing import Any


LOG = logging.getLogger("datalens.grounding")

# 하이픈 앞에 숫자가 오면 음수 부호가 아니라 구분자다. 이걸 놓치면
# "2024-05-30"이 [2024, -05, -30]으로 쪼개져 날짜가 통째로 미검증이 된다.
_NUM = re.compile(r"(?<!\d)-?\d+(?:\.\d+)?")
# 날짜는 성분으로 쪼개지 않고 하나의 주장으로 센다. "2024년 5월 30일"이 3건으로
# 분해되면 반복 인용 한 번에 비율이 왜곡된다(실측 2026-10-01: ungrounded 9건 중
# 6건이 단일 날짜 주장의 반복 분해였다).
_DATE = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|(?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일"
)
# 천 단위 콤마 표기. 이걸 모르면 "41,747,290"이 [41, 747, 290] 3건의 미검증으로
# 쪼개진다(실측 2026-10-01). 콤마를 벗긴 값으로 근거 대조하고, 표기는 원문대로 남긴다.
_GROUPED = re.compile(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d)")
# 스텝 번호·목록 번호처럼 본문 구조에서 나오는 작은 정수. 1단계에서는 세되 표시만 한다.
_SMALL_INT_LIMIT = 12


def numbers_in(text: str) -> list[str]:
    return [match.group() for match in _NUM.finditer(text)]


def collect_facts(node: Any, sink: set[str]) -> None:
    """모델이 받은 객체를 훑어 수치를 집합에 넣는다.

    같은 값이라도 표기가 갈리므로(455.10999999999996 ↔ 455.11) 포맷 변형을 모두 등록한다.
    """
    if isinstance(node, dict):
        for value in node.values():
            collect_facts(value, sink)
    elif isinstance(node, (list, tuple)):
        for value in node:
            collect_facts(value, sink)
    elif isinstance(node, bool):
        return
    elif isinstance(node, (int, float)):
        _register(float(node), sink)
    elif isinstance(node, str):
        rest = node
        for match in _GROUPED.finditer(node):
            parsed = _as_float(match.group().replace(",", ""))
            if parsed is not None:
                _register(parsed, sink)
        rest = _GROUPED.sub(lambda match: " " * len(match.group()), rest)
        for token in numbers_in(rest):
            sink.add(token)
            parsed = _as_float(token)
            if parsed is not None:
                # "05"와 "5"는 같은 값이다. 문자열에서 뽑은 숫자도 변형을 등록해야
                # 날짜·코드 표기가 답변의 자연스러운 표기와 어긋나지 않는다.
                _register(parsed, sink)


def _register(number: float, sink: set[str]) -> None:
    """같은 값의 표기 변형을 모두 등록한다 (455.10999999999996 ↔ 455.11)."""
    sink.add(f"{number:g}")
    sink.add(f"{round(number):g}")
    sink.add(f"{number:.1f}")
    sink.add(f"{number:.2f}")
    sink.add(f"{abs(number):g}")
    sink.add(f"{abs(number):.1f}")
    sink.add(f"{abs(number):.2f}")


def _as_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _nearest(value: float, allowed: list[float]) -> tuple[float | None, float | None]:
    """최근접 근거값과 편차 %. 0.01%면 반올림 오차, 40%면 지어낸 값이다."""
    if not allowed:
        return None, None
    nearest = min(allowed, key=lambda candidate: abs(candidate - value))
    scale = max(abs(nearest), abs(value))
    if scale == 0:
        return nearest, 0.0
    return nearest, round(abs(nearest - value) / scale * 100, 4)


def _claims_in(answer: str) -> list[dict[str, Any]]:
    """answer에서 주장(claim)을 뽑는다. 날짜 한 덩어리가 주장 하나다."""
    claims: list[dict[str, Any]] = []
    for match in _DATE.finditer(answer):
        claims.append(
            {"kind": "date", "value": match.group().strip(), "components": numbers_in(match.group()), "pos": match.start()}
        )
    # 날짜 구간을 같은 길이의 공백으로 치우고 남은 숫자만 일반 수치 주장으로 센다.
    rest = _DATE.sub(lambda match: " " * len(match.group()), answer)
    # 천 단위 콤마 수치는 콤마를 벗긴 값으로 대조하되 표기는 원문대로 남긴다.
    for match in _GROUPED.finditer(rest):
        claims.append(
            {"kind": "number", "value": match.group(), "normalized": match.group().replace(",", ""), "pos": match.start()}
        )
    rest = _GROUPED.sub(lambda match: " " * len(match.group()), rest)
    for match in _NUM.finditer(rest):
        claims.append({"kind": "number", "value": match.group(), "pos": match.start()})
    claims.sort(key=lambda claim: claim["pos"])
    return claims


def _claim_key(claim: dict[str, Any]) -> tuple[Any, ...]:
    """동일 값의 반복 인용은 주장 하나다. "2024-05-30"과 "2024년 5월 30일"도 같은 주장이다."""
    if claim["kind"] == "date":
        return ("date", *(_normal(token) for token in claim["components"]))
    return ("number", _normal(claim.get("normalized") or claim["value"]))


def _normal(token: str) -> str:
    parsed = _as_float(token)
    return f"{parsed:g}" if parsed is not None else token


def _in_facts(token: str, facts: set[str]) -> bool:
    return token in facts or _normal(token) in facts


def measure(answer: str, facts: set[str]) -> dict[str, Any]:
    """answer의 수치 주장을 근거 집합과 대조한다. answer는 건드리지 않는다.

    계수 단위는 주장(claim)이다: 같은 값의 반복 인용은 1건, 날짜는 성분을 묶어 1건.
    """
    allowed_floats = [parsed for parsed in (_as_float(item) for item in facts) if parsed is not None]

    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for claim in _claims_in(answer):
        key = _claim_key(claim)
        if key in unique:
            unique[key]["occurrences"] += 1
            continue
        claim["occurrences"] = 1
        unique[key] = claim

    ungrounded: list[dict[str, Any]] = []
    grounded = 0
    for claim in unique.values():
        if claim["kind"] == "date":
            missing = [token for token in claim["components"] if not _in_facts(token, facts)]
            if not missing:
                grounded += 1
                continue
            ungrounded.append(
                {
                    "value": claim["value"],
                    "date": True,
                    "missing": missing,
                    "nearest": None,
                    "deviation_pct": None,
                    "small_int": False,
                    "occurrences": claim["occurrences"],
                }
            )
            continue
        token = claim.get("normalized") or claim["value"]
        value = _as_float(token)
        if _in_facts(token, facts) or value is None:
            grounded += 1
            continue
        nearest, deviation = _nearest(value, allowed_floats)
        ungrounded.append(
            {
                "value": claim["value"],
                "nearest": nearest,
                "deviation_pct": deviation,
                "small_int": value.is_integer() and abs(value) <= _SMALL_INT_LIMIT,
                "occurrences": claim["occurrences"],
            }
        )

    total = len(unique)
    deviations = [item["deviation_pct"] for item in ungrounded if item["deviation_pct"] is not None]
    return {
        "total": total,
        "grounded": grounded,
        "grounded_ratio": round(grounded / total, 3) if total else None,
        "ungrounded_ratio_pct": round(len(ungrounded) / total * 100, 2) if total else 0.0,
        "max_deviation_pct": max(deviations) if deviations else None,
        "ungrounded": ungrounded,
    }


def safe_measure(answer: str, facts: set[str]) -> dict[str, Any] | None:
    """계측이 턴을 깨뜨리지 않는다 (GRD-AC-6)."""
    try:
        return measure(answer, facts)
    except Exception:
        LOG.warning("grounding_measure_failed", extra={"operation": "grounding", "status": "failed"})
        return None
