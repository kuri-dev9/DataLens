"""답변 스타일(`application/answer_style.py`)과 판정 붙이기(`SqliteTurnStore.attach_verdict`).

graphio 질의의 `[답변 형식]` 구역만 바꿔 끼우고, 표지가 한 쌍이 아니거나 꺼져 있으면 한 글자도
바꾸지 않는다. 판정은 그 세션의 가장 최근 호출에만 붙는다.
"""

from __future__ import annotations

import pytest

from datalens.application import answer_style as st
from datalens.infrastructure.sqlite_turns import SqliteTurnStore

ZONE = f"{st.OPEN}\n8. 기본 규칙\n{st.CLOSE}"
MSG = f"규칙 1~7\n{ZONE}\n규칙 9~11\n\n운영자 질문: 결론은?"
ON: dict = {}                                   # 사용 스위치는 없다 — 표지가 한 쌍이면 늘 바꾼다


# ── check ─────────────────────────────────────────────────────────────

def test_check_fills_defaults():
    assert st.check({}) == {**st.DEFAULTS}
    assert st.check({"enabled": True})["min_sentences"] == 6


@pytest.mark.parametrize("bad", [
    {"nope": 1},
    {"markdown": 1},
    {"min_sentences": 0}, {"min_sentences": 10, "max_sentences": 5}, {"max_sentences": 51},
    {"min_sentences": 2.5}, {"min_sentences": True},
    {"max_chars": 99}, {"max_chars": 10001}, {"max_chars": "300"},
    {"extra": "a\nb"}, {"extra": "x" * 301}, {"extra": f"{st.OPEN} 끼워 넣기"}, {"extra": 5},
])
def test_check_rejects_out_of_range(bad):
    with pytest.raises(st.AnswerStyleError):
        st.check(bad)


def test_check_accepts_the_edges():
    s = st.check({"min_sentences": 1, "max_sentences": 50, "max_chars": 100, "extra": "  짧게  "})
    assert s["max_chars"] == 100 and s["extra"] == "짧게"
    assert st.check({"max_chars": 10000})["max_chars"] == 10000


# ── apply: 바꾸지 않는 경우 ───────────────────────────────────────────

def test_enabled_key_is_ignored_not_rejected():
    # 없앤 스위치 — runtime.json 에 남았거나 옛 화면이 보내도 400 이 아니라 버린다
    assert "enabled" not in st.check({"enabled": False, "max_sentences": 9})
    assert st.check({"enabled": "yes"}) == st.check({})
    out, rec = st.apply(MSG, {"enabled": False})           # 꺼짐을 줘도 바꾼다
    assert rec["applied"] is True and "8. 기본 규칙" not in out and "enabled" not in rec["settings"]


def test_default_style_replaces_with_the_graphio_default_block():
    graphio_zone = f"{st.OPEN}\n{st.block(st.DEFAULTS)[len(st.OPEN) + 1:]}"   # graphio 기본 구역과 같은 모양
    out, rec = st.apply(f"앞\n{graphio_zone}\n뒤", None)
    assert rec["applied"] is True and rec["same"] is True and out == f"앞\n{graphio_zone}\n뒤"
    _, changed = st.apply(MSG, {"markdown": False})
    assert changed["same"] is False


def test_no_marker_changes_nothing():
    follow = "어떻게 해결할 수 있어?"                        # 이어 묻기는 질문만 보낸다
    out, rec = st.apply(follow, ON)
    assert out == follow and rec["applied"] is False and rec["reason"] == "표지 없음"
    assert "설정 꺼짐" not in str(rec)


def test_two_pairs_change_nothing():
    twice = MSG + "\n" + ZONE
    out, rec = st.apply(twice, ON)
    assert out == twice and rec["applied"] is False and rec["reason"] == "표지 없음"


def test_broken_markers_change_nothing():
    for broken in (f"{st.OPEN} 열기만", f"{st.CLOSE} 닫기만 {st.OPEN}", f"{st.CLOSE}\n{st.OPEN}"):
        out, rec = st.apply(broken, ON)
        assert out == broken and rec["applied"] is False


# ── apply: 바꾸는 경우 ────────────────────────────────────────────────

def test_enabled_replaces_only_the_zone():
    out, rec = st.apply(MSG, {**ON, "min_sentences": 3, "max_sentences": 7})
    assert out.startswith("규칙 1~7\n") and out.endswith("규칙 9~11\n\n운영자 질문: 결론은?")
    assert "8. 기본 규칙" not in out and "3~7문장" in out
    assert out.count(st.OPEN) == 1 and out.count(st.CLOSE) == 1  # 새 구역도 같은 표지로 감싼다
    assert rec["applied"] is True and rec["lang"] == "ko"
    assert rec["original"] == ZONE and rec["block"] in out
    assert rec["settings"]["min_sentences"] == 3


def test_markdown_on_gives_the_order_but_no_fixed_headings():
    """2026-11-07 — 네 제목 틀을 없앴다(graphio `_answer_format` 과 같은 문구). 제목 설명까지 베끼고 수치만 늘어놓았다."""
    out, _ = st.apply(MSG, ON)
    zone = out[out.index(st.OPEN):out.index(st.CLOSE)]
    for h in ("## 결론", "## 근거", "## 영향 범위", "## 확인·조치"):
        assert h not in out
    assert "첫 문장에서 결론을 말하고" in zone and "정해진 틀은 없다" in zone and "마크다운은 읽기 좋을 때 쓴다" in zone
    assert "HTML 과 링크는 쓰지 않는다" in zone


