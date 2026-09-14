from __future__ import annotations

from starlette.testclient import TestClient

from datalens.api.app import build_app
from conftest import ReadyProbe


def test_health_is_dependency_free(settings) -> None:
    probe = ReadyProbe(False)
    with TestClient(build_app(settings, readiness=probe)) as client:
        response = client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-request-id"].startswith("dlr_")


def test_untrusted_request_id_is_replaced(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe())) as client:
        response = client.get("/v1/health", headers={"x-request-id": "attacker-controlled"})
    assert response.headers["x-request-id"].startswith("dlr_")
    assert response.headers["x-request-id"] != "attacker-controlled"


def test_ready_reflects_queryforge(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe(True))) as client:
        response = client.get("/v1/ready", headers={"x-api-key": "data-secret"})
    assert response.status_code == 200
    assert response.json()["checks"]["queryforge"] == {"status": "ok"}
    assert response.json()["checks"]["llm"] == {"status": "ok"}


def test_ready_requires_datalens_api_key(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe(True))) as client:
        response = client.get("/v1/ready")
    assert response.status_code == 401


def test_not_ready_is_bounded_dependency_state(settings) -> None:
    with TestClient(build_app(settings, readiness=ReadyProbe(False))) as client:
        response = client.get("/v1/ready", headers={"x-api-key": "data-secret"})
    assert response.status_code == 503
    assert "query-secret" not in response.text
    assert "data-secret" not in response.text
