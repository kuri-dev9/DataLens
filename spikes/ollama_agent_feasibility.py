#!/usr/bin/env python3
"""Non-production Ollama/tool-call feasibility probe for DataLens Phase 0.

The probe never calls QueryForge. It validates representative QuerySpec fixtures and,
when explicitly requested, asks an already-running Ollama instance for a query tool call.
It does not install or download Ollama models.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
QUERY_SCHEMA_PATH = ROOT / "QueryForge/queryforge/schemas/query_spec.schema.json"
TOOL_SOURCE_PATH = ROOT / "QueryForge/queryforge/tools/base.py"


def valid_query() -> dict[str, Any]:
    return {
        "source": {"table": "events"},
        "select": [{"column": "event_time"}, {"column": "service"}],
        "partition_scope": {
            "kind": "time_range",
            "column": "event_time",
            "from": "2026-09-07",
            "to": "2026-09-07",
        },
        "limit": 10,
    }


def static_cases() -> dict[str, tuple[Any, bool]]:
    base = valid_query()
    return {
        "valid_time_range": (base, True),
        "missing_required_partition_scope": (
            {key: value for key, value in base.items() if key != "partition_scope"},
            False,
        ),
        "unknown_field": ({**base, "raw_sql": "select 1"}, False),
        "invalid_enum": (
            {**base, "partition_scope": {"kind": "yesterday"}}, False
        ),
        "wrong_type": ({**base, "limit": "10"}, False),
        "valid_relationship_reference": (
            {**base, "joins": [{"relationship_id": "rel_012345abcdef"}]},
            True,
        ),
        "invalid_relationship_reference": (
            {**base, "joins": [{"relationship_id": "events_to_users"}]},
            False,
        ),
        "valid_non_partitioned": (
            {**base, "partition_scope": {"kind": "not_partitioned"}}, True
        ),
    }


def run_static(schema: dict[str, Any]) -> dict[str, Any]:
    validator = Draft202012Validator(schema)
    results: dict[str, Any] = {}
    for name, (instance, expected_valid) in static_cases().items():
        errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
        actual_valid = not errors
        results[name] = {
            "expected_valid": expected_valid,
            "actual_valid": actual_valid,
            "pass": actual_valid == expected_valid,
            "errors": [error.message for error in errors[:3]],
        }

    malformed = '{"source":{"table":"events"},'
    try:
        json.loads(malformed)
        malformed_rejected = False
    except json.JSONDecodeError:
        malformed_rejected = True
    results["malformed_json"] = {
        "expected_valid": False,
        "actual_valid": not malformed_rejected,
        "pass": malformed_rejected,
        "errors": ["JSON parser rejected malformed input"] if malformed_rejected else [],
    }
    return results


def ollama_tool(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "query",
            "description": "Create a QueryForge Dataset using a validated QuerySpec.",
            "parameters": schema,
        },
    }


def post_json(url: str, payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], float]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body, time.monotonic() - started


def run_ollama(base_url: str, model: str, schema: dict[str, Any], timeout: float) -> dict[str, Any]:
    prompt = (
        "events 테이블에서 2026-09-07 하루의 event_time과 service를 10건 조회해줘. "
        "파티션 키는 event_time이다. 반드시 query 도구를 호출해."
    )
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [ollama_tool(schema)],
    }
    try:
        response, latency = post_json(f"{base_url.rstrip('/')}/api/chat", payload, timeout)
    except (urllib.error.URLError, TimeoutError) as error:
        return {"status": "BLOCKED_BY_ENVIRONMENT", "error": str(error)}

    message = response.get("message") or {}
    calls = message.get("tool_calls") or []
    if not calls:
        return {"status": "FAIL", "latency_seconds": latency, "reason": "no tool_calls"}
    function = (calls[0] or {}).get("function") or {}
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as error:
            return {
                "status": "FAIL",
                "latency_seconds": latency,
                "tool_name": function.get("name"),
                "reason": f"malformed arguments JSON: {error}",
            }
    errors = list(Draft202012Validator(schema).iter_errors(arguments))
    return {
        "status": "PASS" if function.get("name") == "query" and not errors else "FAIL",
        "latency_seconds": latency,
        "tool_name": function.get("name"),
        "arguments_type": type(arguments).__name__,
        "schema_errors": [error.message for error in errors[:5]],
        "arguments": arguments,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-ollama", action="store_true")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="gemma4:26b")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    schema = json.loads(QUERY_SCHEMA_PATH.read_text(encoding="utf-8"))
    static = run_static(schema)
    report: dict[str, Any] = {
        "kind": "datalens_ollama_agent_feasibility_spike",
        "production_code": False,
        "query_schema": str(QUERY_SCHEMA_PATH),
        "tool_source": str(TOOL_SOURCE_PATH),
        "static": static,
        "static_pass": all(result["pass"] for result in static.values()),
    }
    if args.live_ollama:
        report["ollama"] = run_ollama(args.ollama_url, args.model, schema, args.timeout)
    else:
        report["ollama"] = {"status": "NOT_REQUESTED"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["static_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
