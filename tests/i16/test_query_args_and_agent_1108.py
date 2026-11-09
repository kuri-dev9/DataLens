"""`query` 인자 정규화·바른 모양 안내·빈 출력 재시도·모드별 repeat_penalty — 2026-11-08.

graphio 일반 질의(18번 사건 "기지국별 영향 통계")에서 gemma4:12b 가 `query` 인자를 같은 자리에서 거듭 틀려
7번 호출 끝에 `DL_AGENT_LIMIT` 으로 멈췄다. 뜻이 같은 모양은 서버가 고치고(`query_args.normalize`),
못 고치는 것은 오류에 바른 모양을 붙인다(`shape_hint` · `fix:`). 본문도 도구 호출도 없는 생성은 한 번 다시 보낸다.
QueryForge 없이 — 에스터 재현 오류문에서 읽은 모양을 따라 만든 스키마와 가짜 프로바이더.
"""
from __future__ import annotations

import time
from collections import deque
from copy import deepcopy

import pytest

from datalens.application import query_args
from datalens.application import runtime_config as rc
from datalens.application.agent import AgentInvalidUpstreamResponse, BoundedAgent
from datalens.config import Settings
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.ports.llm import (
    AssistantTurn,
    LLMEmptyResponse,
    LLMInvalidResponse,
    ToolCall,
)
from datalens.ports.queryforge import QueryForgeFailure, QueryForgeResult, QueryForgeToolDefinition

normalize, shape_hint = query_args.normalize, query_args.shape_hint

COND_EQ = {"column": "PGW_ID", "operator": "eq", "value": "14"}


# ── normalize ─────────────────────────────────────────────────────────────────

def test_a_condition_list_is_wrapped_in_all():
    fixed, notes = normalize({"filters": [COND_EQ]})
    assert fixed["filters"] == {"all": [COND_EQ]} and notes == ["filters: 목록 → {all: 목록}"]


def test_a_single_condition_is_wrapped_in_all_list():
    fixed, notes = normalize({"filters": COND_EQ})
    assert fixed["filters"] == {"all": [COND_EQ]} and notes == ["filters: 조건 하나 → {all: [조건]}"]


def test_an_all_group_is_left_alone():
    args = {"filters": {"all": [COND_EQ]}}
    fixed, notes = normalize(args)
    assert fixed == args and notes == []


def test_a_dict_that_is_not_a_condition_is_not_wrapped():
    fixed, notes = normalize({"filters": {"any_groups": [[COND_EQ]]}})
    assert fixed["filters"] == {"any_groups": [[COND_EQ]]} and notes == []


def test_in_with_a_list_value_becomes_values():
    fixed, notes = normalize({"filters": {"all": [{"column": "C", "operator": "in", "value": ["a", "b"]}]}})
    assert fixed["filters"]["all"][0] == {"column": "C", "operator": "in", "values": ["a", "b"]}
    assert len(notes) == 1 and "in 의 value → values" in notes[0]


@pytest.mark.parametrize("op", ["in", "not_in"])
def test_a_scalar_value_becomes_a_one_element_list(op):
    fixed, _ = normalize({"filters": {"all": [{"column": "C", "operator": op, "value": 13101}]}})
    assert fixed["filters"]["all"][0]["values"] == [13101] and "value" not in fixed["filters"]["all"][0]


def test_values_already_present_is_not_touched():
    cond = {"column": "C", "operator": "in", "values": ["a"], "value": "stray"}
    fixed, notes = normalize({"filters": {"all": [cond]}})
    assert fixed["filters"]["all"][0] == cond and notes == []


def test_eq_keeps_its_value():
    fixed, notes = normalize({"filters": {"all": [COND_EQ]}})
    assert fixed["filters"]["all"][0] == COND_EQ and notes == []


def test_in_inside_any_groups_is_fixed():
    args = {"filters": {"any_groups": [[{"column": "C", "operator": "in", "value": ["a"]}], [COND_EQ]]}}
    fixed, notes = normalize(args)
    assert fixed["filters"]["any_groups"][0][0]["values"] == ["a"]
    assert fixed["filters"]["any_groups"][1][0] == COND_EQ
    assert notes == ["filters/any_groups/0/0: in 의 value → values"]


