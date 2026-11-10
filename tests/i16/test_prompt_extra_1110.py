"""프롬프트 실험 ② — 추가 지시 반영 · 이력 · 되돌리기 (`runtime_config.py`, `/v1/settings/prompt-extra`, 2026-11-10 · 맥스 구현 고정).

* RuntimeConfig: 반영·이력 모양·같은 값 무변화·2,000자·공백·형식 거부 · 이력 20건과 id 증가 · 되돌리기 · 저장/다시 읽기 · reset 은 추가 지시를 안 지운다.
* profile/agent: 저장된 값은 그 모드·언어 시스템 지시문 끝에만 붙는다.
* lab: `prompt_extra_saved` · 실험만으로는 저장값이 안 바뀐다.
* API: 200/400/401/404 · applied/current/history(최신순).
가짜 프로바이더 — 모델 없음.
"""
from __future__ import annotations

import json
import time
from collections import deque

import pytest
from starlette.testclient import TestClient

from conftest import ReadyProbe
from datalens.api.app import build_app
from datalens.application.agent import BoundedAgent
from datalens.application.chat import ChatApplicationService
from datalens.application.lab import LabService
from datalens.application.runtime_config import (PROMPT_EXTRA_HISTORY_LIMIT, PROMPT_EXTRA_MAX_CHARS, RuntimeConfig,
                                                 RuntimeConfigError)
from datalens.application.sessions import SessionService
from datalens.application.trace import TurnLog
from datalens.config import Settings
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.infrastructure.sqlite_turns import SqliteTurnStore
from datalens.ports.llm import AssistantTurn, TokenUsage
from datalens.ports.queryforge import QueryForgeToolDefinition
from datalens.prompts import load_system_prompt

