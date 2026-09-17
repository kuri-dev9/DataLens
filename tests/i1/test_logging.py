from __future__ import annotations

import json
import logging

from datalens.observability import JsonFormatter, safe_log_value


def test_json_logging_is_structured() -> None:
    record = logging.makeLogRecord(
        {
            "name": "datalens.test",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "http_request",
            "request_id": "dlr_test",
            "status_code": 200,
        }
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "http_request"
    assert payload["request_id"] == "dlr_test"
    assert payload["status_code"] == 200


def test_secret_is_not_implicitly_logged(settings) -> None:
    record = logging.makeLogRecord({"name": "datalens.test", "levelno": 20, "levelname": "INFO", "msg": "ready"})
    rendered = JsonFormatter().format(record)
    assert settings.api_key.get_secret_value() not in rendered
    assert settings.queryforge_api_key.get_secret_value() not in rendered


def test_log_ac4_structured_values_are_masked_and_rows_are_omitted() -> None:
    safe = safe_log_value({
        "source": {"table": "records"},
        "filters": {"value": "private-row-value"},
        "password": "database-secret",
        "preview": [{"name": "private-row"}],
        "rows": [{"name": "private-row"}],
    })
    rendered = json.dumps(safe)
    assert safe["source"] == {"table": "records"}
    assert safe["filters"]["value"] == "***"
    assert "database-secret" not in rendered
    assert "private-row" not in rendered