def test_in_inside_an_aggregation_where_is_fixed():
    args = {"aggregations": [{"function": "sum", "column": "X", "alias": "s",
                              "where": {"column": "C", "operator": "in", "value": "a"}}]}
    fixed, notes = normalize(args)
    assert fixed["aggregations"][0]["where"]["values"] == ["a"]
    assert notes == ["aggregations/0/where: in 의 value → values"]


@pytest.mark.parametrize("item", [{"column": "ENB_ID"}, {"column": "ENB_ID", "alias": "enb"}])
def test_a_column_object_in_group_by_becomes_the_column_name(item):
    fixed, notes = normalize({"group_by": [item, "CELLNUM"]})
    assert fixed["group_by"] == ["ENB_ID", "CELLNUM"] and notes == ["group_by/0: {column} 객체 → 열 이름 문자열"]


def test_a_bucket_object_in_group_by_is_kept():
    bucket = {"column": "EVENT_TIME", "bucket": "minute", "alias": "t"}
    fixed, notes = normalize({"group_by": [bucket]})
    assert fixed["group_by"] == [bucket] and notes == []


def test_a_group_by_object_with_other_keys_is_kept():
    odd = {"column": "ENB_ID", "extra": 1}
    fixed, _ = normalize({"group_by": [odd]})
    assert fixed["group_by"] == [odd]


def test_a_string_group_by_becomes_a_list():
    fixed, notes = normalize({"group_by": "ENB_ID"})
    assert fixed["group_by"] == ["ENB_ID"] and notes == ["group_by: 문자열 → [문자열]"]


def test_partition_scope_is_never_touched():
    scope = {"kind": "time_range", "from": "2026-11-08 09:39:00", "to": "2026-11-08 09:49:00"}
    fixed, notes = normalize({"partition_scope": scope})
    assert fixed["partition_scope"] == scope and "column" not in fixed["partition_scope"] and notes == []


def test_the_original_arguments_are_not_mutated():
    args = {"filters": [{"column": "C", "operator": "in", "value": ["a"]}], "group_by": [{"column": "X"}]}
    snapshot = deepcopy(args)
    normalize(args)
    assert args == snapshot


def test_nothing_to_fix_gives_no_notes_and_an_equal_copy():
    args = {"source": {"table": "T"}, "select": [{"column": "A"}], "group_by": ["A"]}
    fixed, notes = normalize(args)
    assert fixed == args and fixed is not args and notes == []


@pytest.mark.parametrize("bad", [None, "text", 3, ["x"]])
def test_non_dict_arguments_pass_through(bad):
    assert normalize(bad) == (bad, [])


def test_the_reproduced_call_is_fixed_in_all_three_places_but_not_the_missing_column():
    args = {"filters": [COND_EQ, {"column": "SHI", "operator": "in", "value": [13101, 13102]}],
            "group_by": [{"alias": "ENB_ID", "column": "ENB_ID"}],
            "partition_scope": {"kind": "time_range", "from": "a", "to": "b"}}
    fixed, notes = normalize(args)
    assert fixed["filters"]["all"][1]["values"] == [13101, 13102] and fixed["group_by"] == ["ENB_ID"]
    assert "column" not in fixed["partition_scope"] and len(notes) == 3


# ── shape_hint ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("head", ["filters", "group_by", "partition_scope", "aggregations", "order_by", "select"])
def test_each_known_head_gets_its_sentence(head):
    hint = shape_hint([f"{head}/0/x"])
    assert hint and hint.startswith(head + " ")


def test_hints_for_several_paths_are_joined_and_not_repeated():
    hint = shape_hint(["filters/all/2", "filters", "group_by/0"])
    assert hint.count("filters 는") == 1 and hint.count("group_by 는") == 1 and " || " in hint


def test_unknown_heads_and_empty_give_none():
    assert shape_hint(["source/table", "preview_rows"]) is None and shape_hint([]) is None


def test_the_partition_scope_hint_says_where_column_comes_from():
    assert "partition.keys" in shape_hint(["partition_scope"])


# ── _validate — fix: 문장과 2048자 상한 ───────────────────────────────────────

