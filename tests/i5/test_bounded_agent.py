from __future__ import annotations

import time
from collections import deque
from dataclasses import replace

import pytest

from datalens.application.agent import (
    AgentInternalError,
    AgentPolicyError,
    AgentLimitError,
    AgentQueryRejected,
    AgentTimeoutError,
    AgentUpstreamUnavailable,
    BoundedAgent,
)
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.infrastructure.ollama import strip_thought_blocks
from datalens.ports.llm import AssistantTurn, LLMTimeout, LLMUnavailable, ToolCall
from datalens.ports.queryforge import (
    QueryForgeFailure,
    QueryForgeResult,
    QueryForgeToolDefinition,
)


SCHEMA_TOOL = QueryForgeToolDefinition(
    "schema",
    "schema",
    {
        "type": "object",
        "required": ["action"],
        "additionalProperties": False,
        "properties": {"action": {"enum": ["list_tables"]}},
    },
)
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


def assistant(content: str) -> AssistantTurn:
    return AssistantTurn(content, (), "stop")


def calling(name: str, arguments, call_id: str = "call") -> AssistantTurn:
    return AssistantTurn("", (ToolCall(call_id, name, arguments),), "tool_calls")


class FakeProvider:
    def __init__(self, turns) -> None:
        self.turns = deque(turns)
        self.messages = []

    async def complete(self, messages, tools, deadline, output_policy):
        self.messages.append(list(messages))
        value = self.turns.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        return None


class FakeQueryForge:
    def __init__(self, results=()) -> None:
        self.results = deque(results)
        self.calls = []

    async def discover_tools(self, timeout_seconds=None):
        return (SCHEMA_TOOL, QUERY_TOOL)

    async def call_tool(self, name, arguments, *, application_session_id, timeout_seconds):
        self.calls.append((name, arguments, application_session_id, timeout_seconds))
        value = self.results.popleft()
        if isinstance(value, Exception):
            raise value
        return value


def session():
    return InMemorySessionStore(60).create()


def ok_schema(qf_session="Q" * 22):
    return QueryForgeResult(True, qf_session, {"ok": True, "session_id": qf_session, "tables": [{"name": "events"}], "warnings": []})