def test_japanese_request_has_no_fixed_headings_either():
    for marker in ("답(answer_ko 값)은 **일본어**로", "답은 **일본어**로"):
        out, rec = st.apply(f"{marker}\n{ZONE}", ON)
        assert rec["lang"] == "ja"
        for h in ("## 結論", "## 根拠", "## 影響範囲", "## 確認・対処", "## 결론"):
            assert h not in out, (marker, h)
        assert "첫 문장에서 결론을 말하고" in out


def test_markdown_off_asks_for_plain_paragraphs():
    out, rec = st.apply(MSG, {**ON, "markdown": False})
    zone = out[out.index(st.OPEN):out.index(st.CLOSE)]
    assert "## " not in zone and "평문" in zone and "쓰지 않는다" in zone
    assert rec["applied"] is True


def test_char_cap_and_extra_go_into_the_zone():
    out, _ = st.apply(MSG, {**ON, "max_chars": 800, "extra": "존댓말로 쓴다"})
    zone = out[out.index(st.OPEN):out.index(st.CLOSE)]
    assert "800자를 넘기지 않는다" in zone and "존댓말로 쓴다" in zone
    plain, _ = st.apply(MSG, ON)
    assert "넘기지 않는다" not in plain                      # 빈 값이면 제한 없음


def test_text_outside_the_zone_is_byte_identical():
    out, _ = st.apply(MSG, {**ON, "markdown": False, "max_chars": 500})
    head, tail = MSG.split(ZONE)
    assert out.startswith(head) and out.endswith(tail)


# ── attach_verdict ────────────────────────────────────────────────────

def _trace(session: str, at: str, answer: str = "raw") -> dict:
    return {"at": at, "session_id": session, "received": f"질문 {at}",
            "response": {"answer": answer}, "check": {}, "style": {"applied": False}}


@pytest.fixture()
def store():
    s = SqliteTurnStore(":memory:")
    yield s
    s.close()


def _row(store, session, seq):
    return next(r for r in store.recent() if r["session_id"] == session and r["seq"] == seq)


def test_attach_verdict_unknown_session_returns_none(store):
    assert store.attach_verdict("dls_none", {"dropped": []}, "t") is None   # API 는 이것을 404 로 돌려준다
    store.save(_trace("dls_a", "2026-11-04T06:00:00"))
    assert store.attach_verdict("dls_other", {"final": "x"}, "t") is None
    assert _row(store, "dls_a", 1)["verdict_from"] == "재현"


def test_verdict_attaches_to_the_latest_call_only(store):
    store.save(_trace("dls_a", "2026-11-04T06:00:00"))
    store.save(_trace("dls_a", "2026-11-04T06:01:00"))
    store.save(_trace("dls_b", "2026-11-04T06:02:00"))        # 다른 세션은 건드리지 않는다
    got = store.attach_verdict("dls_a", {"final": "## 결론\n끝", "dropped": []}, "2026-11-04T06:03:00")
    assert got == {"id": got["id"], "seq": 2, "dropped": 0}
    assert _row(store, "dls_a", 2)["final"] == "## 결론\n끝"
    assert _row(store, "dls_a", 2)["verdict_from"] == "graphio"
    assert _row(store, "dls_a", 2)["verdict_at"] == "2026-11-04T06:03:00"
    assert _row(store, "dls_a", 1)["verdict_from"] == "재현"
    assert _row(store, "dls_a", 1)["verdict_at"] is None
    assert _row(store, "dls_b", 1)["verdict_from"] == "재현"


def test_verdict_maps_text_to_line_and_marks_sure(store):
    store.save(_trace("dls_a", "2026-11-04T06:00:00"))
    body = {"validator": "graphio rca.llm.ask", "final": "남은 답", "used": True, "evidence_ref": ["EV-00"],
            "dropped": [{"text": "구성 변경 때문입니다", "reason": "단정 표현: 변경 때문"},
                        {"text": "99999건", "reason": "증거팩에 없는 수치: 99999"}, "문자열은 무시"]}
    got = store.attach_verdict("dls_a", body, "t")
    assert got["dropped"] == 2
    row = _row(store, "dls_a", 1)
    assert row["dropped"] == [
        {"line": "구성 변경 때문입니다", "reason": "단정 표현: 변경 때문", "sure": True},
        {"line": "99999건", "reason": "증거팩에 없는 수치: 99999", "sure": True},
    ]
    assert row["final"] == "남은 답"
    assert row["llm_raw"] == "raw" and row["received"] == "질문 2026-11-04T06:00:00"  # 원문 칸은 그대로


def test_verdict_without_final_keeps_the_existing_final(store):
    store.save(_trace("dls_a", "2026-11-04T06:00:00"))
    before = _row(store, "dls_a", 1)["final"]
    store.attach_verdict("dls_a", {"dropped": []}, "t")
    assert _row(store, "dls_a", 1)["final"] == before


def test_new_call_after_verdict_gets_next_seq_and_stays_replayed(store):
    store.save(_trace("dls_a", "2026-11-04T06:00:00"))
    store.attach_verdict("dls_a", {"final": "x"}, "t")
    store.save(_trace("dls_a", "2026-11-04T06:05:00"))
    assert _row(store, "dls_a", 2)["verdict_from"] == "재현"
    store.attach_verdict("dls_a", {"final": "y"}, "t2")
    assert _row(store, "dls_a", 2)["final"] == "y" and _row(store, "dls_a", 1)["final"] == "x"