CONDITION = {
    "anyOf": [
        {"type": "object", "required": ["column", "operator", "value"], "additionalProperties": False,
         "properties": {"column": {"type": "string"}, "operator": {"enum": ["eq", "ne"]}, "value": {}}},
        {"type": "object", "required": ["column", "operator", "values"], "additionalProperties": False,
         "properties": {"column": {"type": "string"}, "operator": {"enum": ["in", "not_in"]},
                        "values": {"type": "array", "minItems": 1, "items": {"type": ["string", "number"]}}}},
    ]
}
QUERY_SCHEMA = {
    "type": "object",
    "required": ["source", "select", "partition_scope"],
    "additionalProperties": False,
    "properties": {
        "source": {"type": "object", "required": ["table"], "properties": {"table": {"type": "string"}}},
        "select": {"type": "array", "minItems": 1},
        "filters": {"type": "object", "required": ["all"], "additionalProperties": False,
                    "properties": {"all": {"type": "array", "items": CONDITION}}},
        "group_by": {"type": "array", "items": {"anyOf": [
            {"type": "string"},
            {"type": "object", "required": ["column", "bucket"], "properties": {
                "column": {"type": "string"}, "bucket": {"type": "string"}, "alias": {"type": "string"}}}]}},
        "aggregations": {"type": "array"},
        "order_by": {"type": "array"},
        "partition_scope": {"type": "object", "required": ["kind", "column"]},
    },
}
QUERY_TOOL = QueryForgeToolDefinition("query", "query", QUERY_SCHEMA)
SCHEMA_TOOL = QueryForgeToolDefinition(
    "schema", "schema",
    {"type": "object", "required": ["action"], "additionalProperties": False, "properties": {"action": {"enum": ["list_tables"]}}})

GOOD = {"source": {"table": "PM_LINK_CELL_NODE_1M"}, "select": [{"column": "ENB_ID"}],
        "partition_scope": {"kind": "time_range", "column": "EVENT_TIME", "from": "a", "to": "b"}}


def test_a_query_error_carries_the_correct_shape_after_fix():
    detail = BoundedAgent._validate(QUERY_TOOL, {**GOOD, "group_by": [{"column": "ENB_ID"}]})
    assert detail is not None and "| fix: group_by 는 열 이름 문자열 목록이다" in detail


def test_the_missing_partition_column_gets_the_partition_hint():
    scope = {"kind": "time_range", "from": "a", "to": "b"}
    detail = BoundedAgent._validate(QUERY_TOOL, {**GOOD, "partition_scope": scope})
    assert detail.startswith("partition_scope: 'column' is a required property")
    assert "fix: partition_scope 는" in detail and "partition.keys" in detail


def test_filters_list_error_gets_the_filters_hint():
    detail = BoundedAgent._validate(QUERY_TOOL, {**GOOD, "filters": [COND_EQ]})
    assert "fix: filters 는" in detail and "values 배열" in detail


def test_other_tools_get_no_fix_sentence():
    detail = BoundedAgent._validate(SCHEMA_TOOL, {"action": "nope"})
    assert detail is not None and "fix:" not in detail


def test_a_valid_query_has_no_error():
    assert BoundedAgent._validate(QUERY_TOOL, GOOD) is None


def test_the_detail_is_cut_at_2048_not_1024():
    # 오류가 많은 호출 — 항목 3건 + 모양 문장이 1024자를 넘어도 2048자까지는 남는다
    bad = {**GOOD, "group_by": [{"column": "X" * 300}], "filters": [{"column": "Y" * 300}],
           "partition_scope": {"kind": "time_range"}}
    detail = BoundedAgent._validate(QUERY_TOOL, bad)
    assert 1024 < len(detail) <= 2048


def test_a_non_object_is_rejected_plainly():
    assert BoundedAgent._validate(QUERY_TOOL, "{") == "arguments must be a JSON object"


# ── agent 루프 ────────────────────────────────────────────────────────────────

@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def assistant(text):
    return AssistantTurn(text, (), "stop")


def calling(name, arguments, call_id="c"):
    return AssistantTurn("", (ToolCall(call_id, name, arguments),), "tool_calls")


class FakeProvider:
    def __init__(self, turns):
        self.turns, self.messages, self.tools = deque(turns), [], []

    async def complete(self, messages, tools, deadline, output_policy):
        self.messages.append(list(messages))
        self.tools.append(tuple(tools))
        value = self.turns.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        return None


