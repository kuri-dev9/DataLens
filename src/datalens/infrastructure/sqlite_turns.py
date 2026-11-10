"""evidence 모드 호출 기록 — memory.sqlite3 의 evidence_turns (2026-11-04).

호출 하나가 한 행이고, 칸은 넷이다(사용자 결정).
  received  graphio 가 보낸 요청 그대로(지시·질문·증거팩)
  llm_raw   모델이 돌려준 원문 그대로
  dropped   검증에서 빠진 줄과 이유 — [{line, reason, sure}] (graphio 검증 재현 · ask_check)
  final     버린 줄을 뺀 답 — 운영자에게 가는 것
같은 세션을 여러 번 부르면 seq 가 1, 2, … 로 쌓인다. 최근 200건만 둔다.

곁칸 (2026-11-04 저녁)
  style         답변 스타일 적용 기록 {applied, settings, reason | lang·original·block} (answer_style.py)
  verdict_from  dropped·final 의 출처 — `재현`(ask_check) | `graphio`(POST …/verdict 로 받은 정본 판정)
  verdict_at    graphio 판정을 받은 시각
graphio 판정이 오면 재현 결과를 덮는다. 판정이 오지 않은 행만 재현(△ 포함)으로 남는다.
원문에 증거팩이 그대로 있으므로 조회는 API 키가 필요한 /v1/settings/evidence-turns 로만 한다.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

KEEP = 200
# agent(일반 질문) 원문 · 프롬프트 실험 결과 보관 수 — 초기값(2026-11-10 사용자 결정 "200건")
AGENT_KEEP = 200
LAB_KEEP = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL,
    received TEXT NOT NULL,
    llm_raw TEXT NOT NULL,
    dropped TEXT NOT NULL,
    final TEXT NOT NULL,
    style TEXT NOT NULL DEFAULT '{}',
    verdict_from TEXT NOT NULL DEFAULT '재현',
    verdict_at TEXT,
    UNIQUE(at, session_id)
);
CREATE INDEX IF NOT EXISTS evidence_turns_session ON evidence_turns(session_id, seq);
CREATE TABLE IF NOT EXISTS agent_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    trace_json TEXT NOT NULL,
    UNIQUE(at, session_id)
);
CREATE TABLE IF NOT EXISTS lab_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL
);
"""


