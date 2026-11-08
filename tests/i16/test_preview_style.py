"""`POST /v1/settings/preview` — 바탕(last·example·custom) · 저장 전 스타일 · 실제 전송과 같은 조립.

모델도 QueryForge 도 부르지 않는다. 조립기는 메시지를 그대로 user 로 싣는 스텁이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from conftest import ReadyProbe
from datalens.api.app import PREVIEW_EXAMPLES, build_app
from datalens.application import answer_style as st
from datalens.application.runtime_config import RuntimeConfig
from datalens.application.trace import TurnLog
from datalens.config import Settings
from datalens.infrastructure.sqlite_turns import SqliteTurnStore
from datalens.ports.llm import ProviderMessage

HEADERS = {"x-api-key": "data-secret"}
ZONE = f"{st.OPEN}\n8. 원래 규칙\n{st.CLOSE}"
FIRST = f"규칙\n{ZONE}\n질문: 첫 질문"
FOLLOW = "어떻게 해결할 수 있어?"


class Echo:
    async def preview(self, session: Any, message: str, profile: Any, timeout: float):
        return [ProviderMessage(role="user", content=message)], [], 1, ""


def settings() -> Settings:
    return Settings(api_key="data-secret", queryforge_api_key="q", queryforge_endpoint="http://q.test:8080/mcp",
                    queryforge_timeout_seconds=0.2)


def make(tmp_path: Path, traces: list[dict] | None = None):
    log = TurnLog()
    for t in traces or []:
        log.add("evidence", t, {})
    s = settings()
    cfg = RuntimeConfig(s, str(tmp_path / "runtime.json"))
    app = build_app(s, readiness=ReadyProbe(), runtime_config=cfg, turn_log=log, previewer=Echo())
    return TestClient(app), cfg


def trace(at: str, text: str, session: str = "dls_a") -> dict:
    return {"at": at, "session_id": session, "received": text, "sent": {"messages": []},
            "response": {"answer": ""}, "style": {"applied": False}}


def post(client, **body):
    return client.post("/v1/settings/preview", headers=HEADERS, json={"mode": "evidence", **body})


def current(r) -> str:
    return next(m for m in r.json()["messages"] if m["kind"] == "current_message")["content"]


@pytest.fixture()
def env(tmp_path):
    with make(tmp_path, [trace("2026-11-04T06:00:00", FIRST), trace("2026-11-04T06:05:00", FOLLOW)])[0] as c:
        yield c


def test_requires_api_key(tmp_path):
    client, _ = make(tmp_path)
    with client:
        assert client.post("/v1/settings/preview", json={"mode": "evidence", "message": "q"}).status_code == 401


def test_custom_uses_the_given_message(env):
    r = post(env, baseline="custom", message="내 질문")
    assert r.status_code == 200 and r.json()["basis"] == {"kind": "custom"}
    assert "내 질문" in current(r)


def test_custom_without_message_is_rejected(env):
    assert post(env, baseline="custom").status_code in (400, 422)


def test_legacy_message_only_call_is_custom(env):
    r = post(env, message="옛 호출")
    assert r.status_code == 200 and r.json()["basis"]["kind"] == "custom" and "옛 호출" in current(r)


def test_example_uses_the_builtin_text(env):
    r = post(env, baseline="example")
    assert r.json()["basis"]["kind"] == "example"
    assert "지금 결론이 무엇인가요?" in current(r)
    assert PREVIEW_EXAMPLES["evidence"].split("\n")[0] in current(r)


def test_last_prefers_the_first_question_with_the_marker(env):
    # 가장 최근은 이어 묻기(06:05)지만 표지가 있는 첫 질문(06:00)을 바탕으로 잡는다
    r = post(env, baseline="last")
    b = r.json()["basis"]
    assert b["kind"] == "last" and b["at"] == "2026-11-04T06:00:00" and b["session_id"] == "dls_a"
    assert "질문: 첫 질문" in current(r)


def test_last_without_marker_uses_the_most_recent(tmp_path):
    client, _ = make(tmp_path, [trace("2026-11-04T06:00:00", "첫"), trace("2026-11-04T06:05:00", FOLLOW)])
    with client:
        r = post(client, baseline="last")
    assert r.json()["basis"]["at"] == "2026-11-04T06:05:00" and FOLLOW in current(r)


def test_no_history_falls_back_to_example_and_says_so(tmp_path):
    client, _ = make(tmp_path)
    with client:
        for body in ({"baseline": "last"}, {}):
            r = post(client, **body)
            b = r.json()["basis"]
            assert r.status_code == 200 and b["kind"] == "example" and "기록이 없다" in b["fallback"]


def test_last_falls_back_to_the_sqlite_store(tmp_path):
    store = SqliteTurnStore(":memory:")
    store.save(trace("2026-11-04T05:00:00", FIRST, "dls_s"))
    s = settings()
    app = build_app(s, readiness=ReadyProbe(), runtime_config=RuntimeConfig(s, str(tmp_path / "r.json")),
                    turn_log=TurnLog(store=store), previewer=Echo())
    with TestClient(app) as client:
        b = post(client, baseline="last").json()["basis"]
    assert b["kind"] == "last" and b["source"] == "sqlite" and b["session_id"] == "dls_s"


def test_unsaved_style_does_not_change_the_saved_one(env):
    before = env.get("/v1/settings/answer-style", headers=HEADERS).json()
    r = post(env, baseline="last", answer_style={"markdown": False, "max_chars": 800})
    assert r.json()["style"]["applied"] is True and "800자" in current(r)
    assert env.get("/v1/settings/answer-style", headers=HEADERS).json() == before
    assert "enabled" not in before["answer_style"] and before["answer_style"]["markdown"] is True
    nxt = post(env, baseline="last").json()["style"]       # 다음 미리보기는 저장값(기본) — 마크다운 그대로
    assert nxt["applied"] is True and nxt["settings"]["markdown"] is True and "800자" not in nxt["block"]


def test_style_result_equals_answer_style_apply(env):
    style = {"min_sentences": 3, "max_sentences": 7, "extra": "존댓말"}
    r = post(env, baseline="last", answer_style=style)
    want_msg, want_rec = st.apply(FIRST, st.check(style))
    assert current(r) == want_msg
    assert r.json()["style"] == want_rec
    assert r.json()["message_chars"] == len(want_msg)


def test_missing_style_fields_use_saved_values(tmp_path):
    client, cfg = make(tmp_path, [trace("2026-11-04T06:00:00", FIRST)])
    with client:
        cfg.update_answer_style({"max_sentences": 9})
        r = post(client, baseline="last", answer_style={"markdown": False})
    assert r.json()["style"]["settings"]["max_sentences"] == 9 and r.json()["style"]["applied"] is True


def test_invalid_style_is_400(env):
    assert post(env, baseline="last", answer_style={"min_sentences": 0}).status_code == 400


def test_reports_message_length_and_limit(env):
    b = post(env, baseline="last").json()
    # 표지가 있으면 기본 구역으로 바꿔 넣으므로 보낸 길이는 바꾼 뒤 메시지 길이다
    assert b["message_chars"] == len(st.apply(FIRST, st.DEFAULTS)[0]) and b["message_max_chars"] == 16384


def test_general_mode_has_no_style(env):
    r = env.post("/v1/settings/preview", headers=HEADERS, json={"mode": "agent", "baseline": "example"})
    assert r.status_code == 200 and r.json()["style"] is None
    assert r.json()["basis"]["kind"] == "example"


def test_no_marker_message_is_not_changed_and_has_no_off_reason(tmp_path):
    client, _ = make(tmp_path, [trace("2026-11-04T06:05:00", FOLLOW)])
    with client:
        r = post(client, baseline="last")
    assert current(r) == FOLLOW and r.json()["style"]["applied"] is False
    assert r.json()["style"]["reason"] == "표지 없음"


def test_put_enabled_is_ignored_and_not_echoed(env):
    r = env.put("/v1/settings/answer-style", headers=HEADERS, json={"enabled": True, "max_sentences": 9})
    assert r.status_code == 200 and "enabled" not in r.json()["answer_style"]
    assert r.json()["answer_style"]["max_sentences"] == 9
    got = env.get("/v1/settings/answer-style", headers=HEADERS).json()
    assert "enabled" not in got["answer_style"] and "enabled" not in got["defaults"]


def test_legacy_enabled_key_in_runtime_json_is_ignored(tmp_path):
    import json
    saved = tmp_path / "runtime.json"
    saved.write_text(json.dumps({"answer_style": {"enabled": True, "max_sentences": 9, "markdown": False}}))
    s = settings()
    cfg = RuntimeConfig(s, str(saved))
    app = build_app(s, readiness=ReadyProbe(), runtime_config=cfg, turn_log=TurnLog(), previewer=Echo())
    with TestClient(app) as client:
        got = client.get("/v1/settings/answer-style", headers=HEADERS).json()["answer_style"]
    assert "enabled" not in got and got["max_sentences"] == 9 and got["markdown"] is False
