from __future__ import annotations

from typing import Protocol, Sequence

from datalens.domain.memory import GlossaryEntry, PlanStep, Recipe


class EmbeddingUnavailable(RuntimeError):
    pass


class Embedder(Protocol):
    async def embed(self, text: str, timeout_seconds: float) -> tuple[float, ...]:
        """단위 벡터를 돌려준다. 실패하면 EmbeddingUnavailable."""

    async def ready(self, timeout_seconds: float) -> bool: ...
    async def close(self) -> None: ...


class MemoryStore(Protocol):
    def search_recipes(
        self, embedding: Sequence[float], locale: str, *, limit: int, threshold: float
    ) -> tuple[tuple[Recipe, float], ...]: ...

    def search_terms(
        self, embedding: Sequence[float], locale: str, *, limit: int, threshold: float
    ) -> tuple[tuple[GlossaryEntry, float], ...]: ...

    def upsert_recipe(
        self,
        question: str,
        locale: str,
        steps: Sequence[PlanStep],
        embedding: Sequence[float],
        *,
        merge_threshold: float,
    ) -> Recipe: ...

    def record_reuse(self, recipe_ids: Sequence[int], *, succeeded: bool) -> None: ...

    def list_recipes(self, locale: str | None = None, *, limit: int) -> tuple[Recipe, ...]: ...
    def delete_recipe(self, recipe_id: int) -> bool: ...

    def upsert_term(
        self,
        term: str,
        locale: str,
        embedding: Sequence[float],
        *,
        table: str | None,
        columns: Sequence[str],
        note: str | None,
        source: str,
    ) -> GlossaryEntry: ...

    def list_terms(self, locale: str | None = None, *, limit: int) -> tuple[GlossaryEntry, ...]: ...
    def delete_term(self, term_id: int) -> bool: ...

    def prune(self, max_recipes: int) -> int: ...
    def close(self) -> None: ...