class FakeQueryForge:
    def __init__(self, results=()):
        self.results, self.calls = deque(results), []

    async def discover_tools(self, timeout_seconds=None):
        return (SCHEMA_TOOL, QUERY_TOOL)

    async def call_tool(self, name, arguments, *, application_session_id, timeout_seconds):
        self.calls.append((name, arguments))
        return self.results.popleft()


def ok_query(qf="Q" * 22):
    return QueryForgeResult(True, qf, {
        "ok": True, "session_id": qf, "dataset_id": "ds_000000001", "row_count": 1,
        "columns": [{"name": "ENB_ID", "type": "string"}], "preview": [{"ENB_ID": "786589"}], "warnings": []})


async def run(provider, qf, **kw):
    agent = BoundedAgent(provider, qf, max_tool_calls=kw.get("max_tool_calls", 4), recovery_budget=kw.get("recovery_budget", 2))
    return await agent.run(InMemorySessionStore(60).create(), "기지국별", time.monotonic() + 5)


@pytest.mark.anyio
async def test_a_wrongly_shaped_query_is_called_once_in_the_fixed_shape():
    wrong = {**GOOD, "filters": [{"column": "PGW_ID", "operator": "in", "value": ["14"]}],
             "group_by": [{"alias": "ENB_ID", "column": "ENB_ID"}]}
    provider = FakeProvider([calling("query", wrong), assistant("786589 가 많습니다")])
    qf = FakeQueryForge([ok_query()])
    result = await run(provider, qf)
    assert result.answer == "786589 가 많습니다" and result.tool_calls == 1 and result.recovery_count == 0
    (name, sent), = qf.calls
    assert name == "query"
    assert sent["filters"] == {"all": [{"column": "PGW_ID", "operator": "in", "values": ["14"]}]}
    assert sent["group_by"] == ["ENB_ID"]


@pytest.mark.anyio
async def test_the_tool_result_tells_the_model_what_was_fixed():
    provider = FakeProvider([calling("query", {**GOOD, "group_by": [{"column": "ENB_ID"}]}), assistant("끝")])
    await run(provider, FakeQueryForge([ok_query()]))
    tool_msg = provider.messages[1][-1]
    assert tool_msg.role == "tool" and "arguments_normalized" in tool_msg.content


@pytest.mark.anyio
async def test_nothing_fixed_means_no_normalized_note():
    provider = FakeProvider([calling("query", GOOD), assistant("끝")])
    await run(provider, FakeQueryForge([ok_query()]))
    assert "arguments_normalized" not in provider.messages[1][-1].content


@pytest.mark.anyio
async def test_a_missing_partition_column_is_not_guessed_and_comes_back_with_the_shape():
    scope = {"kind": "time_range", "from": "a", "to": "b"}
    provider = FakeProvider([calling("query", {**GOOD, "partition_scope": scope}), calling("query", GOOD), assistant("끝")])
    qf = FakeQueryForge([ok_query()])
    result = await run(provider, qf)
    assert len(qf.calls) == 1 and qf.calls[0][1]["partition_scope"]["column"] == "EVENT_TIME", "첫 호출은 QueryForge 로 가지 않았다"
    assert result.recovery_count == 1
    recovery = provider.messages[1][-1].content
    assert "INVALID_TOOL_ARGUMENTS" in recovery or "column" in recovery
    assert "fix: partition_scope 는" in recovery


@pytest.mark.anyio
async def test_other_tools_are_not_normalized():
    provider = FakeProvider([calling("schema", {"action": "list_tables"}), assistant("끝")])
    qf = FakeQueryForge([QueryForgeResult(True, "Q" * 22, {"ok": True, "session_id": "Q" * 22, "tables": [], "warnings": []})])
    await run(provider, qf)
    assert qf.calls == [("schema", {"action": "list_tables"})]


# ── 빈 출력 재시도 ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_one_empty_generation_is_retried_with_the_same_input_plus_a_nudge():
    provider = FakeProvider([LLMEmptyResponse("neither content nor tool calls"), assistant("답")])
    result = await run(provider, FakeQueryForge())
    assert result.answer == "답" and len(provider.messages) == 2
    first, second = provider.messages[0], provider.messages[1]
    assert [m.content for m in second[:-1]] == [m.content for m in first]          # 앞은 같고 끝에 안내 한 줄만 늘었다
    assert second[-1].role == "user" and second[-1].content.startswith("[DataLens 案内] 直前の出力を読み取れませんでした") \
        or second[-1].content.startswith("[DataLens 안내] 직전 출력을 읽지 못했습니다")