HEADERS = {"x-api-key": "data-secret"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def settings():
    return Settings(_env_file=None, api_key="data-secret", queryforge_api_key="q", queryforge_endpoint="http://x/mcp")


def rc(tmp_path, name="r.json"):
    return RuntimeConfig(settings(), str(tmp_path / name))


# ── RuntimeConfig ───────────────────────────────────────────────────────────

def test_initially_empty_and_profile_unchanged(tmp_path):
    c = rc(tmp_path)
    cur = c.prompt_extra()
    assert cur["current"] == {"agent": {"ko": "", "ja": ""}, "evidence": {"ko": "", "ja": ""}}
    assert cur["history"] == [] and cur["max_chars"] == 2000 and cur["history_limit"] == 20
    assert c.profile("agent").prompt_extra == {} and "prompt_extra" not in c.profile("agent").public()
    assert c.describe()["prompt_extra"] == cur["current"]


def test_set_strips_whitespace_and_records_history_entry(tmp_path):
    c = rc(tmp_path)
    e = c.set_prompt_extra("agent", "ja", "  追加指示  ", source_run=7, at="T1")
    assert e == {"id": 1, "at": "T1", "mode": "agent", "locale": "ja", "action": "set", "before": "", "after": "追加指示", "source_run": 7}
    assert c.prompt_extra()["current"]["agent"]["ja"] == "追加指示"
    assert c.profile("agent").prompt_extra == {"ja": "追加指示"}      # 빈 칸(ko)은 실리지 않는다
    assert c.profile("evidence").prompt_extra == {}


def test_same_value_leaves_no_history(tmp_path):
    c = rc(tmp_path)
    c.set_prompt_extra("agent", "ja", "x")
    assert c.set_prompt_extra("agent", "ja", " x ") is None
    assert len(c.prompt_extra()["history"]) == 1


@pytest.mark.parametrize("mode,loc,text", [("zzz", "ja", "x"), ("agent", "en", "x"), ("agent", None, "x"), ("agent", "ja", 5),
                                           ("agent", "ja", None), ("agent", "ja", "x" * (PROMPT_EXTRA_MAX_CHARS + 1))])
def test_bad_input_rejected_and_nothing_changes(tmp_path, mode, loc, text):
    c = rc(tmp_path)
    with pytest.raises(RuntimeConfigError):
        c.set_prompt_extra(mode, loc, text)
    assert c.prompt_extra()["history"] == []


def test_limit_boundary_accepted_and_padded_spaces_do_not_count(tmp_path):
    c = rc(tmp_path)
    assert c.set_prompt_extra("agent", "ko", "x" * PROMPT_EXTRA_MAX_CHARS)
    assert c.set_prompt_extra("agent", "ja", " " * 5 + "y" * PROMPT_EXTRA_MAX_CHARS + " " * 5)


def test_clearing_with_empty_string_is_a_change(tmp_path):
    c = rc(tmp_path)
    c.set_prompt_extra("agent", "ja", "x")
    e = c.set_prompt_extra("agent", "ja", "")
    assert e["before"] == "x" and e["after"] == "" and c.profile("agent").prompt_extra == {}


def test_history_capped_at_20_newest_first_and_ids_keep_growing(tmp_path):
    c = rc(tmp_path)
    for i in range(25):
        c.set_prompt_extra("agent", "ja", f"v{i}")
    h = c.prompt_extra()["history"]
    assert len(h) == PROMPT_EXTRA_HISTORY_LIMIT == 20
    assert [x["id"] for x in h] == list(range(25, 5, -1))          # 최신 먼저 · 잘린 뒤에도 id 는 이어진다
    assert h[0]["after"] == "v24"


def test_revert_restores_and_is_itself_recorded(tmp_path):
    c = rc(tmp_path)
    c.set_prompt_extra("agent", "ja", "A")
    c.set_prompt_extra("agent", "ja", "B")
    e = c.revert_prompt_extra(2, at="T9")                          # 2번(A→B)을 되돌리면 A
    assert c.prompt_extra()["current"]["agent"]["ja"] == "A"
    assert e["action"] == "revert" and e["reverted_from"] == 2 and e["before"] == "B" and e["after"] == "A" and e["id"] == 3
    assert [x["id"] for x in c.prompt_extra()["history"]] == [3, 2, 1]


def test_revert_noop_when_already_that_value_and_unknown_id_raises(tmp_path):
    c = rc(tmp_path)
    c.set_prompt_extra("agent", "ja", "A")
    c.revert_prompt_extra(1)                                       # 비움
    assert c.revert_prompt_extra(1) is None                        # 이미 그 값
    with pytest.raises(RuntimeConfigError, match="unknown history id"):
        c.revert_prompt_extra(99)


def test_revert_of_older_entry_only_touches_its_own_cell(tmp_path):
    c = rc(tmp_path)
    c.set_prompt_extra("agent", "ja", "A")
    c.set_prompt_extra("agent", "ko", "K")
    c.revert_prompt_extra(1)
    cur = c.prompt_extra()["current"]["agent"]
    assert cur == {"ko": "K", "ja": ""}


def test_persist_and_reload_values_and_history(tmp_path):
    c = rc(tmp_path)
    c.set_prompt_extra("evidence", "ko", "한글 지시", source_run=3, at="T")
    c.set_prompt_extra("agent", "ja", "日本語")
    again = rc(tmp_path)
    assert again.prompt_extra()["current"]["evidence"]["ko"] == "한글 지시" and again.prompt_extra()["current"]["agent"]["ja"] == "日本語"
    assert [x["id"] for x in again.prompt_extra()["history"]] == [2, 1]
    again.set_prompt_extra("agent", "ja", "新")
    assert again.prompt_extra()["history"][0]["id"] == 3           # 다시 읽은 뒤에도 id 이어짐
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["prompt_extra"]["agent"]["ja"] == "新"


def test_reset_mode_keeps_prompt_extra_and_other_settings_keep_it_too(tmp_path):
    c = rc(tmp_path)
    c.set_prompt_extra("agent", "ja", "残す")
    c.reset("agent")
    assert c.prompt_extra()["current"]["agent"]["ja"] == "残す"
    c.update_answer_style({"markdown": False})
    assert rc(tmp_path).prompt_extra()["current"]["agent"]["ja"] == "残す"   # 다른 저장이 추가 지시를 지우지 않는다


def test_corrupt_values_load_with_warning_and_do_not_stop_startup(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"prompt_extra": {"agent": {"ja": 123, "ko": "ok"}, "evidence": {"ja": "x" * 5000}},
                             "prompt_extra_history": [{"id": 1}, "bad", {"no_id": 1}, {"id": "x"}]}), encoding="utf-8")
    c = RuntimeConfig(settings(), str(p))
    cur = c.prompt_extra()
    assert cur["current"]["agent"] == {"ko": "ok", "ja": ""} and cur["current"]["evidence"]["ja"] == ""
    assert [x["id"] for x in cur["history"]] == [1]


