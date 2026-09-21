#!/usr/bin/env python3
"""DataLens API 목 서버 — UI 개발용.

실제 백엔드나 내부망 접근 없이 UI를 만들 수 있게, 계약과 같은 응답을 돌려준다.
표준 라이브러리만 쓰므로 설치할 것이 없다.

    python3 scripts/mock_server.py                 # http://localhost:18121
    python3 scripts/mock_server.py --port 9000 --key my-key

데모 화면도 같이 서빙한다. 브라우저에서 http://localhost:18121/demo/ 를 열고
Base URL에 http://localhost:18121, API key에 dev-key를 넣으면 바로 동작한다.

질문 내용으로 시나리오를 고른다.
  "실패"  가 들어가면  도구 호출 도중 DL_AGENT_LIMIT 으로 중단되는 흐름
  "기억"  이 들어가면  memory 이벤트로 지름길을 타는 흐름
  그 외                schema -> query -> dataset -> 토큰 스트리밍 -> done
"""
from __future__ import annotations

import argparse
import json
import random
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSION_PATTERN = re.compile(r"^dls_[A-Za-z0-9_-]{20,64}$")
TABLES = [
    {"name": "PM_CEI_PGW_5M", "comment": "PGW 5분 성능 지표", "row_count_estimate": 597, "partitioned": False},
    {"name": "PM_CEI_SGW_5M", "comment": "SGW 5분 성능 지표", "row_count_estimate": 412, "partitioned": False},
    {"name": "APP_CODE", "comment": "공통 코드", "row_count_estimate": 1284, "partitioned": False},
]
COLUMNS = [
    {"name": "EVENT_TIME", "type": "datetime", "nullable": False, "comment": "수집 시각"},
    {"name": "PGW_ID", "type": "int", "nullable": False, "comment": "PGW 식별자"},
    {"name": "USER_CNT", "type": "int", "nullable": False, "comment": "사용자 수"},
    {"name": "DATA_CEI_VALUE", "type": "decimal(10,2)", "nullable": False, "comment": "데이터 CEI 값"},
]
ANSWER = (
    "마지막 날짜인 2025년 2월 4일 기준 PGW별 데이터 사용량입니다.\n\n"
    "| PGW_ID | 총 사용량 |\n| :--- | :--- |\n| 152 | 91.84 |\n| 1053 | 453.01 |\n| 1151 | 454.07 |\n\n"
    "이외 125개 PGW가 집계되었습니다. 특정 기간이 필요하시면 말씀해 주세요."
)


def now() -> datetime:
    return datetime.now(UTC)


