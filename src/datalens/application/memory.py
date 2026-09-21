from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from datalens.domain.memory import GlossaryEntry, PlanStep, Recall, Recipe
from datalens.ports.memory import Embedder, EmbeddingUnavailable, MemoryStore


LOG = logging.getLogger("datalens.memory")


class MemoryService:
    """과거에 통한 풀이를 찾아 주입하고, 성공한 풀이를 되돌려 저장한다.

    기억은 거들 뿐이다. 임베딩이나 저장소가 죽어도 턴은 그대로 진행되어야 하므로
    모든 경로에서 실패를 삼키고 경고만 남긴다.
    """

    def __init__(
        self,
        store: MemoryStore,
        embedder: Embedder,
        *,
        recipe_limit: int = 3,
        term_limit: int = 5,
        threshold: float = 0.55,
        merge_threshold: float = 0.93,
        max_recipes: int = 500,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._recipe_limit = recipe_limit
        self._term_limit = term_limit
        self._threshold = threshold
        self._merge_threshold = merge_threshold
        self._max_recipes = max_recipes
        self._timeout = timeout_seconds

    async def recall(self, question: str, locale: str) -> Recall:
        embedding = await self._embed(question)
        if embedding is None:
            return Recall()
        try:
            recipes = self._store.search_recipes(
                embedding, locale, limit=self._recipe_limit, threshold=self._threshold
            )
            terms = self._store.search_terms(
                embedding, locale, limit=self._term_limit, threshold=self._threshold
            )
        except Exception:
            LOG.warning("memory_search_failed", extra={"operation": "recall", "status": "failed"})
            return Recall()
        return Recall(recipes=recipes, terms=terms)

    async def record_success(self, question: str, locale: str, steps: Sequence[PlanStep]) -> Recipe | None:
        if not steps:
            return None
        embedding = await self._embed(question)
        if embedding is None:
            return None
        try:
            recipe = self._store.upsert_recipe(
                question, locale, steps, embedding, merge_threshold=self._merge_threshold
            )
            self._store.prune(self._max_recipes)
            return recipe
        except Exception:
            LOG.warning("memory_record_failed", extra={"operation": "record", "status": "failed"})
            return None

    def record_reuse(self, recall: Recall, *, succeeded: bool) -> None:
        if not recall.recipes:
            return
        try:
            self._store.record_reuse([recipe.recipe_id for recipe, _ in recall.recipes], succeeded=succeeded)
        except Exception:
            LOG.warning("memory_feedback_failed", extra={"operation": "reuse", "status": "failed"})

    async def upsert_term(
        self,
        term: str,
        locale: str,
        *,
        table: str | None,
        columns: Sequence[str],
        note: str | None,
        source: str = "manual",
    ) -> GlossaryEntry | None:
        embedding = await self._embed(term)
        if embedding is None:
            return None
        try:
            return self._store.upsert_term(
                term, locale, embedding, table=table, columns=list(columns), note=note, source=source
            )
        except Exception:
            LOG.warning("memory_term_failed", extra={"operation": "upsert_term", "status": "failed"})
            return None

    def list_recipes(self, locale: str | None, *, limit: int) -> tuple[Recipe, ...]:
        return self._store.list_recipes(locale, limit=limit)

    def delete_recipe(self, recipe_id: int) -> bool:
        return self._store.delete_recipe(recipe_id)

    def list_terms(self, locale: str | None, *, limit: int) -> tuple[GlossaryEntry, ...]:
        return self._store.list_terms(locale, limit=limit)

    def delete_term(self, term_id: int) -> bool:
        return self._store.delete_term(term_id)

    async def ready(self, timeout_seconds: float) -> bool:
        try:
            return await self._embedder.ready(timeout_seconds)
        except Exception:
            return False

    async def close(self) -> None:
        try:
            await self._embedder.close()
        finally:
            self._store.close()

    async def _embed(self, text: str) -> tuple[float, ...] | None:
        stripped = text.strip()
        if not stripped:
            return None
        try:
            return await self._embedder.embed(stripped, self._timeout)
        except EmbeddingUnavailable:
            LOG.warning("memory_embedding_unavailable", extra={"operation": "embed", "status": "failed"})
            return None
        except Exception:
            LOG.warning("memory_embedding_failed", extra={"operation": "embed", "status": "failed"})
            return None


def render_hints(recall: Recall) -> str | None:
    """회상 결과를 모델이 읽을 한 덩어리로 만든다. 그대로 베끼지 말라고 못박는다."""
    if not recall:
        return None
    lines: list[str] = []
    if recall.terms:
        lines.append("업무 용어 매핑:")
        for entry, _ in recall.terms:
            target = entry.table or "?"
            if entry.columns:
                target = f"{target}.{'/'.join(entry.columns)}"
            note = f" ({entry.note})" if entry.note else ""
            lines.append(f"- {entry.term} → {target}{note}")
    if recall.recipes:
        if lines:
            lines.append("")
        lines.append("과거에 성공한 풀이입니다. 현재 질문에 맞게 기간과 대상을 조정해서 쓰세요.")
        lines.append("스키마가 바뀌었을 수 있으니 결과가 어긋나면 schema로 확인하세요.")
        for recipe, score in recall.recipes:
            lines.append(f'- "{recipe.question}" (유사도 {score:.2f}, 재사용 {recipe.uses}회)')
            for step in recipe.steps:
                lines.append(f"    {step.tool}: {json.dumps(step.intent, ensure_ascii=False, separators=(',', ':'))}")
    return "\n".join(lines) if lines else None


def plan_from_events(steps: Sequence[dict[str, Any]]) -> tuple[PlanStep, ...]:
    return tuple(
        PlanStep(str(item["tool"]), dict(item.get("intent") or {}))
        for item in steps
        if isinstance(item, dict) and item.get("tool")
    )