@pytest.mark.anyio
async def test_three_empty_generations_in_a_row_fail_as_invalid_response():
    # 첫 빈 출력은 같은 입력으로 재시도, 둘째는 도구 없이 마지막 답, 셋째도 비면 실패
    provider = FakeProvider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), LLMEmptyResponse("c")])
    with pytest.raises(AgentInvalidUpstreamResponse):
        await run(provider, FakeQueryForge())
    assert len(provider.messages) == 3


@pytest.mark.anyio
async def test_the_retry_is_once_per_turn_not_once_per_call():
    provider = FakeProvider([LLMEmptyResponse("a"), calling("query", GOOD), LLMEmptyResponse("b"),
                             LLMEmptyResponse("c")])
    with pytest.raises(AgentInvalidUpstreamResponse):
        await run(provider, FakeQueryForge([ok_query()]))


@pytest.mark.anyio
async def test_a_plain_invalid_response_is_not_retried():
    provider = FakeProvider([LLMInvalidResponse("bad json"), assistant("never")])
    with pytest.raises(AgentInvalidUpstreamResponse):
        await run(provider, FakeQueryForge())
    assert len(provider.messages) == 1


def test_the_empty_response_is_a_kind_of_invalid_response():
    assert issubclass(LLMEmptyResponse, LLMInvalidResponse)


# ── Ollama 프로바이더 ────────────────────────────────────────────────────────

def _ollama(response):
    import httpx
    from datalens.infrastructure.ollama import OllamaProvider

    async def handler(request):
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaProvider("http://ollama.test", "fixture-model", request_timeout_seconds=5, client=client), client


@pytest.mark.anyio
async def test_ollama_empty_body_raises_the_empty_response():
    from datalens.ports.llm import OutputPolicy
    adapter, client = _ollama({"message": {"role": "assistant", "content": ""}, "done": True})
    try:
        with pytest.raises(LLMEmptyResponse, match="neither content nor tool calls"):
            await adapter.complete([], (), time.monotonic() + 2, OutputPolicy())
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_ollama_answer_with_content_is_not_empty():
    from datalens.ports.llm import OutputPolicy
    adapter, client = _ollama({"message": {"role": "assistant", "content": "답"}, "done": True})
    try:
        turn = await adapter.complete([], (), time.monotonic() + 2, OutputPolicy())
    finally:
        await client.aclose()
    assert turn.content == "답"


# ── 모드별 repeat_penalty ────────────────────────────────────────────────────

def _cfg(**env):
    # 더미 키 — 실제 .env 를 읽지 않는다
    return rc.RuntimeConfig(Settings(_env_file=None, api_key="k", queryforge_api_key="q", **env))


def test_agent_defaults_to_1_1_and_evidence_stays_at_1_0():
    cfg = _cfg()
    assert cfg.profile("agent").params["repeat_penalty"] == 1.1
    assert cfg.profile("evidence").params["repeat_penalty"] == 1.0


def test_the_mode_default_table_only_touches_the_agent():
    assert rc._MODE_PARAM_DEFAULTS["agent"] == {"repeat_penalty": 1.1} and rc._MODE_PARAM_DEFAULTS["evidence"] == {}


def test_a_field_setting_beats_the_mode_default_in_both_modes():
    cfg = _cfg(ollama_repeat_penalty=1.0)
    assert cfg.profile("agent").params["repeat_penalty"] == 1.0
    cfg = _cfg(ollama_repeat_penalty=1.3)
    assert cfg.profile("agent").params["repeat_penalty"] == 1.3 and cfg.profile("evidence").params["repeat_penalty"] == 1.3


def test_a_screen_value_beats_both():
    cfg = _cfg(ollama_repeat_penalty=1.3)
    cfg.update("agent", None, {"repeat_penalty": 1.05})
    assert cfg.profile("agent").params["repeat_penalty"] == 1.05
    assert cfg.profile("evidence").params["repeat_penalty"] == 1.3


