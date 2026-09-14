#!/usr/bin/env python3
"""DataLens 대화 테스트 / 디버깅 도구."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("DATALENS_ENV_FILE", ROOT / ".env"))
STATE = Path(os.environ.get("DL_STATE", "/tmp/dl_session"))

LOCALE_ALIASES = {
    "ko": "ko", "kr": "ko", "kor": "ko", "korean": "ko", "한국어": "ko",
    "ja": "ja", "jp": "ja", "jpn": "ja", "japanese": "ja", "日本語": "ja",
}

def norm_locale(value: str) -> str:
    key = str(value).strip().lower().replace("_", "-").split("-")[0]
    if key not in LOCALE_ALIASES:
        raise argparse.ArgumentTypeError(
            f"알 수 없는 로케일: {value} (사용 가능: ko/kr, ja/jp)")
    return LOCALE_ALIASES[key]

# ─────────────────────────── 설정 ───────────────────────────

def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


ENV = load_env()


def base_url() -> str:
    if os.environ.get("DL_BASE"):
        return os.environ["DL_BASE"].rstrip("/")
    host = ENV.get("DATALENS_BIND_HOST", "127.0.0.1")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return f"http://{host}:{ENV.get('DATALENS_BIND_PORT', '18121')}"


BASE = base_url()
KEY = ENV.get("DATALENS_API_KEY") or os.environ.get("DATALENS_API_KEY", "")
QF_BASE = "http://127.0.0.1:8080"
OLLAMA = "http://127.0.0.1:11434"

C = {"dim": "\033[2m", "b": "\033[1m", "r": "\033[31m", "g": "\033[32m",
     "y": "\033[33m", "c": "\033[36m", "x": "\033[0m"}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


# ─────────────────────────── HTTP ───────────────────────────

def call(method: str, path: str, payload=None, timeout=600, base=None):
    """(status, body, elapsed) 반환. body는 dict 또는 원문 str."""
    url = (base or BASE) + path
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"x-api-key": KEY, "content-type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read().decode(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", time.time() - t0
    el = time.time() - t0
    try:
        return status, json.loads(raw) if raw else {}, el
    except json.JSONDecodeError:
        return status, raw, el


# ─────────────────────────── 출력 ───────────────────────────

def show(status, body, wall, detail=False):
    if not isinstance(body, dict):
        print(f"{C['r']}  [!] HTTP {status} — JSON 아님{C['x']}")
        print(f"{C['dim']}{str(body)[:1500]}{C['x']}")
        print(f"{C['dim']}  wall {wall:.1f}s{C['x']}")
        return

    err = body.get("error")
    if err:
        print(f"\n{C['r']}  [{err.get('code')}]{C['x']} {err.get('message')}")
        if err.get("details"):
            print(f"{C['dim']}  details: "
                  f"{json.dumps(err['details'], ensure_ascii=False, indent=2)}{C['x']}")
        print(f"{C['dim']}  retryable={err.get('retryable')}{C['x']}")
    else:
        print("\n" + (body.get("answer") or "(빈 응답)"))

    m = body.get("metadata") or {}
    if m:
        print(f"\n{C['c']}  ── {m.get('duration_ms', 0)/1000:.1f}s"
              f" | tool_calls={m.get('tool_calls')}"
              f" | recovery={m.get('recovery_count')}"
              f" | stop={m.get('stop_reason')}"
              f" | status={body.get('status')}{C['x']}")

    for w in body.get("warnings") or []:
        print(f"{C['y']}  [warn] {w.get('code')}: "
              f"{w.get('detail') or w.get('reason', '')}{C['x']}")

    for ds in body.get("datasets") or []:
        cols = ds.get("columns") or []
        print(f"{C['g']}  [dataset] {ds.get('dataset_id')} "
              f"rows={ds.get('row_count')} cols={len(cols)}{C['x']}")
        if cols:
            names = [str(c.get("name", c)) if isinstance(c, dict) else str(c)
                     for c in cols[:12]]
            print(f"{C['dim']}      {', '.join(names)}"
                  f"{' ...' if len(cols) > 12 else ''}{C['x']}")
        if detail:
            print(f"{C['dim']}{json.dumps(ds, ensure_ascii=False, indent=2)[:2000]}{C['x']}")

    print(f"{C['dim']}  wall {wall:.1f}s | request_id={body.get('request_id')}{C['x']}")

    if detail:
        print(f"{C['dim']}\n── 전체 응답 ──\n"
              f"{json.dumps(body, ensure_ascii=False, indent=2)}{C['x']}")


# ─────────────────────────── 디버그 ───────────────────────────

def trace(since_ts: float, request_id: str | None = None):
    """DataLens 로그 + Ollama 로그에서 이번 턴의 흔적을 뽑는다."""
    print(f"\n{C['b']}══ DataLens 로그 ══{C['x']}")
    out = subprocess.run(
        ["docker", "compose", "logs", "datalens", "--tail", "200", "--no-log-prefix"],
        cwd=ROOT, capture_output=True, text=True).stdout
    for line in out.splitlines():
        if request_id and request_id in line:
            print(f"{C['dim']}{line[:400]}{C['x']}")
        elif any(k in line for k in ("tool", "ERROR", "WARN", "Traceback",
                                     "queryforge", "ollama")):
            print(f"{C['dim']}{line[:400]}{C['x']}")

    print(f"\n{C['b']}══ Ollama (생성 토큰 / 지연) ══{C['x']}")
    out = subprocess.run(
        ["journalctl", "-u", "ollama", "--since", "-5min", "--no-pager"],
        capture_output=True, text=True).stdout
    if not out:
        print(f"{C['dim']}  (sudo 필요: sudo journalctl -u ollama --since -5min){C['x']}")
    for line in out.splitlines():
        if re.search(r"n_gen|prompt eval|POST \"/api/chat\"", line):
            print(f"{C['dim']}{line[-160:]}{C['x']}")

    print(f"\n{C['b']}══ QueryForge 로그 ══{C['x']}")
    out = subprocess.run(
        ["docker", "compose", "logs", "queryforge", "--tail", "60", "--no-log-prefix"],
        cwd=ROOT, capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "/health" in line:
            continue
        print(f"{C['dim']}{line[:400]}{C['x']}")


def health():
    for name, url, hdr in (
        ("DataLens  ", f"{BASE}/v1/health", False),
        ("  ready   ", f"{BASE}/v1/ready", True),
        ("QueryForge", f"{QF_BASE}/health", False),
        ("Ollama    ", f"{OLLAMA}/api/version", False),
    ):
        req = urllib.request.Request(url, headers={"x-api-key": KEY} if hdr else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                print(f"{name}: {C['g']}{r.read().decode()[:200]}{C['x']}")
        except Exception as e:
            print(f"{name}: {C['r']}{e}{C['x']}")

    print(f"\n{C['b']}── 모델 / GPU ──{C['x']}")
    for cmd in (["ollama", "ps"],
                ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                 "--format=csv,noheader"]):
        try:
            print(subprocess.run(cmd, capture_output=True, text=True).stdout.strip())
        except FileNotFoundError:
            print(f"  ({cmd[0]} 없음)")

    print(f"\n{C['b']}── 설정 ──{C['x']}")
    for k in ("DATALENS_OLLAMA_MODEL", "DATALENS_OLLAMA_NUM_CTX",
              "DATALENS_ENABLE_THINKING", "DATALENS_COUNTRY",
              "DATALENS_REQUEST_DEADLINE_SECONDS", "DATALENS_AGENT_MAX_TOOL_CALLS",
              "DB_NAME", "DB_HOST", "DB_PORT"):
        print(f"  {k:38s} {ENV.get(k, '-')}")
    print(f"  {'BASE':38s} {BASE}")


# ─────────────────────────── 세션 ───────────────────────────

def new_session(locale="ko"):
    st, body, _ = call("POST", "/v1/sessions", {"locale": locale}, timeout=20)
    if st not in (200, 201) or not isinstance(body, dict) or "session_id" not in body:
        print(f"{C['r']}세션 생성 실패 (HTTP {st}): {body}{C['x']}")
        sys.exit(1)
    STATE.write_text(body["session_id"])
    print(f"{C['g']}새 세션{C['x']} {body['session_id']}  "
          f"locale={body.get('locale')}  만료={body.get('expires_at')}")
    return body["session_id"]


def get_session():
    if not STATE.exists():
        return new_session()
    return STATE.read_text().strip()


def ask(msg, detail=False, timeout=600):
    sid = get_session()
    print(f"{C['b']}─────────────────────────────────────────────{C['x']}")
    print(f"{C['b']}질문:{C['x']} {msg}")
    t0 = time.time()
    st, body, wall = call("POST", f"/v1/sessions/{sid}/messages",
                          {"message": msg}, timeout=timeout)
    if st == 404:
        print(f"{C['y']}  세션 만료 — 새로 생성합니다{C['x']}")
        sid = new_session()
        st, body, wall = call("POST", f"/v1/sessions/{sid}/messages",
                              {"message": msg}, timeout=timeout)
    show(st, body, wall, detail)
    if detail:
        trace(t0, body.get("request_id") if isinstance(body, dict) else None)
    return body


# ─────────────────────────── main ───────────────────────────

def main():
    p = argparse.ArgumentParser(description="DataLens 대화 테스트")
    p.add_argument("command", nargs="?", default="chat",
                   choices=["chat", "ask", "new", "end", "health", "trace"])
    p.add_argument("message", nargs="*", help="질문 내용")
    p.add_argument("--detail", "-d", action="store_true",
                   help="전체 JSON + DataLens/Ollama/QueryForge 로그 추적")
    p.add_argument("--locale", "-l", default="ko", type=norm_locale)
    p.add_argument("--timeout", "-t", type=int, default=601)
    a = p.parse_args()

    if not KEY:
        sys.exit(f"DATALENS_API_KEY를 읽지 못했습니다: {ENV_FILE}")

    if a.command == "health":
        health()
    elif a.command == "new":
        new_session(a.locale)
    elif a.command == "trace":
        trace(time.time())
    elif a.command == "end":
        if STATE.exists():
            sid = STATE.read_text().strip()
            call("DELETE", f"/v1/sessions/{sid}", timeout=10)
            STATE.unlink()
            print(f"세션 종료: {sid}")
    elif a.command == "ask":
        if not a.message:
            sys.exit("질문을 입력하세요.")
        ask(" ".join(a.message), a.detail, a.timeout)
    else:  # chat
        sid = get_session()
        print(f"세션 {sid}")
        print(f"{C['dim']}  종료 quit | 새 세션 /new [ko|ja] | 상세토글 /detail "
              f"| 로그 /trace | 상태 /health{C['x']}")
        detail = a.detail
        while True:
            try:
                line = input(f"\n{C['b']}> {C['x']}").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("quit", "exit", "q"):
                break
            if line.startswith("/new"):
                parts = line.split()
                try:
                    loc = norm_locale(parts[1]) if len(parts) > 1 else a.locale
                except argparse.ArgumentTypeError as e:
                    print(f"  {e}")
                    continue
                new_session(loc)
                continue
            if line == "/detail":
                detail = not detail
                print(f"  detail = {detail}")
                continue
            if line == "/trace":
                trace(time.time())
                continue
            if line == "/health":
                health()
                continue
            ask(line, detail, a.timeout)


if __name__ == "__main__":
    main()
