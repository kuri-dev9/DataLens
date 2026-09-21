from __future__ import annotations

import json
import sqlite3
import struct
from datetime import UTC, datetime
from operator import mul
from pathlib import Path
from typing import Any, Iterable, Sequence

from datalens.domain.memory import GlossaryEntry, PlanStep, Recipe


SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    recipe_id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    locale TEXT NOT NULL,
    steps TEXT NOT NULL,
    tables TEXT NOT NULL DEFAULT '',
    embedding BLOB NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recipes_locale ON recipes(locale);

CREATE TABLE IF NOT EXISTS glossary (
    term_id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL,
    locale TEXT NOT NULL,
    table_name TEXT,
    columns TEXT NOT NULL DEFAULT '',
    note TEXT,
    source TEXT NOT NULL DEFAULT 'learned',
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(term, locale)
);
CREATE INDEX IF NOT EXISTS glossary_locale ON glossary(locale);
"""


def pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> tuple[float, ...]:
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def similarity(left: Sequence[float], right: Sequence[float]) -> float:
    # 저장 시 단위 벡터로 정규화했으므로 내적이 곧 코사인 유사도다.
    if len(left) != len(right):
        return -1.0
    return sum(map(mul, left, right))


class SqliteMemoryStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self._path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def __repr__(self) -> str:
        return f"SqliteMemoryStore(path={str(self._path)!r})"

    # ---- 회상 -------------------------------------------------------------

    def search_recipes(
        self, embedding: Sequence[float], locale: str, *, limit: int, threshold: float
    ) -> tuple[tuple[Recipe, float], ...]:
        rows = self._connection.execute(
            "SELECT * FROM recipes WHERE locale = ?", (locale,)
        ).fetchall()
        scored = self._rank(rows, embedding, threshold, self._recipe)
        # 유사도만 보면 한 번 쓰이고 실패한 사례가 계속 올라온다. 성적으로 눌러준다.
        scored.sort(key=lambda item: item[1] * item[0].weight, reverse=True)
        return tuple(scored[:limit])

    def search_terms(
        self, embedding: Sequence[float], locale: str, *, limit: int, threshold: float
    ) -> tuple[tuple[GlossaryEntry, float], ...]:
        rows = self._connection.execute(
            "SELECT * FROM glossary WHERE locale = ?", (locale,)
        ).fetchall()
        scored = self._rank(rows, embedding, threshold, self._term)
        scored.sort(key=lambda item: item[1], reverse=True)
        return tuple(scored[:limit])

    @staticmethod
    def _rank(rows: Iterable[sqlite3.Row], embedding: Sequence[float], threshold: float, build) -> list:
        scored = []
        for row in rows:
            score = similarity(embedding, unpack(row["embedding"]))
            if score >= threshold:
                scored.append((build(row), score))
        return scored

    # ---- 기록 -------------------------------------------------------------

    def upsert_recipe(
        self,
        question: str,
        locale: str,
        steps: Sequence[PlanStep],
        embedding: Sequence[float],
        *,
        merge_threshold: float,
    ) -> Recipe:
        payload = json.dumps([step.public() for step in steps], ensure_ascii=False)
        tables = ",".join(sorted({table for step in steps if (table := step.intent.get("table")) and isinstance(table, str)}))
        now = datetime.now(UTC).isoformat()
        existing = self._closest_recipe(embedding, locale, merge_threshold)
        if existing is not None:
            self._connection.execute(
                "UPDATE recipes SET question = ?, steps = ?, tables = ?, embedding = ?, last_used_at = ? WHERE recipe_id = ?",
                (question, payload, tables, pack(embedding), now, existing),
            )
            self._connection.commit()
            return self.get_recipe(existing)  # type: ignore[return-value]
        cursor = self._connection.execute(
            "INSERT INTO recipes (question, locale, steps, tables, embedding, created_at, last_used_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (question, locale, payload, tables, pack(embedding), now, now),
        )
        self._connection.commit()
        return self.get_recipe(int(cursor.lastrowid))  # type: ignore[return-value]

    def _closest_recipe(self, embedding: Sequence[float], locale: str, threshold: float) -> int | None:
        best_id, best_score = None, threshold
        for row in self._connection.execute("SELECT recipe_id, embedding FROM recipes WHERE locale = ?", (locale,)):
            score = similarity(embedding, unpack(row["embedding"]))
            if score >= best_score:
                best_id, best_score = int(row["recipe_id"]), score
        return best_id

    def record_reuse(self, recipe_ids: Sequence[int], *, succeeded: bool) -> None:
        if not recipe_ids:
            return
        now = datetime.now(UTC).isoformat()
        self._connection.executemany(
            "UPDATE recipes SET uses = uses + 1, successes = successes + ?, last_used_at = ? WHERE recipe_id = ?",
            [(1 if succeeded else 0, now, recipe_id) for recipe_id in recipe_ids],
        )
        self._connection.commit()

    def get_recipe(self, recipe_id: int) -> Recipe | None:
        row = self._connection.execute("SELECT * FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone()
        return self._recipe(row) if row else None

    def list_recipes(self, locale: str | None = None, *, limit: int) -> tuple[Recipe, ...]:
        if locale:
            rows = self._connection.execute(
                "SELECT * FROM recipes WHERE locale = ? ORDER BY last_used_at DESC LIMIT ?", (locale, limit)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM recipes ORDER BY last_used_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return tuple(self._recipe(row) for row in rows)

    def delete_recipe(self, recipe_id: int) -> bool:
        cursor = self._connection.execute("DELETE FROM recipes WHERE recipe_id = ?", (recipe_id,))
        self._connection.commit()
        return cursor.rowcount > 0

    # ---- 용어 사전 ---------------------------------------------------------

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
    ) -> GlossaryEntry:
        self._connection.execute(
            "INSERT INTO glossary (term, locale, table_name, columns, note, source, embedding, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(term, locale) DO UPDATE SET"
            " table_name = excluded.table_name, columns = excluded.columns,"
            " note = excluded.note, source = excluded.source, embedding = excluded.embedding",
            (term, locale, table, ",".join(columns), note, source, pack(embedding), datetime.now(UTC).isoformat()),
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT * FROM glossary WHERE term = ? AND locale = ?", (term, locale)
        ).fetchone()
        return self._term(row)

    def list_terms(self, locale: str | None = None, *, limit: int) -> tuple[GlossaryEntry, ...]:
        if locale:
            rows = self._connection.execute(
                "SELECT * FROM glossary WHERE locale = ? ORDER BY term LIMIT ?", (locale, limit)
            ).fetchall()
        else:
            rows = self._connection.execute("SELECT * FROM glossary ORDER BY term LIMIT ?", (limit,)).fetchall()
        return tuple(self._term(row) for row in rows)

    def delete_term(self, term_id: int) -> bool:
        cursor = self._connection.execute("DELETE FROM glossary WHERE term_id = ?", (term_id,))
        self._connection.commit()
        return cursor.rowcount > 0

    # ---- 유지보수 ----------------------------------------------------------

    def prune(self, max_recipes: int) -> int:
        total = self._connection.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        if total <= max_recipes:
            return 0
        # 성적이 나쁘고 오래 쓰이지 않은 것부터 버린다.
        cursor = self._connection.execute(
            "DELETE FROM recipes WHERE recipe_id IN ("
            "  SELECT recipe_id FROM recipes"
            "  ORDER BY (CAST(successes + 1 AS REAL) / (uses + 2)) ASC, last_used_at ASC"
            "  LIMIT ?)",
            (total - max_recipes,),
        )
        self._connection.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._connection.close()

    # ---- 매핑 --------------------------------------------------------------

    @staticmethod
    def _recipe(row: sqlite3.Row) -> Recipe:
        raw: Any = json.loads(row["steps"])
        steps = tuple(
            PlanStep(str(item.get("tool", "")), dict(item.get("intent") or {}))
            for item in raw
            if isinstance(item, dict)
        )
        return Recipe(
            recipe_id=int(row["recipe_id"]),
            question=row["question"],
            locale=row["locale"],
            steps=steps,
            tables=tuple(item for item in str(row["tables"]).split(",") if item),
            uses=int(row["uses"]),
            successes=int(row["successes"]),
        )

    @staticmethod
    def _term(row: sqlite3.Row) -> GlossaryEntry:
        return GlossaryEntry(
            term_id=int(row["term_id"]),
            term=row["term"],
            locale=row["locale"],
            table=row["table_name"],
            columns=tuple(item for item in str(row["columns"]).split(",") if item),
            note=row["note"],
            source=row["source"],
        )
