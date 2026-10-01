"""임시 디버그 로그 버퍼 — request_id로 서버 로그를 조회한다 (운영 전 제거 대상).

데모 디버그 콘솔이 각 턴의 서버 로그를 끌어오는 통로다. 플래그가 꺼져 있으면
엔드포인트 자체가 존재하지 않아야 하고, 켜져 있어도 x-api-key 뒤에 있어야 한다.
"""

from __future__ import annotations

import logging

from starlette.testclient import TestClient

from conftest import ReadyProbe
from datalens.api.app import build_app
from datalens.api.debug_log_buffer import RingBufferLogHandler
from datalens.config import Settings


HEADERS = {"x-api-key": "data-secret"}


def make_settings(**overrides) -> Settings:
    return Settings(
        api_key="data-secret",
        queryforge_api_key="query-secret",
        queryforge_endpoint="http://queryforge.test:8080/mcp",
        queryforge_timeout_seconds=0.2,
        **overrides,
    )


def cleanup_ring_handlers() -> None:
    """install()은 루트 로거에 핸들러를 붙인다 — 테스트 간 오염을 막는다."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, RingBufferLogHandler):
            root.removeHandler(handler)


def test_flag_off_hides_debug_endpoint() -> None:
    app = build_app(make_settings(), readiness=ReadyProbe())
    try:
        with TestClient(app) as client:
            assert client.get("/v1/debug/logs", headers=HEADERS).status_code == 404
        assert not any(isinstance(h, RingBufferLogHandler) for h in logging.getLogger().handlers)
    finally:
        cleanup_ring_handlers()


def test_flag_on_returns_logs_filtered_by_request_id() -> None:
    app = build_app(make_settings(debug_log_buffer=True), readiness=ReadyProbe())
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        logging.getLogger("datalens.agent").info(
            "tool_call", extra={"request_id": "dlr_test_1", "step": 1, "tool": "query", "ok": True}
        )
        logging.getLogger("datalens.agent").info(
            "tool_call", extra={"request_id": "dlr_other", "step": 1, "tool": "schema", "ok": True}
        )
        with TestClient(app) as client:
            body = client.get("/v1/debug/logs?request_id=dlr_test_1", headers=HEADERS).json()
        assert body["count"] == 1
        assert body["logs"][0]["message"] == "tool_call"
        assert body["logs"][0]["request_id"] == "dlr_test_1"
        assert body["logs"][0]["tool"] == "query"
    finally:
        root.setLevel(previous_level)
        cleanup_ring_handlers()


def test_unfiltered_query_returns_recent_records_up_to_limit() -> None:
    app = build_app(make_settings(debug_log_buffer=True), readiness=ReadyProbe())
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    try:
        for index in range(5):
            logging.getLogger("datalens.agent").info("tool_call", extra={"request_id": f"dlr_{index}"})
        with TestClient(app) as client:
            body = client.get("/v1/debug/logs?limit=3", headers=HEADERS).json()
        assert body["count"] == 3
        assert [item["request_id"] for item in body["logs"]] == ["dlr_2", "dlr_3", "dlr_4"]
    finally:
        root.setLevel(previous_level)
        cleanup_ring_handlers()


def test_endpoint_requires_api_key() -> None:
    app = build_app(make_settings(debug_log_buffer=True), readiness=ReadyProbe())
    try:
        with TestClient(app) as client:
            assert client.get("/v1/debug/logs").status_code == 401
    finally:
        cleanup_ring_handlers()
