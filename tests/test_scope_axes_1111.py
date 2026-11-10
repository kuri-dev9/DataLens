"""축 사건(기종·APN·도메인·지역)의 범위 거름 표시 (`agent._SCOPE_AXES`·`_scope_axes`·`_scope_note_inner` · 2026-11-11 · graphio 8번 F-264).

기종 사건에 모델이 기종 거름 없이 전국 합계(35,135 / 35,083)를 냈다(그 기종 원시 834 / 782). 범위 줄에 축 ID(기종·APN·도메인·지역)가 있는데
조회 `filters` 가 그 축의 거름 열을 사건 ID 로 걸지 않았으면 표시를 붙인다. **엄격하다** — 거름 값이 모두 사건 ID 안일 때만 맞은 것이다.
기종은 (VENDOR, MODEL) 쌍으로, 도메인은 http 꼴(`0101`)과 dns 꼴(`.0101.co.jp`)을 같은 사이트로 본다. 노드·eNB 판정은 그대로다.
가짜 입력 — 네트워크 없음.
"""
from __future__ import annotations

import pytest

from datalens.application import agent as A

WIN = "시각 창 2026-11-11T00:35:00 ~ 2026-11-11T00:38:00(끝 포함)"
PS = {"from": "2026-11-11T00:35:00", "to": "2026-11-11T00:38:00", "kind": "time_range", "column": "EVENT_TIME"}
UE = "기종 100OA/A55Y"
OTHER = "values than the incident's query scope"          # 거름 열은 있는데 값이 사건 밖
NOT_FILTERED = "do not restrict to the incident's"        # 거름 열 자체가 없다
WHOLE = "whole-network value"


def eq(col, v):
    return {"column": col, "operator": "eq", "value": v}


def inn(col, vs):
    return {"column": col, "operator": "in", "values": vs}


def ne(col, v):
    return {"column": col, "operator": "ne", "value": v}


def note(extra, *conds, any_groups=None, window=True):
    filters = {"all": list(conds)}
    if any_groups:
        filters["any_groups"] = any_groups
    msg = "조회 범위(이 판정 단위): " + (WIN + " · " if window else "") + extra
    return A._scope_note(msg, {"filters": filters, "partition_scope": PS})


# ── 1 · 범위 줄 읽기 ────────────────────────────────────────────────────────

def test_scope_axes_splits_every_axis_key():
    got = A._scope_axes("기종 100OA/A55Y · APN a.gprs · 도메인 google · 도메인 x · 지역 MESH500 a, b, c · eNB 1, 2")
    assert got == {"기종 ": ["100OA/A55Y"], "APN ": ["a.gprs"], "도메인 ": ["google", "x"], "지역 ": ["a", "b", "c"]}


@pytest.mark.parametrize("line", ["Core 노드 SGW-14 · eNB 5", "eNB 1, 2", "", "시각 창 x"])
def test_lines_without_axis_keys_have_empty_axes(line):
    assert A._scope_axes(line) == {}


def test_area_key_drops_the_level_word_and_keeps_all_codes():
    assert A._scope_axes("지역 MESH500 53394632, 53394633") == {"지역 ": ["53394632", "53394633"]}


def test_incident_scope_carries_axes_and_keeps_other_fields():
    sc = A._incident_scope("조회 범위(이 판정 단위): " + WIN + " · " + UE + " · eNB 123, 456")
    assert sc["axes"] == {"기종 ": ["100OA/A55Y"]} and sc["has_enb"] and sc["enbs"] == ["123", "456"]
    assert sc["window"] == ("2026-11-11T00:35:00", "2026-11-11T00:38:00")
    assert A._incident_scope("조회 범위(이 판정 단위): Core 노드 SGW-5")["axes"] == {}


# ── 2 · 기종 ────────────────────────────────────────────────────────────────

def test_ue_both_columns_matching_gives_no_note():
    assert note(UE, eq("VENDOR", "100OA"), eq("MODEL", "A55Y")) is None


