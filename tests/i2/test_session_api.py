from __future__ import annotations

from starlette.testclient import TestClient

from datalens.api.app import build_app
from conftest import ReadyProbe


HEADERS = {"x-api-key": "data-secret"}


def create(client: TestClient) -> str:
    response = client.post("/v1/sessions", headers=HEADERS, json={})
    assert response.status_code == 201
    assert response.headers["location"].endswith(response.json()["session_id"])
    return response.json()["session_id"]


def test_auth_and_public_session_contract(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        assert client.post("/v1/sessions", json={}).status_code == 401
        session_id = create(client)
        payload = client.post(
            f"/v1/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"message": "조회해줘"},
        ).json()
    assert payload["error"]["code"] == "DL_AGENT_NOT_READY"
    serialized = str(payload)
    assert "query-secret" not in serialized
    assert "sess_" not in serialized
    assert "mcp-session" not in serialized


def test_loc_ac1_session_uses_country_default_locale(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        response = client.post("/v1/sessions", headers=HEADERS, json={})
    assert response.status_code == 201
    assert response.json()["locale"] == "ko"


def test_loc_ac2_session_accepts_explicit_japanese_locale(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        response = client.post("/v1/sessions", headers=HEADERS, json={"locale": "ja"})
    assert response.status_code == 201
    assert response.json()["locale"] == "ja"


def test_loc_ac4_invalid_locale_returns_allowlist(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        response = client.post("/v1/sessions", headers=HEADERS, json={"locale": "en"})
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "DL_INVALID_LOCALE",
        "message": "Unsupported locale",
        "retryable": False,
        "details": {"allowed_locales": ["ko", "ja"]},
    }


def test_message_validation_and_unknown_session(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        session_id = create(client)
        for body in ({"message": ""}, {"message": " "}, {"message": "x", "extra": 1}, {"message": "x" * 8193}):
            response = client.post(f"/v1/sessions/{session_id}/messages", headers=HEADERS, json=body)
            assert response.status_code == 400
        response = client.post(
            "/v1/sessions/dls_00000000000000000000/messages",
            headers=HEADERS,
            json={"message": "x"},
        )
        assert response.status_code == 404


def test_delete_is_idempotent(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        session_id = create(client)
        assert client.delete(f"/v1/sessions/{session_id}", headers=HEADERS).status_code == 204
        assert client.delete(f"/v1/sessions/{session_id}", headers=HEADERS).status_code == 204
        response = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "x"}
        )
        assert response.status_code == 404