def test_non_dict_mode_entry_does_not_crash_startup(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"prompt_extra": {"agent": "문자열", "evidence": {"ja": "ok"}}}), encoding="utf-8")
    c = RuntimeConfig(settings(), str(p))                          # 예외 없이 뜬다
    assert c.prompt_extra()["current"]["agent"] == {"ko": "", "ja": ""}


def test_save_failure_raises_config_error(tmp_path):
    c = RuntimeConfig(settings(), str(tmp_path / "no" / "x" / "r.json"))
    (tmp_path / "no").write_text("파일이라 디렉터리를 못 만든다")
    with pytest.raises(RuntimeConfigError):
        c.set_prompt_extra("agent", "ja", "x")


# ── profile · agent · lab ───────────────────────────────────────────────────

class P:
    def __init__(self, turns):
        self.turns, self.seen = deque(turns), []

    async def complete(self, messages, tools, deadline, policy):
        self.seen.append(list(messages))
        return self.turns.popleft()

    async def complete_structured(self, *a):
        return {}, TokenUsage()

    async def close(self):
        return None


class QF:
    async def discover_tools(self, timeout_seconds=None):
        return (QueryForgeToolDefinition("query", "query", {"type": "object", "properties": {}, "required": []}),)


def build(tmp_path, turns):
    runtime = rc(tmp_path)
    provider = P(turns)
    agent = BoundedAgent(provider, QF(), max_tool_calls=4, recovery_budget=2, runtime=runtime) if "runtime" in BoundedAgent.__init__.__code__.co_varnames else None
    return runtime, provider, agent


def final(t="答"):
    return AssistantTurn(t, (), "stop")


@pytest.mark.anyio
async def test_saved_extra_reaches_only_that_mode_and_locale(tmp_path):
    runtime, provider, agent = build(tmp_path, [final(), final(), final()])
    if agent is None:
        pytest.skip("BoundedAgent 가 runtime 을 받지 않는다")
    runtime.set_prompt_extra("agent", "ja", "保存された追加")
    sessions = SessionService(InMemorySessionStore(60))
    plain_ja = load_system_prompt("ja", "system")
    for locale, expect_extra in (("ja", True), ("ko", False)):
        s = sessions.create(locale=locale)
        await agent.run(s, "q", time.monotonic() + 5, mode="agent")
    sys_ja = next(m.content for m in provider.seen[0] if m.role == "system")
    sys_ko = next(m.content for m in provider.seen[1] if m.role == "system")
    assert sys_ja.endswith("保存された追加") and sys_ja.startswith(plain_ja[:40])
    assert "保存された追加" not in sys_ko
    s = sessions.create(locale="ja")
    await agent.run(s, "q", time.monotonic() + 5, mode="evidence")
    assert "保存された追加" not in next(m.content for m in provider.seen[2] if m.role == "system")


def lab_env(tmp_path, turns):
    runtime, provider, agent = build(tmp_path, turns)
    store = SqliteTurnStore(":memory:")
    turn_log = TurnLog(5, store)
    sessions = SessionService(InMemorySessionStore(60))
    chat = ChatApplicationService(sessions, agent, None, runtime, turn_log)
    lab = LabService(agent, sessions, runtime, turn_log, chat, settings())
    return runtime, provider, store, lab


@pytest.mark.anyio
async def test_lab_source_shows_saved_extra_and_experiment_does_not_change_it(tmp_path):
    runtime, provider, store, lab = lab_env(tmp_path, [final("実験答"), final("実験答2")])
    runtime.set_prompt_extra("agent", "ja", "保存値")
    store.save_agent({"at": "2026-11-10T10:00:00", "session_id": "s", "locale": "ja", "response": {},
                      "sent": {"messages": [{"kind": "current_message", "content": "質問"}]}})
    (row,) = store.recent_agent()
    assert lab.source("agent", row["id"])["prompt_extra_saved"] == "保存値"
    assert lab.source("agent", row["id"])["locale"] == "ja"
    # 실험 칸을 통째로 바꿔 보낸다 — 비우면 추가 지시 없이
    await lab.run({"mode": "agent", "locale": "ja", "message": "質問", "prompt_extra": "実験だけ"}, time.monotonic() + 5)
    assert next(m.content for m in provider.seen[0] if m.role == "system").endswith("実験だけ")
    await lab.run({"mode": "agent", "locale": "ja", "message": "質問", "prompt_extra": ""}, time.monotonic() + 5)
    assert "保存値" not in next(m.content for m in provider.seen[1] if m.role == "system")
    assert runtime.prompt_extra()["current"]["agent"]["ja"] == "保存値" and len(runtime.prompt_extra()["history"]) == 1