def test_ue_without_any_filter_says_not_filtered():
    n = note(UE)
    assert NOT_FILTERED in n and "device model" in n


def test_ue_unrelated_filter_columns_count_as_not_filtered():
    assert NOT_FILTERED in note(UE, eq("SERIES", "http"))


@pytest.mark.parametrize("conds", [
    [eq("VENDOR", "100OA")],                                    # 한 열만 — 제조사 전체 합계
    [eq("MODEL", "A55Y")],
    [eq("VENDOR", "100OA"), eq("MODEL", "ZZ")],                 # 다른 모델
    [eq("VENDOR", "X"), eq("MODEL", "A55Y")],
    [inn("VENDOR", ["100OA", "X"]), eq("MODEL", "A55Y")],        # in 에 사건 밖 값이 섞임
    [eq("VENDOR", "100OA"), inn("MODEL", ["A55Y", "Q"])],
])
def test_ue_partial_or_foreign_filter_says_other_values(conds):
    n = note(UE, *conds)
    assert OTHER in n and "device model" in n


def test_ue_in_with_exactly_the_incident_values_is_a_hit():
    assert note(UE, inn("VENDOR", ["100OA"]), inn("MODEL", ["A55Y"])) is None


def test_ue_id_without_slash_never_counts_as_a_hit():
    assert OTHER in note("기종 A55Y", eq("VENDOR", "A55Y"), eq("MODEL", "A55Y"))


def test_two_ue_models_cross_product_is_not_a_hit_but_one_exact_pair_is():
    two = "기종 A/x · 기종 B/y"
    assert OTHER in note(two, inn("VENDOR", ["A", "B"]), inn("MODEL", ["x", "y"]))      # A/y 같은 사건 밖 조합까지 더한다
    assert note(two, eq("VENDOR", "A"), eq("MODEL", "x")) is None
    assert note(two, eq("VENDOR", "B"), eq("MODEL", "y")) is None


def test_ue_case_is_significant():
    assert OTHER in note(UE, eq("VENDOR", "100oa"), eq("MODEL", "A55Y"))


# ── 3 · APN · 도메인 · 지역 ─────────────────────────────────────────────────

APN = "APN spmode.ne.jp.mnc010.mcc440.gprs"


def test_apn_exact_match_none_missing_foreign_and_case():
    assert note(APN, eq("APN", "spmode.ne.jp.mnc010.mcc440.gprs")) is None
    assert NOT_FILTERED in note(APN)
    assert OTHER in note(APN, eq("APN", "mopera.net.mnc010.mcc440.gprs"))
    assert OTHER in note(APN, eq("APN", "Spmode.ne.jp.mnc010.mcc440.gprs")), "APN 은 대소문자만 다른 값이 공존한다"


def test_apn_in_subset_is_a_hit_and_foreign_member_is_not():
    two = "APN a · APN b"
    assert note(two, inn("APN", ["a"])) is None
    assert note(two, inn("APN", ["a", "b"])) is None
    assert OTHER in note(two, inn("APN", ["a", "c"]))


def test_area_codes_hit_miss_and_mixed():
    area = "지역 MESH500 a, b, c"
    assert note(area, eq("AREA_CD", "b")) is None
    assert OTHER in note(area, eq("AREA_CD", "z"))
    assert OTHER in note("지역 MESH500 a, b", inn("AREA_CD", ["a", "z"]))
    assert NOT_FILTERED in note(area)
    assert note("지역 MESH500 a, b", inn("AREA_CD", ["a", "b"])) is None


def test_domain_http_form_hit_none_and_foreign():
    assert note("도메인 google", eq("HOST_SITE", "google"), eq("SERIES", "http")) is None
    assert NOT_FILTERED in note("도메인 google", eq("SERIES", "http"))
    assert OTHER in note("도메인 google", eq("HOST_SITE", "yahoo"))


