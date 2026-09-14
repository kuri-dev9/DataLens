from __future__ import annotations

import time
from dataclasses import replace

from starlette.testclient import TestClient

from datalens.api.app import build_app
from datalens.application.sessions import SessionService
from datalens.infrastructure.session_store import InMemorySessionStore
from datalens.ports.queryforge import QueryForgeHttpResult, QueryForgeResult


HEADERS = {"x-api-key": "data-secret"}
_DEFAULT_SESSION = object()


class DirectQueryForge:
    def __init__(self) -> None:
        self.data_calls = []
        self.tool_calls = []
        self.dataset_owner = "Q" * 22
        self.tables = [
            {"name": f"table_{index:03d}", "comment": None, "row_count_estimate": index, "partitioned": False}
            for index in range(526)
        ]

    async def ready(self, timeout_seconds):
        return True

    async def close(self):
        return None

    async def fetch_dataset_rows(
        self, dataset_id, application_session_id, *, offset, limit, timeout_seconds
    ):
        self.data_calls.append(("rows", dataset_id, application_session_id, offset, limit))
        if dataset_id != "ds_owned" or application_session_id != self.dataset_owner:
            return QueryForgeHttpResult(404, {"error": "not_found"})
        return QueryForgeHttpResult(
            200,
            {
                "dataset_id": dataset_id,
                "offset": offset,
                "limit": limit,
                "rows": [{"value": index} for index in range(offset, offset + min(limit, 2))],
                "warnings": [],
            },
        )

    async def fetch_dataset_meta(
        self, dataset_id, application_session_id, *, timeout_seconds
    ):
        self.data_calls.append(("meta", dataset_id, application_session_id))
        if dataset_id != "ds_owned" or application_session_id != self.dataset_owner:
            return QueryForgeHttpResult(404, {"error": "not_found"})
        return QueryForgeHttpResult(
            200,
            {
                "dataset_id": dataset_id,
                "row_count": 323,
                "columns": [{"name": "value", "type": "int64"}],
                "parent_dataset_id": None,
            },
        )

    async def call_tool(
        self, name, arguments, *, application_session_id, timeout_seconds
    ):
        self.tool_calls.append((name, arguments, application_session_id))
        resolved_session_id = application_session_id or self.dataset_owner
        if arguments["action"] == "list_tables":
            pattern = arguments.get("name_pattern", "").casefold()
            tables = [table for table in self.tables if pattern in table["name"].casefold()]
            return QueryForgeResult(
                True,
                resolved_session_id,
                {
                    "ok": True,
                    "tables": tables[: arguments["limit"]],
                    "truncated": len(tables) > arguments["limit"],
                    "warnings": [],
                },
            )
        return QueryForgeResult(
            True,
            resolved_session_id,
            {
                "ok": True,
                "table": arguments["table"],
                "columns": [{"name": "value", "type": "int64"}],
                "truncated": False,
                "warnings": [],
            },
        )


def _client(
    settings,
    queryforge: DirectQueryForge,
    queryforge_session_id: str | None | object = _DEFAULT_SESSION,
):
    store = InMemorySessionStore(settings.session_ttl_seconds)
    sessions = SessionService(store)
    session = store.create()
    if queryforge_session_id is _DEFAULT_SESSION:
        queryforge_session_id = queryforge.dataset_owner
    store.update(replace(session, queryforge_session_id=queryforge_session_id))
    app = build_app(
        settings,
        session_service=sessions,
        readiness=queryforge,
        llm_readiness=queryforge,
        queryforge_client=queryforge,
    )
    return TestClient(app), session.session_id


def test_data_ac1_rows_and_meta_return_queryforge_payload_unchanged(settings) -> None:
    queryforge = DirectQueryForge()
    client, session_id = _client(settings, queryforge)
    with client:
        rows = client.get(
            f"/v1/datasets/ds_owned/rows?session_id={session_id}&offset=3&limit=2",
            headers=HEADERS,
        )
        meta = client.get(
            f"/v1/datasets/ds_owned/meta?session_id={session_id}", headers=HEADERS
        )
    assert rows.json() == {
        "dataset_id": "ds_owned",
        "offset": 3,
        "limit": 2,
        "rows": [{"value": 3}, {"value": 4}],
        "warnings": [],
    }
    assert meta.json() == {
        "dataset_id": "ds_owned",
        "row_count": 323,
        "columns": [{"name": "value", "type": "int64"}],
        "parent_dataset_id": None,
    }