# ── API ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path):
    s = settings()
    app = build_app(s, readiness=ReadyProbe(), runtime_config=RuntimeConfig(s, str(tmp_path / "r.json")))
    with TestClient(app) as c:
        yield c


def put(c, **body):
    return c.put("/v1/settings/prompt-extra", headers=HEADERS, json=body)


def test_api_get_put_revert_roundtrip(client):
    assert client.get("/v1/settings/prompt-extra").status_code == 401
    assert client.put("/v1/settings/prompt-extra", json={}).status_code == 401
    assert client.post("/v1/settings/prompt-extra/revert", json={"history_id": 1}).status_code == 401
    g = client.get("/v1/settings/prompt-extra", headers=HEADERS)
    assert g.status_code == 200 and g.json()["history"] == [] and "applied" not in g.json()
    r = put(client, mode="agent", locale="ja", text="  API指示 ", source_run=4)
    j = r.json()
    assert r.status_code == 200 and j["applied"]["after"] == "API指示" and j["applied"]["source_run"] == 4
    assert j["current"]["agent"]["ja"] == "API指示" and j["history"][0]["id"] == 1
    same = put(client, mode="agent", locale="ja", text="API指示").json()
    assert same["applied"] is None and len(same["history"]) == 1
    put(client, mode="agent", locale="ja", text="二番目")
    rv = client.post("/v1/settings/prompt-extra/revert", headers=HEADERS, json={"history_id": 2}).json()
    assert rv["applied"]["action"] == "revert" and rv["current"]["agent"]["ja"] == "API指示"
    assert [h["id"] for h in rv["history"]] == [3, 2, 1]
    assert client.get("/v1/settings", headers=HEADERS).json()["prompt_extra"]["agent"]["ja"] == "API指示"


@pytest.mark.parametrize("body", [{"mode": "zzz", "locale": "ja", "text": "x"}, {"mode": "agent", "locale": "en", "text": "x"},
                                  {"mode": "agent", "locale": "ja", "text": 5}, {"mode": "agent", "locale": "ja"},
                                  {"mode": "agent", "locale": "ja", "text": "x" * 2001},
                                  {"mode": "agent", "locale": "ja", "text": "x", "source_run": "7"},
                                  {"mode": "agent", "locale": "ja", "text": "x", "source_run": True}])
def test_api_put_bad_input_400(client, body):
    assert put(client, **body).status_code == 400
    assert client.get("/v1/settings/prompt-extra", headers=HEADERS).json()["history"] == []


def test_api_put_broken_json_or_array_400(client):
    assert client.put("/v1/settings/prompt-extra", headers=HEADERS, content=b"{bad").status_code == 400
    assert client.put("/v1/settings/prompt-extra", headers=HEADERS, json=[1]).status_code == 400


def test_api_revert_codes(client):
    put(client, mode="agent", locale="ja", text="x")
    r404 = client.post("/v1/settings/prompt-extra/revert", headers=HEADERS, json={"history_id": 99})
    assert r404.status_code == 404 and r404.json()["error"]["code"] == "DL_PROMPT_EXTRA_HISTORY_NOT_FOUND"
    for bad in ({"history_id": "1"}, {"history_id": True}, {}, {"history_id": None}):
        assert client.post("/v1/settings/prompt-extra/revert", headers=HEADERS, json=bad).status_code == 400
    assert client.post("/v1/settings/prompt-extra/revert", headers=HEADERS, content=b"{bad").status_code == 400
    assert client.post("/v1/settings/prompt-extra/revert", headers=HEADERS, json=[1]).status_code == 400


def test_api_without_runtime_config_is_unavailable():
    app = build_app(settings(), readiness=ReadyProbe())
    with TestClient(app) as c:
        assert c.get("/v1/settings/prompt-extra", headers=HEADERS).status_code in (404, 503)
