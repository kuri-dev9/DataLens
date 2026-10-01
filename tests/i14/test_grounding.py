"""ADR-034 인수 기준 GRD-AC-1~8 — 1단계 계측기는 재기만 하고 자르지 않는다."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import replace

import pytest

from datalens.application.agent import BoundedAgent
from datalens.application.grounding import collect_facts, measure, safe_measure
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.ports.llm import AssistantTurn, ToolCall
from datalens.ports.queryforge import QueryForgeResult, QueryForgeToolDefinition


QUERY_TOOL = QueryForgeToolDefinition(
    "query",
    "query",
    {
        "type": "object",
        "required": ["source", "select", "partition_scope"],
        "additionalProperties": False,
        "properties": {
            "source": {"type": "object", "required": ["table"], "properties": {"table": {"type": "string"}}},
            "select": {"type": "array", "minItems": 1},
            "partition_scope": {"type": "object", "required": ["kind"]},
            "preview_rows": {"type": "integer", "minimum": 1},
        },
    },
)


class FakeProvider:
    def __init__(self, turns) -> None:
        self.turns = deque(turns)

    async def complete(self, messages, tools, deadline, output_policy):
        return self.turns.popleft()

    async def close(self):
        return None


class FakeQueryForge:
    def __init__(self, results=()) -> None:
        self.results = deque(results)
        self.call_count = 0

    async def discover_tools(self, timeout_seconds=None):
        return (QUERY_TOOL,)

    async def call_tool(self, name, arguments, *, application_session_id, timeout_seconds):
        self.call_count += 1
        return self.results.popleft()


def pgw_result(qf_session="Q" * 22):
    return QueryForgeResult(
        True,
        qf_session,
        {
            "ok": True,
            "session_id": qf_session,
            "dataset_id": "ds_000000014",
            "row_count": 123,
            "columns": [{"name": "PGW_ID", "type": "Int64"}],
            "preview": [{"PGW_ID": 3, "TOTAL_DATA_USAGE": 455.10999999999996}],
            "warnings": [],
        },
    )


def query_call(content: str = "") -> AssistantTurn:
    return AssistantTurn(
        content,
        (ToolCall("c1", "query", {"source": {"table": "PM_CEI_PGW_5M"}, "select": ["PGW_ID"], "partition_scope": {"kind": "not_partitioned"}}),),
        "tool_calls",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- 순수 계측 로직 -------------------------------------------------------


def test_grd_ac1_ungrounded_number_is_recorded() -> None:
    facts: set[str] = set()
    collect_facts({"row_count": 123}, facts)
    report = measure("총 999999건입니다", facts)
    assert report["ungrounded"][0]["value"] == "999999"
    assert report["ungrounded_ratio_pct"] == 100.0


def test_grd_ac2_preview_and_row_count_are_grounded() -> None:
    facts: set[str] = set()
    collect_facts({"row_count": 123, "preview": [{"PGW_ID": 3}], "statistics": {"max": 987.6}}, facts)
    report = measure("PGW 3, 123행, 최대 987.6", facts)
    assert report["ungrounded"] == []
    assert report["grounded_ratio"] == 1.0


def test_grd_ac3_rounding_difference_is_grounded() -> None:
    facts: set[str] = set()
    collect_facts({"value": 455.10999999999996}, facts)
    assert measure("455.11입니다", facts)["ungrounded"] == []


def test_grd_ac8_deviation_separates_typo_from_fabrication() -> None:
    facts: set[str] = set()
    collect_facts({"row_count": 123}, facts)
    report = measure("124건과 999999건", facts)
    near, far = report["ungrounded"]
    assert near["nearest"] == 123.0 and near["deviation_pct"] < 1.0
    assert far["deviation_pct"] > 90.0
    assert report["max_deviation_pct"] == far["deviation_pct"]


def test_date_string_does_not_split_into_negative_numbers() -> None:
    """실측 회귀: upper_bound "2024-05-30"이 [2024, -05, -30]으로 쪼개져
    답변의 "2024년 5월 30일"이 통째로 미검증으로 잡히던 문제."""
    facts: set[str] = set()
    collect_facts({"upper_bound": "2024-05-30 12:00:00"}, facts)
    assert measure("2024년 5월 30일 기준입니다", facts)["ungrounded"] == []


def test_zero_padded_number_matches_bare_form() -> None:
    facts: set[str] = set()
    collect_facts({"code": "2024-05-30"}, facts)
    assert "5" in facts and "05" in facts


def test_repeated_claim_is_counted_once() -> None:
    """실측 회귀: 같은 미검증 값이 3회 반복 인용되면 3건으로 불어나
    ungrounded_ratio가 왜곡되던 문제. 주장 단위로 1건, occurrences로 횟수를 남긴다."""
    report = measure("총 999999건입니다. 다시 말하지만 999999건, 즉 999999건입니다", set())
    assert report["total"] == 1
    assert report["ungrounded"][0]["occurrences"] == 3
    assert report["ungrounded_ratio_pct"] == 100.0


def test_date_is_one_claim_not_three_numbers() -> None:
    """실측 회귀: "2024년 5월 30일"이 [2024, 5, 30] 3건으로 분해되던 문제.
    날짜는 주장 하나로 세고, 근거에 없는 성분만 missing으로 남긴다."""
    facts: set[str] = set()
    collect_facts({"upper_bound": "2024-06-01"}, facts)
    report = measure("데이터 범위가 2024년 5월 30일까지로 확인되었습니다", facts)
    assert report["total"] == 1
    entry = report["ungrounded"][0]
    assert entry["date"] is True
    assert "5월 30일" in entry["value"]
    assert entry["missing"] == ["5", "30"]  # 2024는 근거에 있다


def test_same_date_in_mixed_formats_dedupes() -> None:
    report = measure("2024-05-30 기준입니다. 즉 2024년 5월 30일입니다", set())
    assert report["total"] == 1
    assert report["ungrounded"][0]["occurrences"] == 2


def test_grounded_date_claim_counts_once() -> None:
    facts: set[str] = set()
    collect_facts({"upper_bound": "2024-05-30 12:00:00"}, facts)
    report = measure("2024년 5월 30일, 재차 2024-05-30 기준입니다", facts)
    assert report["ungrounded"] == []
    assert report["total"] == 1
    assert report["grounded_ratio"] == 1.0


def test_small_int_is_measured_but_flagged() -> None:
    report = measure("1단계와 2단계", set())
    assert [item["small_int"] for item in report["ungrounded"]] == [True, True]


def test_answer_without_numbers_reports_zero() -> None:
    report = measure("수치가 없는 답변입니다", set())
    assert report["total"] == 0
    assert report["grounded_ratio"] is None
    assert report["ungrounded_ratio_pct"] == 0.0


def test_grd_ac6_measure_failure_does_not_raise() -> None:
    assert safe_measure("123", None) is None  # type: ignore[arg-type]


# --- 에이전트 결합 --------------------------------------------------------


@pytest.mark.anyio
async def test_grd_ac2_tool_result_numbers_reach_the_fact_set() -> None:
    provider = FakeProvider([query_call(), AssistantTurn("PGW 3의 값은 455.11이고 총 123행입니다", (), "stop")])
    agent = BoundedAgent(provider, FakeQueryForge([pgw_result()]), max_tool_calls=3, recovery_budget=1)
    result = await agent.run(InMemorySessionStore(60).create(), "PGW별 사용량", time.monotonic() + 2)
    assert result.grounding["ungrounded"] == []
    assert result.grounding["grounded_ratio"] == 1.0


@pytest.mark.anyio
async def test_grd_ac1_fabricated_number_surfaces_in_metadata() -> None:
    provider = FakeProvider([query_call(), AssistantTurn("총 사용량은 88888입니다", (), "stop")])
    agent = BoundedAgent(provider, FakeQueryForge([pgw_result()]), max_tool_calls=3, recovery_budget=1)
    result = await agent.run(InMemorySessionStore(60).create(), "PGW별 사용량", time.monotonic() + 2)
    assert [item["value"] for item in result.grounding["ungrounded"]] == ["88888"]


@pytest.mark.anyio
async def test_grd_ac5_answer_is_never_modified() -> None:
    answer = "근거 없는 수치 77777이 들어간 답변"
    provider = FakeProvider([query_call(), AssistantTurn(answer, (), "stop")])
    agent = BoundedAgent(provider, FakeQueryForge([pgw_result()]), max_tool_calls=3, recovery_budget=1)
    result = await agent.run(InMemorySessionStore(60).create(), "질문", time.monotonic() + 2)
    assert result.answer == answer


@pytest.mark.anyio
async def test_grd_ac7_no_extra_upstream_calls() -> None:
    provider = FakeProvider([query_call(), AssistantTurn("123행", (), "stop")])
    qf = FakeQueryForge([pgw_result()])
    agent = BoundedAgent(provider, qf, max_tool_calls=3, recovery_budget=1)
    await agent.run(InMemorySessionStore(60).create(), "질문", time.monotonic() + 2)
    assert qf.call_count == 1
    assert not provider.turns


@pytest.mark.anyio
async def test_qf_empty_range_warning_reaches_warnings_and_fact_set() -> None:
    """QF_EMPTY_RANGE가 오면 ① result.warnings로 전달되고 ② 경고 속 가용 범위
    수치는 근거 집합에 들어가, 모델이 그 범위를 인용한 답변이 grounded로 계수된다."""
    qf_session = "Q" * 22
    empty_result = QueryForgeResult(
        True,
        qf_session,
        {
            "ok": True,
            "session_id": qf_session,
            "dataset_id": "ds_000000021",
            "row_count": 0,
            "columns": [],
            "preview": [],
            "warnings": [
                {
                    "code": "QF_EMPTY_RANGE",
                    "requested": {"column": "EVENT_TIME", "from": "2024-05-30", "to": "2024-05-30"},
                    "available": {"min": "2023-11-01", "max": "2024-05-28"},
                    "nearest_nonempty": "2024-05-28",
                }
            ],
        },
    )
    provider = FakeProvider(
        [query_call(), AssistantTurn("요청 기간에는 데이터가 없고, 보유 구간은 2024년 5월 28일까지입니다", (), "stop")]
    )
    agent = BoundedAgent(provider, FakeQueryForge([empty_result]), max_tool_calls=3, recovery_budget=1)
    result = await agent.run(InMemorySessionStore(60).create(), "통계 만들어줘", time.monotonic() + 2)
    assert [item["code"] for item in result.warnings] == ["QF_EMPTY_RANGE"]
    assert result.grounding["ungrounded"] == []


@pytest.mark.anyio
async def test_grd_ac4_previous_turn_answer_counts_as_evidence() -> None:
    store = InMemorySessionStore(60)
    session = replace(
        store.create(),
        turn_state=({"role": "user", "content": "이전 질문"}, {"role": "assistant", "content": "총 4242건이었습니다"}),
    )
    provider = FakeProvider([AssistantTurn("앞서 말한 4242건 그대로입니다", (), "stop")])
    agent = BoundedAgent(provider, FakeQueryForge(), max_tool_calls=3, recovery_budget=1)
    result = await agent.run(session, "다시 알려줘", time.monotonic() + 2)
    assert result.grounding["ungrounded"] == []