def test_data_ac2_limit_1000_and_excess_are_clamped_to_queryforge_max(settings) -> None:
    queryforge = DirectQueryForge()
    client, session_id = _client(settings, queryforge)
    with client:
        assert client.get(
            f"/v1/datasets/ds_owned/rows?session_id={session_id}&limit=1000", headers=HEADERS
        ).status_code == 200
        assert client.get(
            f"/v1/datasets/ds_owned/rows?session_id={session_id}&limit=5000", headers=HEADERS
        ).status_code == 200
    assert [call[-1] for call in queryforge.data_calls] == [1000, 1000]


def test_data_ac3_cross_session_dataset_is_indistinguishable_from_missing(settings) -> None:
    queryforge = DirectQueryForge()
    client, session_id = _client(settings, queryforge, "R" * 22)
    with client:
        response = client.get(
            f"/v1/datasets/ds_owned/rows?session_id={session_id}", headers=HEADERS
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DL_DATASET_NOT_FOUND"
    assert "ds_owned" not in response.text


def test_data_ac4_proxy_routes_require_api_key(settings) -> None:
    queryforge = DirectQueryForge()
    client, session_id = _client(settings, queryforge)
    with client:
        assert client.get(
            f"/v1/datasets/ds_owned/rows?session_id={session_id}"
        ).status_code == 401
        assert client.get(f"/v1/catalog/tables?session_id={session_id}").status_code == 401
    assert queryforge.data_calls == []
    assert queryforge.tool_calls == []


def test_data_ac5_catalog_returns_all_tables_without_llm(settings) -> None:
    queryforge = DirectQueryForge()
    client, session_id = _client(settings, queryforge, None)
    with client:
        response = client.get(
            f"/v1/catalog/tables?session_id={session_id}&limit=1000", headers=HEADERS
        )
    assert response.status_code == 200
    assert response.json()["total_count"] == 526
    assert response.json()["returned_count"] == 526
    assert len(response.json()["tables"]) == 526
    assert queryforge.tool_calls[0][0] == "schema"
    assert queryforge.tool_calls[0][2] is None


def test_data_ac6_catalog_pattern_is_case_insensitive(settings) -> None:
    queryforge = DirectQueryForge()
    queryforge.tables = [
        {"name": "Alpha_Report"},
        {"name": "beta_report"},
        {"name": "unrelated"},
    ]
    client, session_id = _client(settings, queryforge)
    with client:
        response = client.get(
            f"/v1/catalog/tables?session_id={session_id}&pattern=REPORT", headers=HEADERS
        )
    assert [table["name"] for table in response.json()["tables"]] == [
        "Alpha_Report",
        "beta_report",
    ]
    assert queryforge.tool_calls[0][1]["name_pattern"] == "REPORT"


def test_catalog_columns_returns_original_columns_with_counts(settings) -> None:
    queryforge = DirectQueryForge()
    client, session_id = _client(settings, queryforge)
    with client:
        response = client.get(
            f"/v1/catalog/tables/orders/columns?session_id={session_id}", headers=HEADERS
        )
    assert response.status_code == 200
    assert response.json() == {
        "table": "orders",
        "total_count": 1,
        "returned_count": 1,
        "columns": [{"name": "value", "type": "int64"}],
        "truncated": False,
        "warnings": [],
    }
    assert queryforge.tool_calls[0][1] == {"action": "list_columns", "table": "orders"}


def test_data_ac7_direct_proxy_routes_complete_under_one_second(settings) -> None:
    queryforge = DirectQueryForge()
    client, session_id = _client(settings, queryforge)
    with client:
        started = time.monotonic()
        rows = client.get(
            f"/v1/datasets/ds_owned/rows?session_id={session_id}", headers=HEADERS
        )
        catalog = client.get(
            f"/v1/catalog/tables?session_id={session_id}", headers=HEADERS
        )
        elapsed = time.monotonic() - started
    assert rows.status_code == catalog.status_code == 200
    assert elapsed < 1