def ok_query(qf_session="Q" * 22):
    return QueryForgeResult(
        True,
        qf_session,
        {
            "ok": True,
            "session_id": qf_session,
            "dataset_id": "ds_000000001",
            "row_count": 1,
            "columns": [{"name": "event_time", "type": "datetime"}],
            "preview": [{"event_time": "2026-09-08T00:00:00"}],
            "warnings": [],
        },
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_schema_then_final_answer() -> None:
    provider = FakeProvider([calling("schema", {"action": "list_tables"}), assistant("events가 있습니다")])
    qf = FakeQueryForge([ok_schema()])
    result = await BoundedAgent(provider, qf, max_tool_calls=3, recovery_budget=1).run(session(), "목록", time.monotonic() + 2)
    assert result.answer == "events가 있습니다"
    assert result.tool_calls == 1
    assert result.updated_session.queryforge_session_id == "Q" * 22
    assert provider.messages[1][-1].role == "tool"
    assert "session_id" not in provider.messages[1][-1].content


@pytest.mark.anyio
async def test_loc_ac2_ac3_sessions_select_prompts_independently() -> None:
    provider = FakeProvider([assistant("완료"), assistant("完了")])
    agent = BoundedAgent(provider, FakeQueryForge(), max_tool_calls=1, recovery_budget=0)
    ko_session = InMemorySessionStore(60, "ko").create()
    ja_session = InMemorySessionStore(60, "ko").create(locale="ja")

    await agent.run(ko_session, "질문", time.monotonic() + 2)
    await agent.run(ja_session, "質問", time.monotonic() + 2)

    ko_prompt = provider.messages[0][0].content
    ja_prompt = provider.messages[1][0].content
    assert "사용하세요" in ko_prompt
    assert "使用してください" in ja_prompt
    assert ko_prompt != ja_prompt
    for prompt in (ko_prompt, ja_prompt):
        assert "partition_scope" in prompt
        assert '"kind":"time_range"' in prompt
        assert '"kind":"not_partitioned"' in prompt
        assert "partitioned" in prompt


@pytest.mark.anyio
async def test_thk_ac4_multiturn_history_keeps_only_final_response() -> None:
    raw = "<|channel>thought 숨은 추론<channel|>첫 답변"
    first_provider = FakeProvider([assistant(strip_thought_blocks(raw))])
    first = await BoundedAgent(
        first_provider, FakeQueryForge(), max_tool_calls=1, recovery_budget=0
    ).run(session(), "첫 질문", time.monotonic() + 2)
    second_provider = FakeProvider([assistant("둘째 답변")])
    await BoundedAgent(
        second_provider, FakeQueryForge(), max_tool_calls=1, recovery_budget=0
    ).run(first.updated_session, "둘째 질문", time.monotonic() + 2)

    second_messages = second_provider.messages[0]
    assert any(message.content == "첫 답변" for message in second_messages)
    assert all("숨은 추론" not in message.content for message in second_messages)


@pytest.mark.anyio
async def test_schema_query_and_dataset_state_update() -> None:
    query = {
        "source": {"table": "events"},
        "select": [{"column": "event_time"}],
        "partition_scope": {"kind": "time_range", "from": "2026-09-08", "to": "2026-09-08"},
    }
    provider = FakeProvider([calling("schema", {"action": "list_tables"}), calling("query", query), assistant("한 건입니다")])
    qf = FakeQueryForge([ok_schema(), ok_query()])
    result = await BoundedAgent(provider, qf, max_tool_calls=3, recovery_budget=1).run(session(), "조회", time.monotonic() + 2)
    assert result.tool_calls == 2
    assert result.datasets[0].dataset_id == "ds_000000001"
    assert result.updated_session.active_dataset_id == "ds_000000001"
    assert result.updated_session.active_table == "events"
    assert result.updated_session.active_period == {"from": "2026-09-08", "to": "2026-09-08"}
    assert qf.calls[1][1]["preview_rows"] == 5


@pytest.mark.anyio
async def test_agent_clamps_model_requested_preview_to_context_policy() -> None:
    query = {
        "source": {"table": "events"},
        "select": [{"column": "event_time"}],
        "partition_scope": {"kind": "not_partitioned"},
        "preview_rows": 100,
    }
    qf = FakeQueryForge([ok_query()])
    await BoundedAgent(FakeProvider([calling("query", query), assistant("완료")]), qf, max_tool_calls=3, recovery_budget=1, preview_rows=5).run(session(), "조회", time.monotonic() + 2)
    assert qf.calls[0][1]["preview_rows"] == 5


@pytest.mark.anyio
async def test_model_cannot_override_queryforge_application_session() -> None:
    query_tool = QueryForgeToolDefinition(
        "query",
        "query",
        {
            **QUERY_TOOL.input_schema,
            "properties": {**QUERY_TOOL.input_schema["properties"], "session_id": {"type": "string"}},
        },
    )
    query = {
        "session_id": "attacker-selected-session",
        "source": {"table": "events"},
        "select": [{"column": "event_time"}],
        "partition_scope": {"kind": "not_partitioned"},
    }
    qf = FakeQueryForge([ok_query("Q" * 22)])

    async def discovered(timeout_seconds=None):
        return (SCHEMA_TOOL, query_tool)

    qf.discover_tools = discovered
    existing = replace(session(), queryforge_session_id="Q" * 22)
    await BoundedAgent(FakeProvider([calling("query", query), assistant("완료")]), qf, max_tool_calls=3, recovery_budget=1).run(existing, "조회", time.monotonic() + 2)
    assert "session_id" not in qf.calls[0][1]
    assert qf.calls[0][2] == "Q" * 22


@pytest.mark.anyio
async def test_invalid_arguments_are_recovered_locally_without_qf_call() -> None:
    provider = FakeProvider([calling("schema", {}), calling("schema", {"action": "list_tables"}), assistant("완료")])
    qf = FakeQueryForge([ok_schema()])
    result = await BoundedAgent(provider, qf, max_tool_calls=3, recovery_budget=1).run(session(), "목록", time.monotonic() + 2)
    assert result.recovery_count == 1
    assert result.tool_calls == 2
    assert len(qf.calls) == 1
    assert "INVALID_TOOL_ARGUMENTS" in provider.messages[1][-1].content


@pytest.mark.anyio
async def test_recovery_budget_exhaustion() -> None:
    provider = FakeProvider([calling("schema", {}), calling("schema", {})])
    with pytest.raises(AgentLimitError, match="Recovery"):
        await BoundedAgent(provider, FakeQueryForge(), max_tool_calls=3, recovery_budget=1).run(session(), "x", time.monotonic() + 2)


@pytest.mark.anyio
async def test_tool_budget_exhaustion() -> None:
    provider = FakeProvider([calling("schema", {"action": "list_tables"})] * 4)
    qf = FakeQueryForge([ok_schema(), ok_schema(), ok_schema()])
    with pytest.raises(AgentLimitError, match="Tool call"):
        await BoundedAgent(provider, qf, max_tool_calls=3, recovery_budget=1).run(session(), "x", time.monotonic() + 2)
    assert len(qf.calls) == 3


@pytest.mark.anyio
async def test_unknown_tool_is_not_recovered_or_called() -> None:
    qf = FakeQueryForge()
    with pytest.raises(AgentPolicyError):
        await BoundedAgent(FakeProvider([calling("shell", {})]), qf, max_tool_calls=3, recovery_budget=1).run(session(), "x", time.monotonic() + 2)
    assert qf.calls == []


@pytest.mark.anyio
async def test_single_candidate_queryforge_error_is_recovered() -> None:
    failure = QueryForgeFailure("UNKNOWN_COLUMN", "bad", True, candidates=("event_time",))
    rejected = QueryForgeResult(False, "Q" * 22, {"ok": False}, failure)
    provider = FakeProvider([calling("schema", {"action": "list_tables"}), calling("schema", {"action": "list_tables"}), assistant("완료")])
    qf = FakeQueryForge([rejected, ok_schema()])
    result = await BoundedAgent(provider, qf, max_tool_calls=3, recovery_budget=1).run(session(), "x", time.monotonic() + 2)
    assert result.recovery_count == 1
    assert len(qf.calls) == 2


@pytest.mark.anyio
async def test_err_ac_n3_retryable_partition_error_is_returned_to_model_and_retried() -> None:
    failure = QueryForgeFailure(
        "MISSING_PARTITION_SCOPE",
        "partition scope mismatch",
        True,
        hint='Use {"kind":"not_partitioned"}',
        safe_metadata={"table": "PM_CEI_PGW_5M", "expected_kind": "not_partitioned"},
    )
    rejected = QueryForgeResult(False, "Q" * 22, {"ok": False}, failure)
    query = {
        "source": {"table": "PM_CEI_PGW_5M"},
        "select": [{"column": "DATA_CONN_ATTEMPT_CNT"}],
        "partition_scope": {"kind": "not_partitioned"},
    }
    provider = FakeProvider([calling("query", query), calling("query", query), assistant("완료")])
    qf = FakeQueryForge([rejected, ok_query()])
    result = await BoundedAgent(provider, qf, max_tool_calls=3, recovery_budget=1).run(
        session(), "조회", time.monotonic() + 2
    )
    payload = __import__("json").loads(provider.messages[1][-1].content)
    assert result.recovery_count == 1
    assert len(qf.calls) == 2
    assert payload["error"] == {
        "code": "MISSING_PARTITION_SCOPE",
        "retryable": True,
        "hint": 'Use {"kind":"not_partitioned"}',
        "details": {"table": "PM_CEI_PGW_5M", "expected_kind": "not_partitioned"},
    }


@pytest.mark.anyio
async def test_multiple_candidates_are_not_recovered() -> None:
    failure = QueryForgeFailure("UNKNOWN_COLUMN", "bad", False, candidates=("a", "b"))
    rejected = QueryForgeResult(False, "Q" * 22, {"ok": False}, failure)
    with pytest.raises(AgentQueryRejected):
        await BoundedAgent(FakeProvider([calling("schema", {"action": "list_tables"})]), FakeQueryForge([rejected]), max_tool_calls=3, recovery_budget=1).run(session(), "x", time.monotonic() + 2)


@pytest.mark.anyio
async def test_llm_and_queryforge_timeouts_are_normalized() -> None:
    with pytest.raises(AgentTimeoutError):
        await BoundedAgent(FakeProvider([LLMTimeout("late")]), FakeQueryForge(), max_tool_calls=3, recovery_budget=1).run(session(), "x", time.monotonic() + 2)
    with pytest.raises(AgentTimeoutError):
        await BoundedAgent(FakeProvider([calling("schema", {"action": "list_tables"})]), FakeQueryForge([TimeoutError("late")]), max_tool_calls=3, recovery_budget=1).run(session(), "x", time.monotonic() + 2)


@pytest.mark.anyio
async def test_expired_before_llm_and_state_preserved_on_failure() -> None:
    original = session()
    provider = FakeProvider([assistant("never")])
    with pytest.raises(AgentTimeoutError):
        await BoundedAgent(provider, FakeQueryForge(), max_tool_calls=3, recovery_budget=1).run(original, "x", time.monotonic() - 1)
    assert provider.messages == []
    assert original.queryforge_session_id is None
    assert original.turn_state == ()


@pytest.mark.anyio
async def test_provider_failure_does_not_change_session() -> None:
    original = session()
    with pytest.raises(AgentUpstreamUnavailable):
        await BoundedAgent(FakeProvider([LLMUnavailable("down")]), FakeQueryForge(), max_tool_calls=3, recovery_budget=1).run(original, "x", time.monotonic() + 2)
    assert original.turn_state == ()
