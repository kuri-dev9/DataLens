from __future__ import annotations

import pytest
from pydantic import ValidationError

from datalens.config import Settings


def test_config_defaults_and_explicit_secrets() -> None:
    settings = Settings(api_key="api-secret", queryforge_api_key="qf-secret")
    assert settings.http_port == 8000
    assert settings.request_deadline_seconds == 240
    assert settings.session_ttl_seconds == 1800
    assert settings.country == "KR"
    assert settings.timezone == "Asia/Seoul"
    assert settings.default_locale == "ko"
    assert settings.ollama_num_ctx == 8192
    assert settings.ollama_temperature == 0.2
    assert settings.ollama_top_p == 0.95
    assert settings.ollama_top_k == 64
    assert settings.enable_thinking is False
    assert settings.agent_max_tool_calls == 12
    assert settings.agent_recovery_budget == 3
    assert "api-secret" not in repr(settings)
    assert "qf-secret" not in repr(settings)


def test_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATALENS_HTTP_PORT", "9010")
    monkeypatch.setenv("DATALENS_SESSION_TTL_SECONDS", "45")
    settings = Settings(api_key="x", queryforge_api_key="y")
    assert settings.http_port == 9010
    assert settings.session_ttl_seconds == 45


def test_loc_ac1_country_derives_default_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATALENS_COUNTRY", "KR")
    settings = Settings(api_key="x", queryforge_api_key="y")
    assert (settings.timezone, settings.default_locale) == ("Asia/Seoul", "ko")


def test_loc_ac5_invalid_country_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATALENS_COUNTRY", "XX")
    with pytest.raises(ValidationError, match="country"):
        Settings(api_key="x", queryforge_api_key="y")


def test_required_secrets_fail_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATALENS_API_KEY", raising=False)
    monkeypatch.delenv("DATALENS_QUERYFORGE_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("http_port", 0),
        ("request_deadline_seconds", 0),
        ("session_ttl_seconds", -1),
        ("queryforge_timeout_seconds", 0),
        ("agent_max_tool_calls", 0),
        ("agent_recovery_budget", -1),
        ("agent_preview_rows", 0),
        ("queryforge_endpoint", "not-a-url"),
    ],
)
def test_invalid_config_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(api_key="x", queryforge_api_key="y", **{field: value})
