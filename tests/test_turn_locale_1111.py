"""턴 언어 — 답은 세션이 아니라 질문한 언어를 따른다 (2026-11-11 사용자 결정 · 맥스 수정 · 에스터 시험).

* `domain.session.question_locale` — graphio `rca.llm.question_lang` 과 **같은 규칙**(같은 표로 시험한다).
* `MessageRequest.locale` — 선택 칸. 없어도 받고, ko·ja 만, `en` 은 거부.
* `api` — 턴 언어 = 요청 값 > 질문 글자 > 세션 기본값. 세션과 같으면 `locale` 을 핸들러에 넘기지 않는다(예전 호출 모양).
* `chat._turn_session`/`_keep_locale`/`ChatApplicationService` — ja 세션에 ko 턴이면 그 턴만 ko, 커밋된 세션은 ja.
"""
from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from datalens.api.app import MessageRequest, build_app
from datalens.application import chat as chat_mod
from datalens.application.chat import ChatApplicationService, _keep_locale, _turn_session
from datalens.application.sessions import SessionService
from datalens.config import Settings
from datalens.domain.session import Session, question_locale
from datalens.infrastructure.session_store import InMemorySessionStore

# graphio tests/test_answer_lang_1111.py 의 표와 같다 — 두 구현이 갈라지면 한쪽이 깨진다
CASES = [
    ("SGW-6 말고 다른 장비도 영향이 있었나요?", "ko"),
    ("SGW-6以外の装置も影響がありましたか？", "ja"),
    ("新宿区 영향은?", "ko"),
    ("新宿区の影響は?", "ja"),
    ("ㅋㅋ", "ko"),
    ("ラーメン", "ja"),
    ("ーー", "ja"),
    ("가ー", None),
    ("新宿区", None),
    ("東京都 千代田区", None),
    ("SGW-6 10:20 count", None),
    ("12345", None),
    ("", None),
    ("   ", None),
    (None, None),
    ("가あ", None),
    ("가나あい", None),
    ("가나다あい", "ko"),
    ("가あいう", "ja"),
]


@pytest.mark.parametrize("text,want", CASES)
def test_question_locale(text, want):
    assert question_locale(text) == want


def test_han_is_not_counted():
    assert question_locale("新宿区千代田区港区豊島区文京区 영향") == "ko"
    assert question_locale("新宿区千代田区港区豊島区文京区 の影響") == "ja"


def test_range_edges():
    assert question_locale("ㄱ") == "ko" and question_locale("힣") == "ko"
    assert question_locale("ぁ") == "ja" and question_locale("ヺ") == "ja"
    assert question_locale("・") is None


# ── MessageRequest ──────────────────────────────────────────────────────────

def test_message_request_locale_is_optional():
    assert MessageRequest.model_validate({"message": "hi"}).locale is None


@pytest.mark.parametrize("loc", ["ko", "ja"])
def test_message_request_accepts_ko_ja(loc):
    assert MessageRequest.model_validate({"message": "hi", "locale": loc}).locale == loc


@pytest.mark.parametrize("loc", ["en", "KO", "", "jp"])
def test_message_request_rejects_other_locales(loc):
    with pytest.raises(ValidationError):
        MessageRequest.model_validate({"message": "hi", "locale": loc})


def test_message_request_still_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        MessageRequest.model_validate({"message": "hi", "lang": "ko"})


# ── _turn_session · _keep_locale ────────────────────────────────────────────

def _session(locale="ja"):
    return InMemorySessionStore(60).create(locale=locale)


def test_turn_session_changes_only_the_copy():
    s = _session("ja")
    base, turn = _turn_session(s, "ko")
    assert base == "ja" and turn.locale == "ko" and s.locale == "ja"
    assert turn.session_id == s.session_id


@pytest.mark.parametrize("loc", [None, "ja", "en", ""])
def test_turn_session_same_or_unsupported_locale_makes_no_copy(loc):
    s = _session("ja")
    base, turn = _turn_session(s, loc)
    assert base is None and turn is s


def test_keep_locale_restores_the_base():
    s = _session("ja")
    _, turn = _turn_session(s, "ko")
    kept = _keep_locale(turn, "ja")
    assert kept.locale == "ja" and kept.session_id == s.session_id


def test_keep_locale_without_base_returns_the_same_object():
    s = _session("ko")
    assert _keep_locale(s, None) is s
    assert _keep_locale(s, "ko") is s


# ── ChatApplicationService — 가짜 에이전트 ───────────────────────────────────

class _Agent:
    """받은 세션의 locale 을 적어 두고, 그 locale 이 박힌 세션을 `updated_session` 으로 돌려준다."""

    def __init__(self):
        self.seen = []

    def _result(self, session):
        self.seen.append(session.locale)
        return SimpleNamespace(answer="ok", datasets=(), warnings=(), tool_calls=0, recovery_count=0,
                               updated_session=session, plan=(), grounding=None, context=None, profile=None, trace=None)

    async def run(self, session, message, deadline, event_sink=None, hints=None, mode="agent"):
        return self._result(session)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _service(monkeypatch):
    store = InMemorySessionStore(60)
    sessions = SessionService(store)
    svc = ChatApplicationService(sessions, _Agent())
    monkeypatch.setattr(svc, "_record", lambda *a, **k: None)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(svc, "_remember", noop)

    async def no_recall(*a, **k):
        return None

    monkeypatch.setattr(svc, "_recall", no_recall)
    monkeypatch.setattr(chat_mod, "render_hints", lambda r: None)
    return svc, sessions, store


