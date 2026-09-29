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