class SqliteTurnStore:
    def __init__(self, path: str | Path, keep: int = KEEP, agent_keep: int = AGENT_KEEP, lab_keep: int = LAB_KEEP) -> None:
        self._agent_keep = agent_keep
        self._lab_keep = lab_keep
        self._path = Path(path)
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._keep = keep
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(self._path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        legacy = self._legacy_rows()
        self._connection.executescript(SCHEMA)
        self._add_side_columns()
        self._connection.commit()
        for trace in legacy:
            self.save(trace)

    def _legacy_rows(self) -> list[dict[str, Any]]:
        # 11-04 15:30 판(trace_json 을 통째로 두던 모양)을 4칸으로 옮긴다 — 시연 사례가 그 판에 있다
        cols = {row[1] for row in self._connection.execute("PRAGMA table_info(evidence_turns)")}
        if "trace_json" not in cols:
            return []
        rows = [json.loads(r[0]) for r in self._connection.execute("SELECT trace_json FROM evidence_turns ORDER BY at")]
        self._connection.execute("DROP TABLE evidence_turns")
        self._connection.commit()
        return rows

    def _add_side_columns(self) -> None:
        # 15:32 판(4칸만 있던 모양)에 곁칸을 붙인다 — 행은 그대로 둔다
        cols = {row[1] for row in self._connection.execute("PRAGMA table_info(evidence_turns)")}
        for name, ddl in (
            ("style", "style TEXT NOT NULL DEFAULT '{}'"),
            ("verdict_from", "verdict_from TEXT NOT NULL DEFAULT '재현'"),
            ("verdict_at", "verdict_at TEXT"),
        ):
            if name not in cols:
                self._connection.execute(f"ALTER TABLE evidence_turns ADD COLUMN {ddl}")

    def save(self, trace: dict[str, Any]) -> None:
        from datalens.application.ask_check import split

        messages = (trace.get("sent") or {}).get("messages") or []
        received = trace.get("received")
        if received is None:  # 옛 판 trace — 바꿔 끼우기 전이라 보낸 메시지가 곧 받은 원문이다
            received = next((m.get("content", "") for m in reversed(messages) if m.get("kind") == "current_message"), "")
        style = trace.get("style") or {"applied": False, "reason": "기록 없음"}
        llm_raw = (trace.get("response") or {}).get("answer") or ""
        dropped, final = split(trace.get("check") or {})
        at, session_id = trace.get("at", ""), trace.get("session_id", "")
        with self._lock:
            known = self._connection.execute(
                "SELECT seq FROM evidence_turns WHERE at = ? AND session_id = ?", (at, session_id)
            ).fetchone()
            if known:
                return  # 같은 호출을 두 번 넣지 않는다
            seq = self._connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM evidence_turns WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            self._connection.execute(
                "INSERT INTO evidence_turns (at, session_id, seq, received, llm_raw, dropped, final, style)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (at, session_id, seq, received, llm_raw, json.dumps(dropped, ensure_ascii=False), final,
                 json.dumps(style, ensure_ascii=False)),
            )
            self._connection.execute(
                "DELETE FROM evidence_turns WHERE id NOT IN"
                " (SELECT id FROM evidence_turns ORDER BY at DESC, id DESC LIMIT ?)",
                (self._keep,),
            )
            self._connection.commit()

    def attach_verdict(self, session_id: str, verdict: dict[str, Any], at: str) -> dict[str, Any] | None:
        """graphio 판정을 그 세션의 가장 최근 호출에 붙인다. 그 세션 기록이 없으면 None.

        graphio 는 DataLens 답을 받은 뒤 검증해 보내므로, 판정이 올 때 그 호출의 행은 이미 있다.
        """
        dropped = [
            {"line": str(item.get("text", "")), "reason": str(item.get("reason", "")), "sure": True}
            for item in verdict.get("dropped") or []
            if isinstance(item, dict)
        ]
        final = verdict.get("final")
        with self._lock:
            row = self._connection.execute(
                "SELECT id, seq FROM evidence_turns WHERE session_id = ? ORDER BY seq DESC, id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            self._connection.execute(
                "UPDATE evidence_turns SET dropped = ?, final = COALESCE(?, final), verdict_from = 'graphio',"
                " verdict_at = ? WHERE id = ?",
                (json.dumps(dropped, ensure_ascii=False), final if isinstance(final, str) else None, at, row["id"]),
            )
            self._connection.commit()
        return {"id": row["id"], "seq": row["seq"], "dropped": len(dropped)}

    def recent(self, limit: int = KEEP) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM evidence_turns ORDER BY at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{**dict(row), "dropped": json.loads(row["dropped"]), "style": json.loads(row["style"] or "{}")} for row in rows]

    # ── agent 원문(2026-11-10) ─────────────────────────────────────────────
    # 일반 질문은 메모리 trace(모드별 5건)뿐이라 재시작하면 사라졌다. 프롬프트 실험이 "어제 그 질문"을
    # 고르려면 남아야 한다. 모양이 evidence 4칸과 달라(도구 단계가 있다) trace 를 통째로 둔다.
    def save_agent(self, trace: dict[str, Any]) -> None:
        if self._agent_keep <= 0:
            return
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO agent_traces (at, session_id, trace_json) VALUES (?, ?, ?)",
                (trace.get("at", ""), trace.get("session_id", ""), json.dumps(trace, ensure_ascii=False, default=str)),
            )
            self._connection.execute(
                "DELETE FROM agent_traces WHERE id NOT IN (SELECT id FROM agent_traces ORDER BY at DESC, id DESC LIMIT ?)",
                (self._agent_keep,),
            )
            self._connection.commit()

    def recent_agent(self, limit: int = AGENT_KEEP) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, trace_json FROM agent_traces ORDER BY at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"id": row["id"], **json.loads(row["trace_json"])} for row in rows]

    def agent_trace(self, trace_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT id, trace_json FROM agent_traces WHERE id = ?", (trace_id,)).fetchone()
        return None if row is None else {"id": row["id"], **json.loads(row["trace_json"])}

    def evidence_turn(self, turn_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM evidence_turns WHERE id = ?", (turn_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "dropped": json.loads(row["dropped"]), "style": json.loads(row["style"] or "{}")}

    def evidence_before(self, session_id: str, seq: int) -> list[dict[str, Any]]:
        """같은 세션의 앞 호출들(오래된 것부터) — evidence 실험의 이전 문답을 다시 채운다."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT received, llm_raw, final FROM evidence_turns WHERE session_id = ? AND seq < ? ORDER BY seq",
                (session_id, seq),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── 프롬프트 실험 결과 ──────────────────────────────────────────────────
    # 운영 기록(evidence_turns·agent_traces)과 섞지 않는다 — 실험은 이 표에만 남는다.
    def save_lab(self, source_kind: str, source_id: int, request: dict[str, Any], result: dict[str, Any], at: str) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO lab_runs (at, source_kind, source_id, request_json, result_json) VALUES (?, ?, ?, ?, ?)",
                (at, source_kind, source_id, json.dumps(request, ensure_ascii=False, default=str),
                 json.dumps(result, ensure_ascii=False, default=str)),
            )
            self._connection.execute(
                "DELETE FROM lab_runs WHERE id NOT IN (SELECT id FROM lab_runs ORDER BY id DESC LIMIT ?)",
                (self._lab_keep,),
            )
            self._connection.commit()
            return int(cursor.lastrowid)

    def lab_runs(self, limit: int = LAB_KEEP) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, at, source_kind, source_id, request_json, result_json FROM lab_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._lab_row(row) for row in rows]

    def lab_run(self, run_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, at, source_kind, source_id, request_json, result_json FROM lab_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return None if row is None else self._lab_row(row)

    @staticmethod
    def _lab_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "at": row["at"], "source_kind": row["source_kind"], "source_id": row["source_id"],
                "request": json.loads(row["request_json"]), "result": json.loads(row["result_json"])}

    def close(self) -> None:
        self._connection.close()
