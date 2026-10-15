from __future__ import annotations

from starlette.testclient import TestClient

from datalens.api.app import MESSAGE_MAX_CHARS, build_app
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
        for body in (
            {"message": ""},
            {"message": " "},
            {"message": "x", "extra": 1},
            {"message": "x" * (MESSAGE_MAX_CHARS + 1)},
        ):
            response = client.post(f"/v1/sessions/{session_id}/messages", headers=HEADERS, json=body)
            assert response.status_code == 400
        response = client.post(
            "/v1/sessions/dls_00000000000000000000/messages",
            headers=HEADERS,
            json={"message": "x"},
        )
        assert response.status_code == 404


def test_message_cap_matches_the_context_budget(settings) -> None:
    # 상한은 ollama_num_ctx(16384)에서 역산한 값이다. 한쪽만 바뀌면 프롬프트가
    # 조용히 잘린다 — 400 대신 "확인할 수 없다"는 답이 나온다.
    assert MESSAGE_MAX_CHARS == 16384
    assert settings.ollama_num_ctx == 16384


def test_validation_failure_reports_the_reason_without_echoing_input(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        session_id = create(client)
        secret = "비밀" * 9000
        response = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": secret}
        )
        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "DL_INVALID_REQUEST"
        assert error["message"] == "Invalid message request"
        assert error["details"]["fields"] == [
            {
                "field": "message",
                "error": f"String should have at most {MESSAGE_MAX_CHARS} characters",
                "type": "string_too_long",
            }
        ]
        # 입력값은 응답에 실리지 않는다 (pydantic errors()의 기본 include_input 차단)
        assert "비밀" not in response.text

        # 모르는 필드는 이름이 그대로 보인다 — 오타를 즉시 찾게 한다
        response = client.post(
            f"/v1/sessions/{session_id}/messages",
            headers=HEADERS,
            json={"message": "조회", "mesage": "오타"},
        )
        assert response.json()["error"]["details"]["fields"][0]["field"] == "mesage"
        assert response.json()["error"]["details"]["fields"][0]["type"] == "extra_forbidden"


def test_malformed_json_is_separated_from_field_violations(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        session_id = create(client)
        headers = {**HEADERS, "content-type": "application/json"}
        for path in (f"/v1/sessions/{session_id}/messages", "/v1/sessions"):
            response = client.post(path, headers=headers, content=b'{"message": ')
            assert response.status_code == 400
            error = response.json()["error"]
            # 파싱 실패 사유(위치·토큰)는 노출하지 않는다
            assert error == {
                "code": "DL_INVALID_REQUEST",
                "message": "Malformed JSON",
                "retryable": False,
                "details": None,
            }


def test_session_and_glossary_validation_also_report_the_reason(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        response = client.post("/v1/sessions", headers=HEADERS, json={"locale": "ko", "extra": 1})
        assert response.status_code == 400
        assert response.json()["error"]["message"] == "Invalid session request"
        assert response.json()["error"]["details"]["fields"][0]["field"] == "extra"


def test_delete_is_idempotent(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        session_id = create(client)
        assert client.delete(f"/v1/sessions/{session_id}", headers=HEADERS).status_code == 204
        assert client.delete(f"/v1/sessions/{session_id}", headers=HEADERS).status_code == 204
        response = client.post(
            f"/v1/sessions/{session_id}/messages", headers=HEADERS, json={"message": "x"}
        )
        assert response.status_code == 404
