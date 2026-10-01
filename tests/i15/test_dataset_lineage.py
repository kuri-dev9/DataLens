"""ADR-035 — 세션 데이터셋 계보: 모델이 과거 데이터셋을 지어내지 않고 재사용한다."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import replace

import pytest

from datalens.application.agent import BoundedAgent, DatasetReference, _LINEAGE_LIMIT
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

    async def discover_tools(self, timeout_seconds=None):
        return (QUERY_TOOL,)

    async def call_tool(self, name, arguments, *, application_session_id, timeout_seconds):
        return self.results.popleft()


def query_result(dataset_id: str = "ds_000000014", row_count: int = 123):
    qf_session = "Q" * 22
    return QueryForgeResult(
        True,
        qf_session,
        {
            "ok": True,
            "session_id": qf_session,
            "dataset_id": dataset_id,
            "row_count": row_count,
            "columns": [{"name": "PGW_ID", "type": "Int64"}],
            "preview": [{"PGW_ID": 3}],
            "warnings": [],
        },
    )


def query_call() -> AssistantTurn:
    return AssistantTurn(
        "",
        (ToolCall("c1", "query", {"source": {"table": "PM_CEI_PGW_5M"}, "select": ["PGW_ID"], "partition_scope": {"kind": "not_partitioned"}}),),
        "tool_calls",
    )


def make_agent(provider, queryforge) -> BoundedAgent:
    return BoundedAgent(provider, queryforge, max_tool_calls=3, recovery_budget=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_lin_ac1_query_dataset_is_recorded_in_lineage() -> None:
    provider = FakeProvider([query_call(), AssistantTurn("완료", (), "stop")])
    agent = make_agent(provider, FakeQueryForge([query_result()]))
    result = await agent.run(InMemorySessionStore(60).create(), "통계", time.monotonic() + 2)
    assert result.updated_session.dataset_lineage == (
        {"dataset_id": "ds_000000014", "action": "query", "table": "PM_CEI_PGW_5M", "row_count": 123},
    )
    assert result.updated_session.active_dataset_id == "ds_000000014"


@pytest.mark.anyio
async def test_lin_ac2_lineage_appears_in_next_turn_context() -> None:
    provider = FakeProvider([query_call(), AssistantTurn("완료", (), "stop")])
    agent = make_agent(provider, FakeQueryForge([query_result()]))
    result = await agent.run(InMemorySessionStore(60).create(), "통계", time.monotonic() + 2)
    messages = agent._context(result.updated_session, "이어서 분석해줘")
    context = next(m.content for m in messages if m.role == "system" and m.content.startswith("DataLens session context:"))
    assert "ds_000000014" in context
    assert '"action": "query"' in context or '"action":"query"' in context.replace(" ", "")


@pytest.mark.anyio
async def test_lin_ac3_lineage_row_count_is_grounding_evidence_next_turn() -> None:
    """계보의 수치는 다음 턴 근거 집합에 들어간다 — 재인용이 미검증 오탐이 되지 않는다."""
    provider = FakeProvider([query_call(), AssistantTurn("완료", (), "stop")])
    agent = make_agent(provider, FakeQueryForge([query_result(row_count=4242)]))
    first = await agent.run(InMemorySessionStore(60).create(), "통계", time.monotonic() + 2)
    provider2 = FakeProvider([AssistantTurn("앞선 결과는 4242행이었습니다", (), "stop")])
    agent2 = make_agent(provider2, FakeQueryForge())
    second = await agent2.run(first.updated_session, "몇 행이었지?", time.monotonic() + 2)
    assert second.grounding["ungrounded"] == []


def test_lin_ac4_lineage_is_capped_and_reuse_moves_to_end() -> None:
    store = InMemorySessionStore(60)
    old = tuple({"dataset_id": f"ds_{index:09d}", "action": "query"} for index in range(_LINEAGE_LIMIT))
    session = replace(store.create(), dataset_lineage=old)
    updated = BoundedAgent._updated_session(
        session, "질문", "답변", None,
        ({"dataset_id": "ds_000000001", "action": "transform", "parent_dataset_id": "ds_000000000"},
         {"dataset_id": "ds_new000001", "action": "query"}),
        None, None,
    )
    lineage = updated.dataset_lineage
    assert len(lineage) == _LINEAGE_LIMIT
    # 재사용된 ds_000000001은 최신 쪽으로 옮겨지고 내용이 갱신된다
    assert lineage[-2]["dataset_id"] == "ds_000000001"
    assert lineage[-2]["action"] == "transform"
    assert lineage[-1]["dataset_id"] == "ds_new000001"
    # 상한을 넘긴 가장 오래된 항목부터 떨어진다
    assert all(item["dataset_id"] != "ds_000000000" for item in lineage)


def test_lin_ac5_parent_dataset_id_is_preserved() -> None:
    result = QueryForgeResult(
        True, "Q" * 22,
        {"ok": True, "dataset_id": "ds_000000002", "parent_dataset_id": "ds_000000001", "row_count": 7},
    )
    reference = DatasetReference(dataset_id="ds_000000002", row_count=7)
    entry = BoundedAgent._lineage_entry("transform", {"dataset_id": "ds_000000001"}, result, reference)
    assert entry == {
        "dataset_id": "ds_000000002",
        "action": "transform",
        "parent_dataset_id": "ds_000000001",
        "row_count": 7,
    }