def test_clearing_the_screen_value_returns_to_the_mode_default():
    cfg = _cfg()
    cfg.update("agent", None, {"repeat_penalty": 1.5})
    cfg.update("agent", None, {"repeat_penalty": None})
    assert cfg.profile("agent").params["repeat_penalty"] == 1.1


def test_describe_reports_the_effective_value_and_source():
    d = _cfg().describe()["modes"]
    agent, ev = d["agent"]["params"]["repeat_penalty"], d["evidence"]["params"]["repeat_penalty"]
    assert agent["value"] == 1.1 and agent["env_or_default"] == 1.1 and agent["source"] == "default"
    assert ev["value"] == 1.0 and ev["env_or_default"] == 1.0 and ev["source"] == "default"


def test_describe_marks_a_field_setting_as_env():
    d = _cfg(ollama_repeat_penalty=1.0).describe()["modes"]["agent"]["params"]["repeat_penalty"]
    assert d["value"] == 1.0 and d["source"] == "env"


# ── 빈 출력이 두 번 이어지면 도구 없이 마지막 답 (2026-11-08 · dlr_rDTXUMnNJoKruy0q4b38rHCK) ──────────

def _ja_session():
    return InMemorySessionStore(60, "ko").create(locale="ja")


async def run_with(provider, qf, sess):
    return await BoundedAgent(provider, qf, max_tool_calls=4, recovery_budget=2).run(
        sess, "어떤 실패를 주로 했는지", time.monotonic() + 5)


@pytest.mark.anyio
async def test_two_empty_generations_then_a_tool_free_final_answer():
    provider = FakeProvider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), assistant("증거 값으로 답합니다")])
    result = await run(provider, FakeQueryForge())
    assert result.answer == "증거 값으로 답합니다" and len(provider.messages) == 3
    assert provider.tools[0] and provider.tools[1], "처음 두 번은 도구 목록을 보냈다"
    assert provider.tools[2] == (), "마지막 호출은 도구 목록이 비어 있다"


@pytest.mark.anyio
async def test_the_tool_free_call_carries_the_instruction_as_the_last_user_message():
    provider = FakeProvider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), assistant("답")])
    await run(provider, FakeQueryForge())
    last = provider.messages[2][-1]
    assert last.role == "user" and last.content.startswith("[DataLens 안내]")
    assert "도구를 더 부를 수 없습니다" in last.content and "조회하지 못한 것은 조회하지 못했다고" in last.content
    assert "받지 않은 수치는 쓰지 마세요" in last.content
    assert [m.content for m in provider.messages[2][:-1]] == [m.content for m in provider.messages[1]]


@pytest.mark.anyio
async def test_the_instruction_follows_the_session_language():
    provider = FakeProvider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), assistant("答")])
    await run_with(provider, FakeQueryForge(), _ja_session())
    last = provider.messages[2][-1]
    assert last.role == "user" and last.content.startswith("[DataLens 案内]") and "これ以上ツールは呼べません" in last.content


@pytest.mark.anyio
async def test_the_answer_carries_the_warning_that_no_tool_was_used_for_it():
    provider = FakeProvider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), assistant("답")])
    result = await run(provider, FakeQueryForge())
    codes = [w.get("code") for w in result.warnings if isinstance(w, dict)]
    assert codes == ["DATALENS_FINAL_WITHOUT_TOOLS"]
    assert [w for w in result.warnings][0]["action"] == "answered_without_tools"


@pytest.mark.anyio
async def test_a_normal_turn_and_a_single_retry_carry_no_such_warning():
    r1 = await run(FakeProvider([assistant("답")]), FakeQueryForge())
    r2 = await run(FakeProvider([LLMEmptyResponse("a"), assistant("답")]), FakeQueryForge())
    for r in (r1, r2):
        assert all(w.get("code") != "DATALENS_FINAL_WITHOUT_TOOLS" for w in r.warnings if isinstance(w, dict))


