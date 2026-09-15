from __future__ import annotations

from starlette.testclient import TestClient

from conftest import ReadyProbe
from datalens.api.app import build_app


ORIGIN = "http://localhost:8080"


class StreamingHandler:
    async def handle_stream(self, session, message, request_id, deadline, event_sink):
        await event_sink("token", {"text": "ok"})
        return {"status": "completed", "metadata": {"duration_ms": 1, "tool_calls": 0}}


def test_cors_ac1_ac2_preflight_bypasses_auth_and_returns_contract_headers(settings) -> None:
    app = build_app(settings, readiness=ReadyProbe())
    with TestClient(app) as client:
        response = client.options(
            "/v1/sessions",
            headers={
                "origin": ORIGIN,
                "access-control-request-method": "POST",
                "access-control-request-headers": "x-api-key, content-type, accept",
            },
        )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["access-control-allow-headers"] == "x-api-key, content-type, accept"
    assert response.headers["access-control-allow-methods"] == "GET, POST, DELETE, OPTIONS"


def test_cors_ac3_sse_response_includes_cors_headers(settings) -> None:
    app = build_app(settings, readiness=ReadyProbe(), message_handler=StreamingHandler())
    with TestClient(app) as client:
        session_id = client.post(
            "/v1/sessions", headers={"x-api-key": "data-secret"}, json={}
        ).json()["session_id"]
        response = client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={
                "origin": ORIGIN,
                "x-api-key": "data-secret",
                "accept": "text/event-stream",
            },
            json={"message": "hello"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["access-control-allow-origin"] == "*"


def test_cors_ac4_actual_requests_still_require_api_key(settings) -> None:
    app = build_app(settings, readiness=ReadyProbe())
    with TestClient(app) as client:
        for method, path in (("GET", "/v1/ready"), ("POST", "/v1/sessions"), ("DELETE", "/v1/sessions/missing")):
            response = client.request(method, path, headers={"origin": ORIGIN})
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "DL_UNAUTHORIZED"
            assert response.headers["access-control-allow-origin"] == "*"


def test_explicit_cors_origin_list_only_echoes_allowed_origin(settings) -> None:
    configured = settings.model_copy(update={"cors_origins": "https://one.test, https://two.test"})
    app = build_app(configured, readiness=ReadyProbe())
    with TestClient(app) as client:
        allowed = client.get("/v1/health", headers={"origin": "https://two.test"})
        denied = client.get("/v1/health", headers={"origin": "https://other.test"})
    assert allowed.headers["access-control-allow-origin"] == "https://two.test"
    assert "access-control-allow-origin" not in denied.headers
