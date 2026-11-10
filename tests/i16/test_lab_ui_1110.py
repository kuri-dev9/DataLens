"""프롬프트 실험 화면 정리 — `question_of` · `link_of` · `sources`/`runs` 새 필드 (`application/lab.py`, 2026-11-10 · 맥스 구현 고정).

* question_of: 표지 네 종류 · 「반드시 지킬 것」 꼬리와 빈 줄 뒤 떼기 · 표지 없음 → 원문 그대로 · 표지가 여럿이면 마지막.
* link_of: 증거팩 event_id → 「사건 ID:」 → 「조각 ID:」 → 아무 ID 순 · conclusion 의 unit·root_code · 없으면 None.
* sources: 새 필드(ask·event_id·unit·root_code) + 기존 필드 유지 · 이어 묻기가 같은 세션에서 연결을 빌린다(link_from).
* runs: 같은 새 필드.
* settings.html: 화면 스크립트가 쓰는 새 필드 이름이 서버 응답과 맞는다(문자열 확인 — 화면 동작은 시험하지 못한다).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from datalens.application.lab import LabService, link_of, question_of
from datalens.infrastructure.sqlite_turns import SqliteTurnStore

TAIL = ("\n\n반드시 지킬 것:\n- 근거 ID 는 이 목록에서만 쓴다: EV-00, EV-09\n- 증거팩에 없는 수치를 쓰지 않는다.\n- 답은 일본어로 쓴다.")


# ── question_of ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("marker", ["운영자 질문:", "운영자 추가 질문:", "[질문]", "[質問]"])
def test_each_marker_keeps_only_the_question(marker):
    text = f"당신은 RCA 설명자다.\n증거팩 {{...}}\n{marker} セルは何個ですか?{TAIL}"
    assert question_of(text) == "セルは何個ですか?"


def test_tail_phrase_without_blank_line_is_cut_too():
    assert question_of("운영자 질문: 원인은? 반드시 지킬 것: - 근거 ID") == "원인은?"
    assert question_of("[質問] 原因は? 必ず守ること: - 根拠ID") == "原因は?"


def test_question_stops_at_first_blank_line_and_whitespace_is_collapsed():
    assert question_of("[질문]\n  첫째 줄\n  둘째 줄\n\n뒤 문단") == "첫째 줄 둘째 줄"


def test_without_marker_returns_the_original_collapsed_not_empty():
    assert question_of("  그냥   직접 질문  ") == "그냥 직접 질문"
    assert question_of("") == ""                       # 호출부는 항상 문자열을 준다(`or ""`) — None 은 받지 않는다


def test_several_markers_use_the_last_one():
    text = "운영자 질문: 앞 질문\n\n...\n운영자 추가 질문: 뒤 질문" + TAIL
    assert question_of(text) == "뒤 질문"
    assert question_of("운영자 추가 질문: 앞\n\n[질문] 더 뒤") == "더 뒤"


def test_extra_marker_is_not_confused_with_plain_marker():
    # 「운영자 추가 질문:」 안에 「운영자 질문:」 이 들어 있지 않다 — 앞 단어가 잘리지 않는다
    assert question_of("운영자 추가 질문: 추가") == "추가"


def test_question_text_that_contains_the_marker_word_still_works():
    assert question_of("운영자 질문: [질문] 이라는 말이 들어간 질문") == "이라는 말이 들어간 질문"      # 마지막 표지 기준(알려진 한계)


# ── link_of ─────────────────────────────────────────────────────────────────

EVT = "EVT-313dee37ce"
IX = "IX26111011141e8c"
PACK = ('{"ctx": {"event": true, "scope": {"unit": "L1_BUNDLE"}}, "conclusion": {"unit": "L1_BUNDLE", '
        '"root_code": "EVENT_CROWD", "label": "x", "confidence": 0.593}, "other": {"unit": "PIECE"}}')


def test_event_json_wins_over_later_ids():
    text = f'x "event": {{"event_id": "{EVT}", "members": 5}} 사건 ID: EVT-aaaaaaaaaa 조각 ID: {IX} {PACK}'
    assert link_of(text)["event_id"] == EVT


def test_event_line_then_piece_line_then_any_id_order():
    assert link_of(f"사건 ID: {EVT}\n조각 ID: {IX}")["event_id"] == EVT
    assert link_of(f"조각 ID: {IX} 그리고 {EVT}")["event_id"] == IX
    assert link_of(f"본문 중 {EVT} 와 {IX}")["event_id"] == EVT
    assert link_of(f"본문 중 {IX} 만")["event_id"] == IX


def test_unit_and_root_code_come_only_from_the_conclusion_block():
    out = link_of(f"조각 ID: {IX} {PACK}")
    assert out == {"event_id": IX, "unit": "L1_BUNDLE", "root_code": "EVENT_CROWD"}      # 다른 칸의 unit(PIECE) 이 아니다


def test_piece_id_without_conclusion_unit_defaults_to_piece():
    assert link_of(f"조각 ID: {IX}") == {"event_id": IX, "unit": "PIECE", "root_code": None}


def test_event_id_without_conclusion_has_no_unit():
    assert link_of(f"사건 ID: {EVT}") == {"event_id": EVT, "unit": None, "root_code": None}


def test_nothing_found_gives_all_none():
    for text in ("", None, "ID 가 없는 질문", "EVT-zz 도 IX12 도 모양이 아니다"):
        assert link_of(text) == {"event_id": None, "unit": None, "root_code": None}


def test_conclusion_with_null_values_and_missing_fields():
    assert link_of('"conclusion": {"unit": "L2_EVENT", "shape": null}')["unit"] == "L2_EVENT"
    assert link_of('"conclusion": {"unit": "L2_EVENT", "shape": null}')["root_code"] is None


# ── sources · runs ──────────────────────────────────────────────────────────

def _evidence(store, at, sid, seq, received):
    store.save({"at": at, "session_id": sid, "mode": "evidence",
                "sent": {"messages": [{"kind": "current_message", "content": received}]},
                "response": {"answer": "答"}, "received": received, "check": {"dropped": [], "final": "答"}})


def _agent(store, at, sid, message):
    store.save_agent({"at": at, "session_id": sid, "locale": "ja", "response": {"tool_calls": 1, "warnings": []},
                      "sent": {"messages": [{"kind": "current_message", "content": message}]}})


def make_lab():
    store = SqliteTurnStore(":memory:")
    turn_log = type("TL", (), {"store": store})()
    return store, LabService(None, None, None, turn_log, None, None)


def first_msg(q="この障害の原因は?"):
    return f"증거팩 조각 ID: {IX} {PACK}\n운영자 질문: {q}{TAIL}"


def test_sources_have_new_fields_and_keep_old_ones():
    store, lab = make_lab()
    _evidence(store, "2026-11-10T10:00:00", "S1", 1, first_msg())
    _agent(store, "2026-11-10T11:00:00", "S9", f"조각 ID: {IX}\n[質問] セル数は?")
    rows = lab.sources()
    ev = next(r for r in rows if r["kind"] == "evidence")
    ag = next(r for r in rows if r["kind"] == "agent")
    for r in (ev, ag):
        assert {"kind", "id", "at", "mode", "session_id", "locale", "question"} <= r.keys()       # 기존 필드
        assert {"ask", "event_id", "unit", "root_code"} <= r.keys()                                 # 새 필드
    assert ev["ask"] == "この障害の原因は?" and ev["event_id"] == IX and ev["unit"] == "L1_BUNDLE" and ev["root_code"] == "EVENT_CROWD"
    assert ag["ask"] == "セル数は?" and ag["unit"] == "PIECE"
    assert ev["question"] and "dropped" in ev and "tool_calls" in ag            # 기존 값이 그대로
    assert "link_from" not in ev and "link_from" not in ag


def test_follow_up_without_pack_borrows_link_from_the_same_session_only():
    store, lab = make_lab()
    _evidence(store, "2026-11-10T10:00:00", "S1", 1, first_msg())
    _evidence(store, "2026-11-10T10:05:00", "S1", 2, "운영자 추가 질문: 셀은 몇 개?" + TAIL)          # 증거팩 없음 — 같은 세션
    _evidence(store, "2026-11-10T10:06:00", "S2", 1, "운영자 추가 질문: 다른 세션" + TAIL)               # 다른 세션 — 못 빌린다
    by_sid = {(r["session_id"], r["seq"]): r for r in lab.sources() if r["kind"] == "evidence"}
    follow, other = by_sid[("S1", 2)], by_sid[("S2", 1)]
    assert follow["event_id"] == IX and follow["unit"] == "L1_BUNDLE" and follow["root_code"] == "EVENT_CROWD"
    assert follow["link_from"] == "session" and follow["ask"] == "셀은 몇 개?"
    assert other["event_id"] is None and "link_from" not in other


def test_borrowing_does_not_overwrite_a_row_that_has_its_own_link():
    store, lab = make_lab()
    _evidence(store, "2026-11-10T10:00:00", "S1", 1, first_msg())
    _evidence(store, "2026-11-10T10:05:00", "S1", 2, f"조각 ID: IX2611101200abcd\n운영자 질문: 다른 장애{TAIL}")
    rows = {r["seq"]: r for r in lab.sources() if r["kind"] == "evidence"}
    assert rows[2]["event_id"] == "IX2611101200abcd" and "link_from" not in rows[2]


def test_sources_still_sorted_newest_first():
    store, lab = make_lab()
    _evidence(store, "2026-11-10T09:00:00", "A", 1, first_msg())
    _agent(store, "2026-11-10T12:00:00", "B", "[質問] 新しい")
    _evidence(store, "2026-11-09T09:00:00", "C", 1, first_msg())
    assert [r["session_id"] for r in lab.sources()] == ["B", "A", "C"]


def test_runs_have_new_fields():
    store, lab = make_lab()
    store.save_lab("evidence", 5, {"message": first_msg("実験の質問"), "prompt_extra": "追加"}, {"mode": "evidence", "locale": "ja"}, "t")
    store.save_lab("custom", 0, {"message": "직접 입력한 질문"}, {"mode": "agent", "locale": "ko"}, "t2")
    rows = {r["source_kind"]: r for r in lab.runs()}
    ev = rows["evidence"]
    assert ev["ask"] == "実験の質問" and ev["event_id"] == IX and ev["unit"] == "L1_BUNDLE" and ev["root_code"] == "EVENT_CROWD"
    assert {"id", "at", "source_kind", "source_id", "mode", "locale", "question", "prompt_extra", "tool_calls"} <= ev.keys()   # 기존 필드
    custom = rows["custom"]
    assert custom["ask"] == "직접 입력한 질문" and custom["event_id"] is None and custom["unit"] is None


# ── 화면 스크립트가 서버 필드와 맞는다 ───────────────────────────────────────

HTML = (Path(__file__).resolve().parents[2] / "src" / "datalens" / "ui" / "settings.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["event_id", "root_code", "ask"])
def test_ui_reads_the_new_fields(name):
    assert name in HTML


def test_ui_has_no_external_script_and_escapes_before_markdown():
    import re
    assert not re.search(r'<script[^>]+src=["\']https?://', HTML)
    assert "<details" in HTML


# ── md 렌더러 (node 가 있을 때만) ─────────────────────────────────────────────

def test_md_renderer_escapes_html_before_styling():
    import shutil
    import subprocess
    import tempfile
    if not shutil.which("node"):
        pytest.skip("node 없음")
    start = HTML.index("function mdInline")
    end = HTML.index("\n}\n", HTML.index("function md(")) + 3
    js = (
        'const esc=(s)=>String(s ?? "").replace(/[&<>"]/g,(c)=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;"}[c]));\n'
        + HTML[start:end]
        + '\nconst cases={a:md("<script>alert(1)</script> **굵게**"),b:md("# 제목\\n- 가\\n- 나\\n\\n| x | y |\\n|---|---|\\n| 1 | 2 |"),'
          'c:md("<img src=x onerror=alert(1)>"),d:md("`<b>`"),e:md(""),f:md("[x](javascript:alert(1))")};\n'
          'console.log(JSON.stringify(cases));'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
    import json
    out = json.loads(subprocess.run(["node", fh.name], capture_output=True, text=True, check=True).stdout)
    assert "<script>" not in out["a"] and "&lt;script&gt;" in out["a"] and "<b>굵게</b>" in out["a"]
    assert "<h2>" in out["b"] and out["b"].count("<li>") == 2 and "<table>" in out["b"] and "<td>1</td>" in out["b"]
    assert "<img" not in out["c"] and "<b>" not in out["d"] and "&lt;b&gt;" in out["d"]
    assert out["e"].startswith('<div class="md">') and "href" not in out["f"]