@pytest.mark.anyio
async def test_a_failed_query_then_two_empties_still_answers_without_tools():
    rejected = QueryForgeResult(False, "Q" * 22, {"ok": False}, QueryForgeFailure(
        "INVALID_QUERY_SPEC", "query requires select columns or aggregations", True, safe_metadata={"path": ["select"]}))
    provider = FakeProvider([calling("query", GOOD), LLMEmptyResponse("a"), LLMEmptyResponse("b"), assistant("조회는 실패했다")])
    result = await run(provider, FakeQueryForge([rejected]))
    assert result.answer == "조회는 실패했다" and result.tool_calls == 1
    assert provider.tools[3] == () and provider.messages[3][-1].role == "user"
    assert any(m.role == "tool" for m in provider.messages[3]), "지금까지의 도구 결과가 마지막 호출에 실린다"


@pytest.mark.anyio
async def test_a_tool_call_after_the_tools_were_withdrawn_is_dropped_when_there_is_a_body():
    both = AssistantTurn("본문으로 답합니다", (ToolCall("c", "query", GOOD),), "tool_calls")
    qf = FakeQueryForge([ok_query()])
    provider = FakeProvider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), both])
    result = await run(provider, qf)
    assert result.answer == "본문으로 답합니다" and qf.calls == [] and result.tool_calls == 0


@pytest.mark.anyio
async def test_a_tool_call_without_a_body_after_the_tools_were_withdrawn_fails():
    provider = FakeProvider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), calling("query", GOOD)])
    with pytest.raises(AgentInvalidUpstreamResponse):
        await run(provider, FakeQueryForge([ok_query()]))


@pytest.mark.anyio
async def test_the_log_shows_the_retry_before_the_tool_free_call(caplog):
    caplog.set_level("INFO")
    provider = FakeProvider([LLMEmptyResponse("a"), LLMEmptyResponse("b"), assistant("답")])
    await run(provider, FakeQueryForge())
    names = [r.getMessage() for r in caplog.records if r.getMessage() in ("llm_empty_retry", "llm_final_without_tools")]
    assert names == ["llm_empty_retry", "llm_final_without_tools"]
    warn = [r for r in caplog.records if r.getMessage() == "llm_final_without_tools"][0]
    assert warn.levelname == "WARNING"


# ── QueryForge 실행 단계 거절 — 이유와 바른 모양을 모델에 (2026-11-08) ──────────────────────────

def _reject(path=..., message="query requires select columns or aggregations", code="INVALID_QUERY_SPEC"):
    meta = {} if path is ... else {"path": path}
    return QueryForgeResult(False, "Q" * 22, {"ok": False}, QueryForgeFailure(code, message, True, safe_metadata=meta))


async def _recovery_payload(tool, args, rejected):
    import json
    provider = FakeProvider([calling(tool, args), assistant("끝")])
    qf = FakeQueryForge([rejected])
    await run(provider, qf)
    return json.loads(provider.messages[1][-1].content)["error"]


@pytest.mark.anyio
async def test_a_rejected_select_carries_the_reason_the_path_and_the_select_shape():
    err = await _recovery_payload("query", GOOD, _reject(["select"]))
    assert err["message"] == "query requires select columns or aggregations"
    assert err["details"]["path"] == ["select"]
    assert "select 와 aggregations 중 하나는 반드시 준다(둘 다 없으면 거절된다)" in err["fix"]


@pytest.mark.anyio
async def test_a_long_path_gets_the_hint_of_its_head():
    err = await _recovery_payload("query", GOOD, _reject(["filters", "all", 0], message="bad filter"))
    assert err["fix"].startswith("filters 는") and err["message"] == "bad filter"


@pytest.mark.anyio
async def test_a_path_with_an_unknown_head_has_no_fix():
    assert "fix" not in await _recovery_payload("query", GOOD, _reject(["source", "table"]))


@pytest.mark.anyio
@pytest.mark.parametrize("path", [..., [], "select", None])
async def test_a_missing_empty_or_malformed_path_has_no_fix(path):
    assert "fix" not in await _recovery_payload("query", GOOD, _reject(path))


@pytest.mark.anyio
async def test_other_tools_get_the_reason_but_never_a_fix():
    err = await _recovery_payload("schema", {"action": "list_tables"}, _reject(["select"], message="no such table"))
    assert err["message"] == "no such table" and "fix" not in err


def test_the_select_hint_says_one_of_select_or_aggregations_is_required():
    hint = shape_hint(["select"])
    assert "select 와 aggregations 중 하나는 반드시 준다" in hint and "group_by 에도 있어야 한다" in hint