def rows(count: int) -> list[dict]:
    return [
        {
            "EVENT_TIME": "2025-02-04T14:00:00",
            "PGW_ID": 100 + index,
            "USER_CNT": random.randint(1, 200000),
            "DATA_CEI_VALUE": round(random.uniform(80, 460), 2),
        }
        for index in range(count)
    ]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    api_key = "dev-key"
    delay = 1.0

    # ---- 배관 ---------------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        print(f"  {self.command:6s} {self.path}")

    def cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.headers.get("origin", "*"))
        self.send_header("Access-Control-Allow-Headers", "x-api-key, content-type, accept")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("x-request-id", f"dlr_{secrets.token_urlsafe(18)}")
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def send_error_payload(self, code: str, message: str, status: int, **extra) -> None:
        self.send_json(
            {
                "request_id": f"dlr_{secrets.token_urlsafe(18)}",
                "status": "failed",
                "error": {"code": code, "message": message, "retryable": False, "details": None, **extra},
            },
            status,
        )

    def authorized(self) -> bool:
        if self.path.startswith("/demo") or self.path in {"/", "/v1/health"}:
            return True
        if self.headers.get("x-api-key") == self.api_key:
            return True
        self.send_error_payload("DL_UNAUTHORIZED", "Unauthorized", 401)
        return False

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.cors()
        self.send_header("content-length", "0")
        self.end_headers()

    def do_DELETE(self) -> None:
        if not self.authorized():
            return
        self.send_response(204)
        self.cors()
        self.send_header("content-length", "0")
        self.end_headers()

    # ---- 라우팅 -------------------------------------------------------------

    def do_GET(self) -> None:
        if not self.authorized():
            return
        path = self.path.split("?", 1)[0]
        if path in {"/", "/demo", "/demo/"}:
            return self.serve_demo()
        if path == "/v1/health":
            return self.send_json({"status": "ok"})
        if path == "/v1/ready":
            return self.send_json(
                {
                    "status": "ready",
                    "checks": {
                        "queryforge": {"status": "ok"},
                        "llm": {"status": "ok"},
                        "memory": {"status": "ok"},
                    },
                }
            )
        if path.endswith("/catalog/tables"):
            return self.send_json({"tables": TABLES, "total_count": len(TABLES), "warnings": []})
        if "/catalog/tables/" in path and path.endswith("/columns"):
            return self.send_json({"columns": COLUMNS, "total_count": len(COLUMNS), "warnings": []})
        if path.endswith("/rows"):
            return self.send_json({"rows": rows(128), "offset": 0, "limit": 1000, "row_count": 128})
        if path.endswith("/meta"):
            return self.send_json({"dataset_id": "ds_000000008", "row_count": 128, "columns": COLUMNS})
        self.send_error_payload("DL_NOT_FOUND", "Not found", 404)

    def do_POST(self) -> None:
        if not self.authorized():
            return
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        if path == "/v1/sessions":
            created = now()
            return self.send_json(
                {
                    "session_id": f"dls_{secrets.token_urlsafe(18)}",
                    "created_at": created.isoformat(),
                    "expires_at": (created + timedelta(minutes=30)).isoformat(),
                    "locale": body.get("locale") or "ko",
                },
                201,
            )
        if path.endswith("/messages"):
            session_id = path.split("/")[3]
            if not SESSION_PATTERN.fullmatch(session_id):
                return self.send_error_payload("DL_SESSION_NOT_FOUND", "Session not found", 404)
            message = str(body.get("message") or "")
            if "text/event-stream" in (self.headers.get("accept") or ""):
                return self.stream(session_id, message)
            return self.send_json(self.final_payload(session_id, message))
        self.send_error_payload("DL_NOT_FOUND", "Not found", 404)

    def serve_demo(self) -> None:
        html = (ROOT / "demo" / "datalens-demo.html").read_bytes()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    # ---- SSE ----------------------------------------------------------------

    def emit(self, event: str, data: dict) -> None:
        chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
        self.wfile.write(chunk)
        self.wfile.flush()

    def stream(self, session_id: str, message: str) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("x-accel-buffering", "no")
        self.cors()
        self.end_headers()

        request_id = f"dlr_{secrets.token_urlsafe(18)}"
        self.emit("start", {"request_id": request_id, "session_id": session_id})
        started = time.monotonic()

        remembered = "기억" in message
        if remembered:
            self.emit(
                "memory",
                {
                    "recipes": [
                        {
                            "recipe_id": 1,
                            "question": "PGW별 사용량 구해줄래?",
                            "locale": "ko",
                            "steps": [{"tool": "query", "intent": {"table": "PM_CEI_PGW_5M", "group_by": ["PGW_ID"]}}],
                            "tables": ["PM_CEI_PGW_5M"],
                            "uses": 4,
                            "successes": 4,
                            "weight": 0.833,
                            "score": 0.89,
                        }
                    ],
                    "terms": [],
                }
            )

        steps: list[tuple[str, dict, dict | None]] = []
        if not remembered:
            steps.append(("schema", {"action": "list_tables"}, {"action": "list_tables", "tables_count": 3}))
            steps.append((
                "schema",
                {"action": "describe_table", "table": "PM_CEI_PGW_5M"},
                {"action": "describe_table", "table": "PM_CEI_PGW_5M", "columns_count": 90, "partitioned": False},
            ))
        query_intent = {
            "table": "PM_CEI_PGW_5M",
            "group_by": ["PGW_ID"],
            "aggregations": ["sum(DATA_CEI_VALUE)"],
            "partition_scope": {"kind": "not_partitioned"},
        }

        index = 0
        for tool, intent, result in steps:
            index += 1
            self.emit("tool_call", {"index": index, "tool": tool, "status": "started", "intent": intent})
            time.sleep(self.delay)
            self.emit(
                "tool_call",
                {"index": index, "tool": tool, "status": "completed", "elapsed_ms": 70, "intent": intent, "result": result},
            )

        if "실패" in message:
            index += 1
            self.emit("tool_call", {"index": index, "tool": "query", "status": "started", "intent": query_intent})
            time.sleep(self.delay)
            self.emit(
                "tool_call",
                {
                    "index": index, "tool": "query", "status": "rejected", "elapsed_ms": 0, "intent": query_intent,
                    "error": {"code": "INVALID_TOOL_ARGUMENTS", "detail": "aggregations/0: 'alias' is a required property"},
                    "recovery": {"attempt": 5, "budget": 5, "will_retry": False},
                },
            )
            failed_step = {"index": index, "tool": "query", "upstream_code": "INVALID_TOOL_ARGUMENTS", "intent": query_intent}
            self.emit(
                "error",
                {
                    "request_id": request_id,
                    "status": "failed",
                    "error": {
                        "code": "DL_AGENT_LIMIT",
                        "message": "Agent execution limit reached",
                        "retryable": False,
                        "details": {"failed_step": failed_step},
                    },
                    "metadata": {
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "tool_calls": index,
                        "recovery_count": 5,
                        "stop_reason": "failed",
                        "failed_step": failed_step,
                    },
                    "datasets": [],
                    "warnings": [],
                    "session_id": session_id,
                },
            )
            return

        index += 1
        self.emit("tool_call", {"index": index, "tool": "query", "status": "started", "intent": query_intent})
        time.sleep(self.delay)
        self.emit(
            "tool_call",
            {
                "index": index, "tool": "query", "status": "completed", "elapsed_ms": 146, "intent": query_intent,
                "result": {"dataset_id": "ds_000000008", "row_count": 128},
            },
        )
        self.emit(
            "dataset",
            {
                "dataset_id": "ds_000000008",
                "role": "primary",
                "row_count": 128,
                "columns": COLUMNS,
                "preview": rows(5),
            },
        )
        for piece in re.findall(r".{1,12}", ANSWER, re.DOTALL):
            self.emit("token", {"text": piece})
            time.sleep(self.delay / 40)
        self.emit(
            "done",
            {
                "status": "completed",
                "metadata": {
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "tool_calls": index,
                    "recovery_count": 0,
                    "stop_reason": "completed",
                    "memory": None,
                },
            },
        )

    def final_payload(self, session_id: str, message: str) -> dict:
        return {
            "request_id": f"dlr_{secrets.token_urlsafe(18)}",
            "session_id": session_id,
            "status": "completed",
            "answer": ANSWER,
            "datasets": [
                {"dataset_id": "ds_000000008", "role": "primary", "row_count": 128, "columns": COLUMNS, "preview": rows(5)}
            ],
            "warnings": [],
            "metadata": {"duration_ms": 4200, "tool_calls": 3, "recovery_count": 0, "stop_reason": "completed", "memory": None},
            "error": None,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="DataLens API 목 서버")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18121)
    parser.add_argument("--key", default="dev-key", help="x-api-key 값")
    parser.add_argument("--delay", type=float, default=1.0, help="도구 호출 한 건당 지연(초). 실제는 15~20초")
    args = parser.parse_args()

    Handler.api_key = args.key
    Handler.delay = args.delay
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    base = f"http://{args.host}:{args.port}"
    print(f"DataLens 목 서버 {base}")
    print(f"  데모 화면   {base}/demo/")
    print(f"  API key     {args.key}")
    print("  시나리오    질문에 '실패' 포함 → 오류 흐름 / '기억' 포함 → 회상 흐름")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    main()