@pytest.mark.parametrize("site,hit", [
    (".google.com", True), (".0101.co.jp", True), ("google", True),             # dns 꼴 · http 꼴
    (".googleapis.com", False), ("xgoogle.com", False), ("Google", False), (".Google.com", False), ("google.com", False),
])
def test_domain_dns_form_matches_the_same_site_only(site, hit):
    ids = "도메인 google · 도메인 0101"
    got = note(ids, eq("HOST_SITE", site))
    assert (got is None) is hit, (site, got)
    if not hit:
        assert OTHER in got


def test_domain_in_with_http_and_dns_forms_is_a_hit_but_foreign_dns_is_not():
    ids = "도메인 0101"
    assert note(ids, inn("HOST_SITE", ["0101", ".0101.co.jp"])) is None
    assert OTHER in note(ids, inn("HOST_SITE", ["0101", ".0202.co.jp"]))


# ── 4 · eNB 가 함께 있을 때 · 노드·eNB 사건 ─────────────────────────────────

def test_correct_axis_filter_suppresses_the_whole_network_note_even_with_enb_key():
    assert note(UE + " · eNB 123, 456", eq("VENDOR", "100OA"), eq("MODEL", "A55Y")) is None


def test_wrong_axis_filter_with_enb_key_gives_both_notes():
    n = note(UE + " · eNB 123, 456", eq("VENDOR", "X"), eq("MODEL", "Y"))
    assert OTHER in n and "device model" in n
    assert "cells/eNBs/nodes" in n                       # 축이 틀리면 eNB 표시도 그대로 붙는다 — 둘 다 "사건 값이 아니다"


def test_missing_axis_filter_with_enb_key_still_says_whole_network():
    n = note(UE + " · eNB 123, 456")
    assert NOT_FILTERED in n and WHOLE in n


def test_node_and_enb_only_events_behave_as_before():
    assert WHOLE in note("Core 노드 SGW-5")
    assert note("Core 노드 SGW-5", eq("SGW_ID", "5")) is None
    assert note("Core 노드 SGW-5", inn("SGW_ID", ["5", "6"])) is None                 # 노드는 겹침만 본다 — 엄격하지 않다
    assert note("eNB 123, 456", eq("ENB_ID", "123")) is None
    assert "other cells/eNBs/nodes" in note("eNB 123, 456", eq("ENB_ID", "999"))


def test_no_axis_key_no_note_from_the_axis_branch():
    assert note("시각 창 x", window=False) is None


# ── 5 · 기존 거름 규칙이 축 쪽에도 통한다 ───────────────────────────────────

def test_ne_conditions_are_ignored_for_axes():
    assert note(UE, eq("VENDOR", "100OA"), eq("MODEL", "A55Y"), ne("VENDOR", "Z")) is None
    assert NOT_FILTERED in note(UE, ne("VENDOR", "100OA"), ne("MODEL", "A55Y"))


def test_any_groups_values_are_read_and_foreign_ones_spoil_the_hit():
    assert note(UE, eq("VENDOR", "100OA"), any_groups=[[eq("MODEL", "A55Y")]]) is None
    assert OTHER in note(UE, eq("VENDOR", "100OA"), any_groups=[[eq("MODEL", "A55Y"), eq("MODEL", "Q")]])


def test_malformed_filters_never_raise():
    for bad in (None, "x", [], {"all": "x"}, {"all": [None, 3]}):
        A._scope_note("조회 범위(이 판정 단위): " + UE, {"filters": bad, "partition_scope": PS})        # 예외로 턴을 막지 않는다
    assert A._scope_note("조회 범위(이 판정 단위): " + UE, "not a dict") is None
    assert A._scope_note("질문뿐", {"filters": {}}) is None


def test_axis_note_is_independent_of_the_time_window_note():
    n = A._scope_note("조회 범위(이 판정 단위): " + WIN + " · " + UE,
                      {"filters": {"all": [eq("VENDOR", "100OA"), eq("MODEL", "A55Y")]},
                       "partition_scope": {**PS, "from": "2026-11-09T00:00:00", "to": "2026-11-09T23:59:00"}})
    assert not n or "device model" not in n             # 창이 벗어나도 축 표시는 거름만 본다
