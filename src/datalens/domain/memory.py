from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PlanStep:
    """성공한 도구 호출 하나. 행 데이터는 담지 않고 조회 대상만 남긴다."""

    tool: str
    intent: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {"tool": self.tool, "intent": dict(self.intent)}


@dataclass(frozen=True, slots=True)
class Recipe:
    """질문 하나를 끝까지 풀어낸 도구 호출 시퀀스."""

    recipe_id: int
    question: str
    locale: str
    steps: tuple[PlanStep, ...]
    tables: tuple[str, ...] = ()
    uses: int = 0
    successes: int = 0

    @property
    def weight(self) -> float:
        # 한 번도 재사용되지 않은 사례는 0.5에서 출발해 실제 성적으로 수렴한다.
        return (self.successes + 1) / (self.uses + 2)

    def public(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "question": self.question,
            "locale": self.locale,
            "steps": [step.public() for step in self.steps],
            "tables": list(self.tables),
            "uses": self.uses,
            "successes": self.successes,
            "weight": round(self.weight, 3),
        }


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    """업무 용어와 스키마의 매핑. 사람이 고칠 수 있어야 한다."""

    term_id: int
    term: str
    locale: str
    table: str | None = None
    columns: tuple[str, ...] = ()
    note: str | None = None
    source: str = "learned"

    def public(self) -> dict[str, Any]:
        return {
            "term_id": self.term_id,
            "term": self.term,
            "locale": self.locale,
            "table": self.table,
            "columns": list(self.columns),
            "note": self.note,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class Recall:
    """한 번의 회상 결과. 무엇을 근거로 주입했는지 그대로 보여줄 수 있어야 한다."""

    recipes: tuple[tuple[Recipe, float], ...] = ()
    terms: tuple[tuple[GlossaryEntry, float], ...] = ()

    def __bool__(self) -> bool:
        return bool(self.recipes or self.terms)

    def public(self) -> dict[str, Any]:
        return {
            "recipes": [{**recipe.public(), "score": round(score, 3)} for recipe, score in self.recipes],
            "terms": [{**entry.public(), "score": round(score, 3)} for entry, score in self.terms],
        }
