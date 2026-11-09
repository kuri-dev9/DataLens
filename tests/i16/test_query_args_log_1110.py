"""query 도구 인자 값 로그 `_query_args_summary` (2026-11-10 · graphio 25·16번 셀 질문 "조회할 수 없었다").

셀 하나를 콕 집어 물은 조회가 값 없이 끝났는데 로그에 인자 **키 이름**만 남아 어떤 조건이 갔는지 알 수 없었다. 테이블·거름(값 포함)·시간 범위·집계를
INFO `tool_call` 의 `query_args` 로 남긴다. 비밀 키는 가리고 500자에서 자른다. 가짜 세션 — 모델·QueryForge 없이.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from datalens.application.agent import QUERY_ARGS_LOG_MAX_CHARS, BoundedAgent, _query_args_summary

ARGS = {
    "source": {"table": "PM_LINK_CELL_NODE_1M"},
    "filters": {"all": [{"column": "ENB_ID", "operator": "eq", "value": "181234"},
                        {"column": "CELLNUM", "operator": "eq", "value": 4}]},
    "partition_scope": {"kind": "time_range", "column": "EVENT_TIME", "from": "2026-11-10T03:55:00", "to": "2026-11-10T03:58:00"},
    "aggregations": [{"function": "sum", "column": "ATT_CNT", "alias": "a"}, {"function": "sum", "column": "FAIL_CNT", "alias": "f", "where": None}],
    "group_by": ["CELLNUM"],
    "confirmation_token": "tok-123",
}


def _log(caplog, tool, args, ok=True, **kw):
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="datalens.agent"):
        BoundedAgent._log_tool_call(SimpleNamespace(session_id="dls_x"), 1, tool, args, ok, 12, False, **kw)
    return next(r for r in caplog.records if r.message == "tool_call")


# ── _query_args_summary ──────────────────────────────────────────────────────

def test_summary_carries_table_filter_values_time_range_and_aggregations():
    d = json.loads(_query_args_summary(ARGS))
    assert d["table"] == "PM_LINK_CELL_NODE_1M"
    assert d["filters"]["all"][0] == {"column": "ENB_ID", "operator": "eq", "value": "181234"}
    assert d["filters"]["all"][1] == {"column": "CELLNUM", "operator": "eq", "value": 4}          # 숫자 타입이 그대로 보인다
    assert d["partition_scope"]["from"] == "2026-11-10T03:55:00" and d["partition_scope"]["to"] == "2026-11-10T03:58:00"
    assert d["aggregations"] == [{"function": "sum", "column": "ATT_CNT", "alias": "a"}, {"function": "sum", "column": "FAIL_CNT", "alias": "f"}]
    assert d["group_by"] == ["CELLNUM"]


def test_secret_keys_are_masked_wherever_they_appear():
    s = _query_args_summary({"source": "T", "filters": {"password": "p1", "api_key": "k1", "all": [{"column": "A", "value": 1, "Authorization": "Bearer z"}]},
                             "partition_scope": {"dsn": "mysql://u:pw@h/db"}, "confirmation_token": "tok"})
    d = json.loads(s)
    assert d["filters"]["password"] == "***" and d["filters"]["api_key"] == "***" and d["filters"]["all"][0]["Authorization"] == "***"
    assert d["partition_scope"]["dsn"] == "***"
    for raw in ("p1", "k1", "Bearer z", "pw@h", "tok"):
        assert raw not in s                                          # 요약 대상이 아닌 칸(`confirmation_token`)도 새지 않는다


def test_a_string_source_and_missing_fields_are_tolerated():
    d = json.loads(_query_args_summary({"source": "PM_X"}))
    assert d == {"table": "PM_X"}
    assert _query_args_summary({}) == "{}"


def test_select_columns_are_flattened_and_non_dict_aggregations_skipped():
    d = json.loads(_query_args_summary({"source": {"table": "T"}, "select": [{"column": "A"}, "B"], "aggregations": ["bad", {"function": "count"}]}))
    assert d["select"] == ["A", "B"] and d["aggregations"] == [{"function": "count"}]


def test_long_summaries_are_cut_at_500_characters_with_an_ellipsis():
    big = {"source": {"table": "T"}, "filters": {"all": [{"column": f"C{i}", "operator": "eq", "value": "x" * 40} for i in range(30)]}}
    s = _query_args_summary(big)
    assert QUERY_ARGS_LOG_MAX_CHARS == 500 and len(s) == 501 and s.endswith("…")
    assert len(_query_args_summary(ARGS)) <= 501


def test_korean_values_are_not_escaped():
    assert "品川区" in _query_args_summary({"source": "T", "filters": {"all": [{"column": "N", "value": "品川区"}]}})


# ── _log_tool_call ───────────────────────────────────────────────────────────

def test_a_query_call_gets_query_args_next_to_the_key_names(caplog):
    rec = _log(caplog, "query", ARGS)
    assert rec.argument_keys == sorted(ARGS) and json.loads(rec.query_args)["filters"]["all"][1]["value"] == 4
    assert not hasattr(rec, "arguments")                              # DEBUG 가 아니면 전체 인자는 내보내지 않는다


def test_other_tools_get_no_query_args(caplog):
    for tool in ("schema", "describe", "relationship"):
        rec = _log(caplog, tool, {"source": {"table": "T"}})
        assert not hasattr(rec, "query_args"), tool


def test_failed_queries_keep_their_args_so_the_bad_condition_is_visible(caplog):
    rec = _log(caplog, "query", ARGS, ok=False, code="INVALID_TOOL_ARGUMENTS")
    assert rec.levelno == logging.WARNING and rec.error_code == "INVALID_TOOL_ARGUMENTS"
    assert json.loads(rec.query_args)["table"] == "PM_LINK_CELL_NODE_1M"


def test_non_dict_arguments_never_break_the_log_line(caplog):
    rec = _log(caplog, "query", "깨진 인자")
    assert rec.argument_keys == [] and not hasattr(rec, "query_args")


def test_debug_level_keeps_the_full_arguments_instead(caplog):
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="datalens.agent"):
        BoundedAgent._log_tool_call(SimpleNamespace(session_id="dls_x"), 1, "query", ARGS, True, 1, False)
    rec = next(r for r in caplog.records if r.message == "tool_call")
    assert hasattr(rec, "arguments") and not hasattr(rec, "query_args")