@pytest.mark.anyio
async def test_handle_ko_turn_in_a_ja_session_runs_in_ko_and_commits_ja(monkeypatch):
    svc, sessions, _ = _service(monkeypatch)
    s = sessions.create(locale="ja")
    await svc.handle(s, "질문", "r1", time.monotonic() + 5, locale="ko")
    assert svc._agent.seen == ["ko"]
    assert sessions.require(s.session_id).locale == "ja"


@pytest.mark.anyio
async def test_handle_same_locale_turn_passes_the_session_untouched(monkeypatch):
    svc, sessions, _ = _service(monkeypatch)
    s = sessions.create(locale="ja")
    await svc.handle(s, "質問", "r1", time.monotonic() + 5, locale="ja")
    await svc.handle(s, "質問", "r2", time.monotonic() + 5)
    assert svc._agent.seen == ["ja", "ja"]
    assert sessions.require(s.session_id).locale == "ja"


@pytest.mark.anyio
async def test_next_turn_without_locale_is_not_dragged_by_the_previous_turn(monkeypatch):
    svc, sessions, _ = _service(monkeypatch)
    s = sessions.create(locale="ja")
    await svc.handle(s, "질문", "r1", time.monotonic() + 5, locale="ko")
    s2 = sessions.require(s.session_id)
    await svc.handle(s2, "123", "r2", time.monotonic() + 5)
    assert svc._agent.seen == ["ko", "ja"]


@pytest.mark.anyio
async def test_handle_stream_follows_the_same_rule(monkeypatch):
    svc, sessions, _ = _service(monkeypatch)
    s = sessions.create(locale="ja")

    async def sink(event, data):
        return None

    await svc.handle_stream(s, "질문", "r1", time.monotonic() + 5, sink, locale="ko")
    assert svc._agent.seen == ["ko"]
    assert sessions.require(s.session_id).locale == "ja"


# ── api — 핸들러에 넘기는 locale ────────────────────────────────────────────

class _Handler:
    def __init__(self):
        self.calls = []

    async def handle(self, session, message, request_id, deadline, **kw):
        self.calls.append(kw)
        return {"answer": "ok"}

    async def handle_stream(self, session, message, request_id, deadline, event_sink, **kw):
        self.calls.append(kw)
        return {"answer": "ok"}


@pytest.fixture
def api():
    handler = _Handler()
    app = build_app(Settings(api_key="k", queryforge_api_key="q"), message_handler=handler)
    with TestClient(app) as client:
        yield client, handler


def _new_session(client, locale):
    r = client.post("/v1/sessions", headers={"x-api-key": "k"}, json={"locale": locale})
    assert r.status_code == 201
    return r.json()["session_id"]


def _send(client, sid, **body):
    return client.post(f"/v1/sessions/{sid}/messages", headers={"x-api-key": "k"}, json=body)


def test_korean_question_in_a_ja_session_passes_ko(api):
    client, h = api
    sid = _new_session(client, "ja")
    assert _send(client, sid, message="새로운 사건의 원인이 뭐야?").status_code == 200
    assert h.calls == [{"locale": "ko"}]


def test_japanese_question_in_a_ko_session_passes_ja(api):
    client, h = api
    sid = _new_session(client, "ko")
    _send(client, sid, message="原因は何ですか？")
    assert h.calls == [{"locale": "ja"}]


def test_same_language_as_the_session_passes_nothing(api):
    client, h = api
    sid = _new_session(client, "ja")
    _send(client, sid, message="原因は何ですか？")
    assert h.calls == [{}]


def test_undecidable_question_passes_nothing(api):
    client, h = api
    sid = _new_session(client, "ja")
    _send(client, sid, message="SGW-6 10:20 count")
    _send(client, sid, message="新宿区")
    assert h.calls == [{}, {}]


def test_request_locale_beats_the_question_letters(api):
    """graphio 처럼 한국어 맥락을 붙여 보내는 호출자는 질문만 센 값을 `locale` 로 준다."""
    client, h = api
    sid = _new_session(client, "ko")
    _send(client, sid, message="[지금 보고 있는 화면] 한국어 맥락 한국어 맥락\n질문: SGW-6以外は?", locale="ja")
    assert h.calls == [{"locale": "ja"}]


def test_request_locale_equal_to_session_passes_nothing(api):
    client, h = api
    sid = _new_session(client, "ko")
    _send(client, sid, message="質問です", locale="ko")
    assert h.calls == [{}]


def test_mode_and_locale_are_both_passed(api):
    client, h = api
    sid = _new_session(client, "ja")
    _send(client, sid, message="질문", mode="evidence")
    assert h.calls == [{"mode": "evidence", "locale": "ko"}]


def test_unsupported_request_locale_is_rejected_with_400(api):
    client, h = api
    sid = _new_session(client, "ko")
    r = _send(client, sid, message="hi", locale="en")
    assert r.status_code in (400, 422) and h.calls == []


def test_request_without_locale_field_is_still_accepted(api):
    client, h = api
    sid = _new_session(client, "ko")
    assert _send(client, sid, message="hi").status_code == 200
    assert h.calls == [{}]


def test_session_default_is_untouched_by_a_different_language_turn(api):
    client, h = api
    sid = _new_session(client, "ja")
    _send(client, sid, message="질문입니다")
    _send(client, sid, message="123")
    assert h.calls == [{"locale": "ko"}, {}]
