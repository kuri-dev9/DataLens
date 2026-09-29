"""답변 근거 검증기 1단계 — 재기만 하고 자르지 않는다 (ADR-034).

최종 answer의 수치가 이번 턴에 모델이 실제로 받은 사실에 있었는지 대조해 지표만 남긴다.
폐기는 2단계이며, 임계값은 여기서 모은 분포를 보고 정한다.
"""

from __future__ import annotations

import logging
import re
from typing import Any


LOG = logging.getLogger("datalens.grounding")

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
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
        number = float(node)
        sink.add(f"{number:g}")
        sink.add(f"{round(number):g}")
        sink.add(f"{number:.1f}")
        sink.add(f"{number:.2f}")
        sink.add(f"{abs(number):g}")
        sink.add(f"{abs(number):.1f}")
        sink.add(f"{abs(number):.2f}")
    elif isinstance(node, str):
        sink.update(numbers_in(node))


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


def measure(answer: str, facts: set[str]) -> dict[str, Any]:
    """answer의 수치를 근거 집합과 대조한다. answer는 건드리지 않는다."""
    found = numbers_in(answer)
    allowed_floats = [parsed for parsed in (_as_float(item) for item in facts) if parsed is not None]

    ungrounded: list[dict[str, Any]] = []
    grounded = 0
    for token in found:
        if token in facts:
            grounded += 1
            continue
        value = _as_float(token)
        if value is None:
            grounded += 1
            continue
        nearest, deviation = _nearest(value, allowed_floats)
        ungrounded.append(
            {
                "value": token,
                "nearest": nearest,
                "deviation_pct": deviation,
                "small_int": value.is_integer() and abs(value) <= _SMALL_INT_LIMIT,
            }
        )

    total = len(found)
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
