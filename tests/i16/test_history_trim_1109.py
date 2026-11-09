"""조회 모드 이력 자르기 — 긴 사용자 메시지는 끝 600자만 (2026-11-09 · graphio 18번 F-218 · 맥스 수정 고정).

이어진 대화에서 첫 증거 질의 원문(약 1.5만 자)이 이력에 통째로 실려 컨텍스트가 넘쳤다(데이터 질문 빈 답 · 공지문 약속).
graphio 는 이어 묻는 메시지마다 화면 맥락을 다시 싣는다 — 지워지는 것은 겹치는 부분뿐이다. 모델 없이 — 메시지 조립만.
"""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from datalens.application.agent import (
    HISTORY_USER_MAX_CHARS,
    HISTORY_USER_TAIL_CHARS,
    BoundedAgent,
    _history_content,
)
from datalens.infrastructure.session_store import InMemorySessionStore


def test_constants_are_the_decided_initial_values():
    assert HISTORY_USER_MAX_CHARS == 1200 and HISTORY_USER_TAIL_CHARS == 600


def test_long_user_message_in_lookup_mode_keeps_marker_and_last_600_chars():
    body = "A" * 14000 + "끝" * 600
    out = _history_content("user", body, "agent", "ko")
    assert out.endswith("끝" * 600) and "A" not in out                     # 앞 14,000자는 사라졌다
    assert out.startswith("[앞 질문 — 본문 14600자 중 끝 600자만 싣는다")
    assert len(out) < 800


def test_japanese_marker():
    out = _history_content("user", "x" * 5000, "agent", "ja")
    assert out.startswith("[前の質問 — 本文 5000字のうち末尾 600字だけを載せる")


@pytest.mark.parametrize("content", ["짧은 질문", "x" * 1200])
def test_short_or_exactly_limit_user_message_is_untouched(content):
    assert _history_content("user", content, "agent", "ko") == content


def test_assistant_history_is_never_cut():
    long = "답" * 5000
    assert _history_content("assistant", long, "agent", "ko") == long


def test_evidence_mode_is_never_cut():
    long = "증" * 15000
    assert _history_content("user", long, "evidence", "ko") == long


def _agent():
    class P:
        async def complete(self, *a, **k):
            raise AssertionError

        async def close(self):
            return None

    class Q:
        async def discover_tools(self, timeout_seconds=None):
            return ()

    return BoundedAgent(P(), Q(), max_tool_calls=2, recovery_budget=1)


def _session(history):
    s = InMemorySessionStore(60).create(locale="ja")
    return replace(s, turn_state=tuple(history)) if hasattr(s, "__dataclass_fields__") else s


def test_context_trims_history_but_not_the_current_message():
    agent = _agent()
    sess = _session([{"role": "user", "content": "H" * 15000 + "질문1"},
                     {"role": "assistant", "content": "답" * 3000}])
    now = "N" * 20000 + "지금 질문"
    msgs = agent._context(sess, now, None, agent.profile("agent"))
    users = [m for m in msgs if m.role == "user"]
    assert users[-1].content == now                                          # 현재 메시지는 그대로
    hist = [m.content for m in msgs[:-1] if m.role in ("user", "assistant")]
    assert hist[0].startswith("[前の質問") and hist[0].endswith("질문1") and len(hist[0]) < 800
    assert hist[1] == "답" * 3000
