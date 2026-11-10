"""프롬프트 실험 ① — 원문 보관(agent_traces) · 다시 보내기(LabService) · API (2026-11-10 · 맥스 구현 고정).

* sqlite: 새 표 두 개 · 200건 상한 · `agent_trace_keep=0` 이면 저장 안 함 · evidence_turns 는 그대로.
* TurnLog: agent 턴 저장 · 저장 실패해도 답은 나간다.
* chat: `active_turns()` 는 진행 중에만 차고 예외 뒤에도 빈다 · trace 에 warnings·locale.
* Profile/agent: `prompt_extra` 가 비면 지시문이 같다 · 그 언어 칸만 끝에 붙는다.
* lab: 입력 검사 · 사용 중이면 LabBusy · 임시 세션 정리 · 운영 기록에 아무것도 안 늘어난다.
* API: 라우트 · 409/400/404 · API 키.
가짜 프로바이더·가짜 QueryForge — 네트워크·모델 없음.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque

import pytest
from starlette.testclient import TestClient

from conftest import ReadyProbe
from datalens.api.app import build_app
from datalens.application.agent import AgentError, BoundedAgent
from datalens.application.chat import ChatApplicationService
from datalens.application.lab import LabBusy, LabError, LabService, PROMPT_EXTRA_MAX_CHARS, steps_from_blocks
from datalens.application.runtime_config import Profile
from datalens.application.sessions import SessionService
from datalens.application.trace import TurnLog
from datalens.config import Settings
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.infrastructure.sqlite_turns import SqliteTurnStore
from datalens.ports.llm import AssistantTurn, TokenUsage, ToolCall
from datalens.ports.queryforge import QueryForgeResult, QueryForgeToolDefinition
from datalens.prompts import load_system_prompt

HEADERS = {"x-api-key": "data-secret"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── 가짜 ────────────────────────────────────────────────────────────────────
SCHEMA = {"type": "object", "properties": {k: {} for k in ("source", "partition_scope", "aggregations")}, "required": []}
TOOLS = (QueryForgeToolDefinition("query", "query", SCHEMA),)


class P:
    def __init__(self, turns):
        self.turns, self.seen = deque(turns), []

    async def complete(self, messages, tools, deadline, policy):
        self.seen.append(list(messages))
        v = self.turns.popleft()
        if isinstance(v, Exception):
            raise v
        return v

    async def complete_structured(self, *a):
        return {}, TokenUsage()

    async def close(self):
        return None


class QF:
    async def discover_tools(self, timeout_seconds=None):
        return TOOLS

    async def call_tool(self, name, arguments, *, application_session_id, timeout_seconds):
        return QueryForgeResult(True, "Q" * 22, {"ok": True, "session_id": "Q" * 22, "dataset_id": "ds_000000001", "row_count": 1,
                                                 "columns": [{"name": "fail_cnt", "type": "integer"}],
                                                 "preview": [{"fail_cnt": 4257}], "warnings": []})


def final(text="답"):
    return AssistantTurn(text, (), "stop")


def query_turn():
    return AssistantTurn("", (ToolCall(call_id="c1", name="query", arguments={
        "source": {"table": "T"}, "partition_scope": {"kind": "time_range", "column": "EVENT_TIME", "from": "a", "to": "b"},
        "aggregations": [{"function": "count", "alias": "fail_cnt"}]}),), "tool_calls")


class Env:
    def __init__(self, turns, keep=200, runtime=None):
        self.store = SqliteTurnStore(":memory:", agent_keep=keep)
        self.turn_log = TurnLog(5, self.store)
        self.provider = P(turns)
        self.agent = BoundedAgent(self.provider, QF(), max_tool_calls=6, recovery_budget=4)
        self.sessions = SessionService(InMemorySessionStore(60))
        self.chat = ChatApplicationService(self.sessions, self.agent, None, runtime, self.turn_log)
        self.settings = Settings(api_key="data-secret", queryforge_api_key="q", queryforge_endpoint="http://x/mcp")
        self.lab = LabService(self.agent, self.sessions, runtime, self.turn_log, self.chat, self.settings)


async def chat_turn(env, text="こんにちは", locale="ja", rid="r1"):
    s = env.sessions.create(locale=locale)
    return await env.chat.handle(s, text, rid, time.monotonic() + 5)


# ── sqlite ──────────────────────────────────────────────────────────────────

def test_tables_created_and_existing_evidence_rows_untouched(tmp_path):
    path = tmp_path / "m.sqlite3"
    s1 = SqliteTurnStore(path)
    names = {r[0] for r in s1._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"evidence_turns", "agent_traces", "lab_runs"} <= names
    s1.close()
    s2 = SqliteTurnStore(path)                                   # 다시 열어도 CREATE IF NOT EXISTS 라 안 깨진다
    assert s2.recent_agent() == [] and s2.lab_runs() == []


def test_agent_traces_keep_latest_n_only():
    st = SqliteTurnStore(":memory:", agent_keep=3)
    for i in range(5):
        st.save_agent({"at": f"2026-11-10T10:00:0{i}", "session_id": f"s{i}", "mode": "agent"})
    rows = st.recent_agent()
    assert [r["session_id"] for r in rows] == ["s4", "s3", "s2"]
    assert st.agent_trace(rows[0]["id"])["session_id"] == "s4" and st.agent_trace(1) is None


def test_agent_keep_zero_saves_nothing():
    st = SqliteTurnStore(":memory:", agent_keep=0)
    st.save_agent({"at": "2026-11-10T10:00:00", "session_id": "s"})
    assert st.recent_agent() == []


def test_default_keeps_are_200():
    from datalens.infrastructure import sqlite_turns as m
    assert m.AGENT_KEEP == 200 and m.LAB_KEEP == 200 and Settings(api_key="data-secret", queryforge_api_key="q", queryforge_endpoint="http://x/mcp").agent_trace_keep == 200


def test_lab_runs_keep_latest_and_roundtrip():
    st = SqliteTurnStore(":memory:", lab_keep=2)
    ids = [st.save_lab("agent", i, {"message": f"m{i}"}, {"mode": "agent", "answer": "한글"}, "t") for i in range(3)]
    rows = st.lab_runs()
    assert [r["id"] for r in rows] == [ids[2], ids[1]] and st.lab_run(ids[0]) is None
    assert st.lab_run(ids[2])["result"]["answer"] == "한글" and st.lab_run(ids[2])["request"]["message"] == "m2"


# ── TurnLog ─────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_agent_turn_is_saved_with_locale_and_warnings():
    env = Env([query_turn(), final("끝")])
    await chat_turn(env)
    (row,) = env.store.recent_agent()
    assert row["mode"] == "agent" and row["locale"] == "ja" and "warnings" in row["response"] and row["response"]["answer"] == "끝"
    assert env.store.recent() == []                                # evidence_turns 는 안 늘었다


@pytest.mark.anyio
async def test_store_failure_does_not_block_the_answer():
    env = Env([final("답")])

    def boom(trace):
        raise RuntimeError("disk full")
    env.store.save_agent = boom
    out = await chat_turn(env)
    assert out["answer"] == "답"


# ── chat.active_turns ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_active_turns_filled_only_while_running_and_empty_after_exception():
    env = Env([final("a")])
    seen = {}
    real = env.agent.run

    async def spy(*a, **k):
        seen["during"] = env.chat.active_turns()
        return await real(*a, **k)
    env.agent.run = spy
    await chat_turn(env, rid="rA")
    assert [t["request_id"] for t in seen["during"]] == ["rA"] and seen["during"][0]["mode"] == "agent"
    assert env.chat.active_turns() == []

    async def bad(*a, **k):
        seen["bad"] = env.chat.active_turns()
        raise AgentError("x")
    env.agent.run = bad
    with pytest.raises(AgentError):
        await chat_turn(env, rid="rB")
    assert len(seen["bad"]) == 1 and env.chat.active_turns() == []


# ── Profile · prompt_extra ──────────────────────────────────────────────────

def base_profile(**kw):
    return Profile(mode="agent", system_prompt=True, session_context=False, memory=False, tools="full", history_items=6, params={}, **kw)


def test_public_unchanged_when_prompt_extra_empty():
    assert "prompt_extra" not in base_profile().public()
    assert "prompt_extra" not in base_profile(prompt_extra={"ko": "", "ja": ""}).public()
    assert base_profile(prompt_extra={"ja": "x"}).public()["prompt_extra"] == {"ja": "x"}


async def system_of(env_turns, profile, locale):
    env = Env(env_turns)
    s = env.sessions.create(locale=locale)
    await env.agent.run(s, "質問", time.monotonic() + 5, mode="agent", profile=profile)
    return next(m.content for m in env.provider.seen[0] if m.role == "system")


@pytest.mark.anyio
async def test_empty_extra_keeps_system_prompt_identical_and_extra_goes_to_end_for_that_locale_only():
    plain = await system_of([final()], base_profile(), "ja")
    assert plain.startswith(load_system_prompt("ja", "system")[:50])
    assert await system_of([final()], base_profile(prompt_extra={"ja": "  "}), "ja") == plain
    with_ja = await system_of([final()], base_profile(prompt_extra={"ja": "追加の指示XYZ"}), "ja")
    assert with_ja == f"{plain}\n\n追加の指示XYZ"
    assert await system_of([final()], base_profile(prompt_extra={"ko": "한국어 지시"}), "ja") == plain   # 다른 언어 칸은 안 붙는다


# ── LabService ──────────────────────────────────────────────────────────────

REQ = {"mode": "agent", "locale": "ja", "message": "質問です"}


@pytest.mark.anyio
@pytest.mark.parametrize("patch,msg", [({"mode": "zzz"}, "mode"), ({"locale": "en"}, "locale"), ({"message": "  "}, "message"),
                                       ({"prompt_extra": "x" * (PROMPT_EXTRA_MAX_CHARS + 1)}, "prompt_extra"),
                                       ({"prompt_extra": 5}, "prompt_extra"),
                                       ({"history": "no"}, "history"),
                                       ({"history": [{"role": "system", "content": "x"}]}, "history"),
                                       ({"history": [{"role": "user", "content": 1}]}, "history"),
                                       ({"history": [{"role": "user", "content": "a"}] * 50}, "history")])
async def test_run_rejects_bad_input(patch, msg):
    env = Env([final()])
    with pytest.raises(LabError) as e:
        await env.lab.run({**REQ, **patch}, time.monotonic() + 5)
    assert msg in str(e.value)
    assert env.provider.seen == []                               # 모델은 부르지 않았다


@pytest.mark.anyio
async def test_run_success_saves_only_lab_and_cleans_temp_session():
    env = Env([query_turn(), final("結果")])
    out = await env.lab.run({**REQ, "prompt_extra": "実験用の指示", "history": [{"role": "user", "content": "前"}, {"role": "assistant", "content": "答"}]},
                            time.monotonic() + 5)
    r = out["result"]
    assert r["answer"] == "結果" and r["tool_calls"] == 1 and r["mode"] == "agent" and r["locale"] == "ja"
    step = r["steps"][0]
    assert step["tool"] == "query" and step["preview"] == [{"fail_cnt": 4257}] and "not an event/failure count" in step["aggregation_meaning"][0]
    system = next(m.content for m in env.provider.seen[0] if m.role == "system")
    assert system.endswith("実験用の指示")
    assert any(m.content == "前" for m in env.provider.seen[0])    # 이전 문답이 채워졌다
    assert env.sessions._store.all() == [] if hasattr(env.sessions._store, "all") else True
    # 운영 기록에 아무것도 안 늘었다
    assert env.store.recent_agent() == [] and env.store.recent() == [] and env.turn_log.history() == []
    assert [x["id"] for x in env.store.lab_runs()] == [out["id"]]
    assert env.lab.busy()["busy"] is False


@pytest.mark.anyio
async def test_agent_error_is_saved_as_error_result_and_session_cleaned():
    env = Env([AgentError("터졌다")])
    # AgentError 를 직접 던지는 프로바이더 대신 agent.run 을 바꾼다
    async def bad(*a, **k):
        e = AgentError("터졌다", tool_calls=2)
        e.warnings = ({"code": "W1"},)
        raise e
    env.agent.run = bad
    out = await env.lab.run(REQ, time.monotonic() + 5)
    assert out["result"]["error"]["type"] == "AgentError" and out["result"]["tool_calls"] == 2 and out["result"]["warnings"] == ["W1"]
    assert env.store.lab_run(out["id"])["result"]["error"]["message"] == "터졌다"
    assert env.sessions._store.all() == []


@pytest.mark.anyio
async def test_lab_busy_when_real_turn_active_or_lab_running():
    env = Env([final()])
    env.chat._active["real"] = (time.monotonic(), "agent")
    with pytest.raises(LabBusy) as e:
        await env.lab.run(REQ, time.monotonic() + 5)
    assert e.value.active[0]["request_id"] == "real" and not e.value.lab_running and env.provider.seen == []
    env.chat._active.clear()

    gate = asyncio.Event()
    real = env.agent.run

    async def slow(*a, **k):
        await gate.wait()
        return await real(*a, **k)
    env.agent.run = slow
    first = asyncio.create_task(env.lab.run(REQ, time.monotonic() + 5))
    await asyncio.sleep(0.05)
    assert env.lab.busy()["lab"] is not None and env.lab.busy()["busy"]
    with pytest.raises(LabBusy) as e2:
        await env.lab.run(REQ, time.monotonic() + 5)
    assert e2.value.lab_running
    gate.set()
    await first
    assert env.lab.busy()["busy"] is False


@pytest.mark.anyio
async def test_real_turn_arriving_while_waiting_for_lock_wins():
    env = Env([final()])
    # 잠금을 잡은 뒤 실제 턴이 들어온 경우 — 두 번째 검사가 막는다
    class Chat:
        n = 0

        def active_turns(self):
            Chat.n += 1
            return [] if Chat.n == 1 else [{"request_id": "late", "mode": "agent", "elapsed_s": 0}]
    env.lab._chat = Chat()
    with pytest.raises(LabBusy):
        await env.lab.run(REQ, time.monotonic() + 5)
    assert env.provider.seen == [] and env.lab.busy()["lab"] is None


# ── sources / source ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_sources_and_source_for_agent_trace():
    env = Env([query_turn(), final("끝")])
    await chat_turn(env, text="セルは?")
    (src,) = [r for r in env.lab.sources() if r["kind"] == "agent"]
    assert src["locale"] == "ja" and "セルは?" in src["question"] and src["tool_calls"] == 1
    full = env.lab.source("agent", src["id"])
    assert full["message"] == "セルは?" and full["locale"] == "ja" and full["system_prompt"]
    assert full["original"]["answer"] == "끝" and full["original"]["steps"][0]["tool"] == "query"
    assert full["original"]["warnings_recorded"] is True
    with pytest.raises(LabError):
        env.lab.source("agent", 9999)
    with pytest.raises(LabError):
        env.lab.source("zzz", 1)


def test_sources_sorted_by_time_and_locale_guess():
    env = Env([])
    st = env.store
    st.save_agent({"at": "2026-11-10T10:00:00", "session_id": "a", "response": {}, "sent": {"messages": [{"kind": "current_message", "content": "한국어 질문"}]}})
    st.save_agent({"at": "2026-11-10T11:00:00", "session_id": "b", "response": {}, "sent": {"messages": [{"kind": "current_message", "content": "日本語の質問"}]}})
    rows = env.lab.sources()
    assert [r["session_id"] for r in rows] == ["b", "a"] and [r["locale"] for r in rows] == ["ja", "ko"]


def test_source_for_evidence_row_with_history():
    env = Env([])
    for seq, (r, raw) in enumerate((("첫 원문", "첫 답"), ("둘째 원문", "둘째 답")), start=1):
        env.store.save({"at": f"2026-11-10T10:0{seq}:00", "session_id": "S", "mode": "evidence",
                        "sent": {"messages": [{"kind": "current_message", "content": r}]},
                        "response": {"answer": raw}, "received": r, "check": {"dropped": [], "final": raw}})
    rows = [r for r in env.lab.sources() if r["kind"] == "evidence"]
    assert len(rows) == 2
    last = next(r for r in rows if r["seq"] == 2)
    full = env.lab.source("evidence", last["id"])
    assert full["mode"] == "evidence" and [h["role"] for h in full["history"]] == ["user", "assistant"]
    assert full["original"]["steps"] == []


def test_steps_from_blocks_pairs_calls_with_results():
    items = [{"kind": "agent_step", "tool_calls": [{"name": "query", "arguments": {"a": 1}}]},
             {"kind": "tool_result", "content": json.dumps({"ok": False, "error": {"code": "UNKNOWN_COLUMN", "message": "m"}})},
             {"kind": "agent_step", "content": "안내", "role": "user", "tool_calls": []},
             {"kind": "tool_result", "content": "not json"}]
    s = steps_from_blocks(items)
    assert s[0]["tool"] == "query" and s[0]["ok"] is False and s[0]["error"]["code"] == "UNKNOWN_COLUMN"
    assert s[1]["kind"] == "note"
    assert s[2]["raw"] == "not json" and s[2]["tool"] is None


# ── API ─────────────────────────────────────────────────────────────────────

def make_client(env):
    app = build_app(env.settings, session_service=env.sessions,
                    readiness=ReadyProbe(), turn_log=env.turn_log, lab=env.lab)
    return TestClient(app)


def test_api_routes_need_key_and_map_errors():
    env = Env([final()])
    c = make_client(env)
    for path in ("busy", "sources", "runs"):
        assert c.get(f"/v1/settings/lab/{path}").status_code == 401
        assert c.get(f"/v1/settings/lab/{path}", headers=HEADERS).status_code == 200
    assert c.post("/v1/settings/lab/runs", json=REQ).status_code == 401
    assert c.get("/v1/settings/lab/sources/agent/999", headers=HEADERS).status_code == 404
    assert c.get("/v1/settings/lab/runs/999", headers=HEADERS).status_code == 404
    assert c.post("/v1/settings/lab/runs", headers=HEADERS, json={**REQ, "mode": "zzz"}).status_code == 400
    assert c.post("/v1/settings/lab/runs", headers=HEADERS, content=b"{bad").status_code == 400
    assert c.post("/v1/settings/lab/runs", headers=HEADERS, json=[1]).status_code == 400
    env.chat._active["real"] = (time.monotonic(), "agent")
    r = c.post("/v1/settings/lab/runs", headers=HEADERS, json=REQ)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DL_MODEL_BUSY"


def test_api_disabled_when_no_lab():
    env = Env([])
    app = build_app(env.settings, session_service=env.sessions,
                    readiness=ReadyProbe(), turn_log=env.turn_log)
    c = TestClient(app)
    assert c.get("/v1/settings/lab/busy", headers=HEADERS).status_code == 404


def test_api_message_over_limit_is_400_without_calling_the_model():
    from datalens.api.app import MESSAGE_MAX_CHARS
    env = Env([final()])
    c = make_client(env)
    r = c.post("/v1/settings/lab/runs", headers=HEADERS, json={**REQ, "message": "あ" * (MESSAGE_MAX_CHARS + 1)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "DL_INVALID_LAB_REQUEST" and env.provider.seen == []


def test_api_run_then_list_and_detail_roundtrip():
    env = Env([final("API答")])
    c = make_client(env)
    r = c.post("/v1/settings/lab/runs", headers=HEADERS, json={**REQ, "source": {"kind": "agent", "id": 7}})
    assert r.status_code == 200 and r.json()["result"]["answer"] == "API答"
    rid = r.json()["id"]
    (row,) = c.get("/v1/settings/lab/runs", headers=HEADERS).json()["rows"]
    assert row["id"] == rid and row["source_kind"] == "agent" and row["source_id"] == 7
    assert c.get(f"/v1/settings/lab/runs/{rid}", headers=HEADERS).json()["result"]["answer"] == "API答"
