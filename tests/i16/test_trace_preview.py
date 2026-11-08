"""실제로 나간 것 · 미리보기 · 효과 비교 기록 (`application/trace.py`, `/v1/settings/*`).

모델도 QueryForge 도 부르지 않는다 — 조립 결과를 흉내 내는 previewer 를 끼운다. 검증 대상은
"블록이 맞게 나뉘는가 · 저장하지 않은 값이 저장된 설정을 건드리지 않는가 · 원문이 메모리에만
남는가"다(원문에 호출부가 보낸 증거가 그대로 들어 있다).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from conftest import ReadyProbe
from datalens.api.app import build_app
from datalens.application.runtime_config import RuntimeConfig
from datalens.application.trace import HISTORY_LIMIT, TurnLog, blocks
from datalens.config import Settings
from datalens.ports.llm import ProviderMessage
from datalens.ports.queryforge import QueryForgeToolDefinition

HEADERS = {"x-api-key": "data-secret"}


def make_settings() -> Settings:
    return Settings(
        api_key="data-secret",
        queryforge_api_key="query-secret",
        queryforge_endpoint="http://queryforge.test:8080/mcp",
        queryforge_timeout_seconds=0.2,
    )


def msg(role: str, content: str) -> ProviderMessage:
    return ProviderMessage(role=role, content=content)


def tool(name: str = "query") -> QueryForgeToolDefinition:
    return QueryForgeToolDefinition(name, "조회 도구 " * 20, {"type": "object", "properties": {"sql": {"type": "string"}}})


# ── trace.blocks ────────────────────────────────────────────────────────────────

def conversation() -> list[ProviderMessage]:
    return [
        msg("system", "DataLens session context: now=2026-11-04"),
        msg("system", "DataLens memory hints: ..."),
        msg("system", "자료만으로 답하라"),
        msg("user", "첫 질문"),
        msg("assistant", "첫 답"),
        msg("user", "이번 질문"),
    ]


def test_blocks_classify_system_history_and_current_message() -> None:
    msgs = conversation()
    out = blocks(msgs, [], base_len=len(msgs), prompt_tokens=None)
    assert [m["kind"] for m in out["messages"]] == [
        "session_context", "memory", "system_prompt", "history", "history", "current_message",
    ]
    assert out["summary"]["history"]["count"] == 2
    assert "tools" not in out["summary"]
    assert out["total_chars"] == sum(m["chars"] for m in out["messages"])


def test_blocks_mark_messages_after_the_base_as_agent_steps() -> None:
    msgs = conversation() + [msg("assistant", "도구를 부른다"), msg("tool", "결과")]
    out = blocks(msgs, [], base_len=6, prompt_tokens=None)
    assert [m["kind"] for m in out["messages"][-2:]] == ["agent_step", "tool_result"]


def test_blocks_count_tool_definitions_in_the_total() -> None:
    msgs = conversation()
    without = blocks(msgs, [], len(msgs), None)
    with_tools = blocks(msgs, [tool("a"), tool("b")], len(msgs), None)
    assert with_tools["summary"]["tools"]["count"] == 2
    assert with_tools["total_chars"] == without["total_chars"] + with_tools["summary"]["tools"]["chars"]
    assert [t["name"] for t in with_tools["tools"]] == ["a", "b"]


def test_blocks_tokens_follow_the_measured_prompt_token_count() -> None:
    msgs = conversation()
    out = blocks(msgs, [tool()], len(msgs), prompt_tokens=1000)
    # 합계가 Ollama 가 알려 준 실측 토큰에 맞는다 — 블록은 글자 비율로 나눈 추정이다
    assert out["ratio_measured"] is True
    assert out["chars_per_token"] == pytest.approx(out["total_chars"] / 1000, abs=0.001)
    assert out["tokens_est_total"] == pytest.approx(1000, abs=1)


def test_blocks_fall_back_to_the_default_ratio_and_say_it_is_an_estimate() -> None:
    msgs = conversation()
    out = blocks(msgs, [], len(msgs), prompt_tokens=None)
    assert out["ratio_measured"] is False
    assert out["chars_per_token"] == 2.15
    custom = blocks(msgs, [], len(msgs), None, chars_per_token=3.0)
    assert custom["chars_per_token"] == 3.0 and custom["ratio_measured"] is False


# ── TurnLog ─────────────────────────────────────────────────────────────────────

def trace(at: str, ratio: float | None = None, measured: bool = True) -> dict[str, Any]:
    return {"at": at, "sent": {"chars_per_token": ratio, "ratio_measured": measured}}


def test_turn_log_keeps_the_latest_n_per_mode() -> None:
    log = TurnLog(keep=2)
    for i in range(3):
        log.add("agent", trace(f"2026-11-04T10:0{i}"), {"n": i})
    log.add("evidence", trace("2026-11-04T10:09"), {"n": 9})
    assert [t["at"] for t in log.traces("agent")] == ["2026-11-04T10:02", "2026-11-04T10:01"]
    assert [t["at"] for t in log.traces("evidence")] == ["2026-11-04T10:09"]
    # 모드를 안 주면 전부, 최신이 먼저
    assert [t["at"] for t in log.traces()][0] == "2026-11-04T10:09"


def test_turn_log_history_is_capped_and_drops_the_oldest() -> None:
    log = TurnLog(keep=5)
    for i in range(HISTORY_LIMIT + 10):
        log.add("evidence", None, {"n": i})
    rows = log.history()
    assert HISTORY_LIMIT == 50 and len(rows) == 50
    assert rows[0]["n"] == 10 and rows[-1]["n"] == 59


def test_turn_log_keep_zero_stores_no_originals_but_still_records_history() -> None:
    log = TurnLog(keep=0)
    log.add("agent", trace("2026-11-04T10:00"), {"n": 1})
    assert log.traces() == [] and len(log.history()) == 1


def test_turn_log_returns_copies_so_callers_cannot_alter_the_record() -> None:
    log = TurnLog(keep=3)
    log.add("agent", trace("2026-11-04T10:00"), {"n": 1})
    log.traces("agent")[0]["at"] = "변조"
    log.history()[0]["n"] = 99
    assert log.traces("agent")[0]["at"] == "2026-11-04T10:00" and log.history()[0]["n"] == 1


def test_turn_log_writes_nothing_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 원문에 호출부가 보낸 증거가 그대로 있다 — 메모리에만 둔다
    monkeypatch.chdir(tmp_path)
    log = TurnLog(keep=5)
    for i in range(5):
        log.add("evidence", trace(f"2026-11-04T10:0{i}"), {"n": i})
    assert list(tmp_path.iterdir()) == []


def test_chars_per_token_ignores_unmeasured_and_out_of_range_ratios() -> None:
    log = TurnLog(keep=5)
    assert log.chars_per_token("evidence") is None
    log.add("evidence", trace("2026-11-04T10:00", 2.3), {})
    log.add("evidence", trace("2026-11-04T10:01", 20.0), {})            # 비정상 — 범위 0.8~8 밖
    log.add("evidence", trace("2026-11-04T10:02", 0.1), {})
    log.add("evidence", trace("2026-11-04T10:03", 3.0, measured=False), {})  # 실측이 아님
    assert log.chars_per_token("evidence") == 2.3                        # 쓸 수 있는 가장 최근 값
    assert log.chars_per_token("agent") is None


# ── /v1/settings/preview ────────────────────────────────────────────────────────

class StubPreviewer:
    """조립만 흉내 낸다 — 받은 프로필을 남겨 덮어쓰기가 닿았는지 본다."""

    def __init__(self) -> None:
        self.profile: Any = None

    async def preview(self, session: Any, message: str, profile: Any, timeout: float):
        self.profile = profile
        msgs = []
        if profile.system_prompt:
            msgs.append(msg("system", "자료만으로 답하라"))
        msgs.append(msg("user", message))
        tools = [tool()] if profile.tools != "off" else []
        return msgs, tools, len(msgs), ""


@pytest.fixture
def env(tmp_path: Path):
    settings = make_settings()
    saved = tmp_path / "runtime.json"
    config = RuntimeConfig(settings, str(saved))
    previewer = StubPreviewer()
    app = build_app(settings, readiness=ReadyProbe(), runtime_config=config, turn_log=TurnLog(), previewer=previewer)
    with TestClient(app) as client:
        yield client, previewer, saved


def preview(client: TestClient, **body: Any):
    return client.post("/v1/settings/preview", headers=HEADERS, json={"mode": "evidence", "message": "질문", **body})


def test_preview_applies_unsaved_overrides_without_touching_the_saved_config(env) -> None:
    client, previewer, saved = env
    before = client.get("/v1/settings", headers=HEADERS).json()
    r = preview(client, params={"num_predict": 3000, "think": True}, context={"history_items": 2, "tools": "summary"})
    assert r.status_code == 200
    body = r.json()
    assert body["profile"]["params"]["num_predict"] == 3000 and body["profile"]["params"]["think"] is True
    assert body["profile"]["context"]["history_items"] == 2 and body["profile"]["context"]["tools"] == "summary"
    assert previewer.profile.tools == "summary"                    # 조립 쪽에도 덮어쓴 값이 갔다
    assert body["num_predict"] == 3000 and body["summary"]["tools"]["count"] == 1
    # 저장된 설정은 그대로다 — 파일도 만들어지지 않았다
    assert client.get("/v1/settings", headers=HEADERS).json() == before
    assert not saved.exists()
    again = preview(client).json()
    assert again["profile"]["context"]["tools"] == "off" and again["num_predict"] != 3000


@pytest.mark.parametrize(
    "body",
    [
        {"params": {"temperature": 5}},          # 범위 0~2
        {"params": {"num_ctx": 100}},            # 최소 1024
        {"params": {"top_k": 1.5}},              # 정수여야 한다
        {"params": {"think": "yes"}},            # 불리언이어야 한다
        {"params": {"unknown": 1}},              # 모르는 키
        {"context": {"tools": "everything"}},    # off | summary | full
        {"context": {"history_items": 999}},     # 상한 밖
        {"context": {"history_items": True}},    # 불리언은 정수가 아니다
    ],
)
def test_preview_rejects_out_of_range_values_with_400(env, body) -> None:
    client, previewer, saved = env
    r = preview(client, **body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "DL_INVALID_SETTINGS"
    assert previewer.profile is None and not saved.exists()       # 조립도 저장도 하지 않았다


def test_preview_reports_context_pressure(env) -> None:
    client, _, _ = env
    ok = preview(client, params={"num_ctx": 131072}).json()
    assert ok["status_est"] == "ok" and ok["usage_pct"] < 5
    # 입력(추정)이 num_ctx 를 넘으면 잘린다고 알린다
    big = preview(client, message="가" * 4000, params={"num_ctx": 1024}).json()
    assert big["status_est"] == "truncated" and big["usage_pct"] > 100


def test_preview_requires_the_api_key_and_a_known_mode(env) -> None:
    client, _, _ = env
    assert client.post("/v1/settings/preview", json={"mode": "evidence", "message": "q"}).status_code == 401
    r = client.post("/v1/settings/preview", headers=HEADERS, json={"mode": "nope", "message": "q"})
    assert r.status_code in (400, 422)
    assert client.get("/v1/settings/traces?mode=nope", headers=HEADERS).status_code == 400


def test_traces_and_history_endpoints_need_the_key(env) -> None:
    client, _, _ = env
    for path in ("/v1/settings/traces", "/v1/settings/history", "/v1/settings"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=HEADERS).status_code == 200
    assert json.loads(client.get("/v1/settings/traces", headers=HEADERS).text)["traces"] == []
