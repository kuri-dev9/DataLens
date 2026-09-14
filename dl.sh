#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${DATALENS_ENV_FILE:-${ROOT_DIR}/.env}"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"

env_value() {
  local name="$1" default_value="$2" value=""
  if [[ -f "${ENV_FILE}" ]]; then
    value="$(awk -F= -v key="${name}" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "${ENV_FILE}")"
  fi
  printf '%s' "${value:-${default_value}}"
}

api_base() {
  local host port
  host="$(env_value DATALENS_BIND_HOST 127.0.0.1)"
  port="$(env_value DATALENS_BIND_PORT 8000)"
  [[ "${host}" == "0.0.0.0" ]] && host="127.0.0.1"
  printf 'http://%s:%s' "${host}" "${port}"
}

api_key() { env_value DATALENS_API_KEY "${DATALENS_API_KEY:-}"; }

compose() {
  DATALENS_ENV_FILE="${ENV_FILE}" docker compose \
    --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

preflight() {
  command -v docker >/dev/null || { echo "Docker is required." >&2; exit 1; }
  docker compose version >/dev/null
  [[ -f "${ENV_FILE}" ]] || { echo "Missing environment file: ${ENV_FILE}" >&2; exit 1; }
  local name value
  for name in DATALENS_API_KEY DATALENS_QUERYFORGE_ENDPOINT \
    DATALENS_QUERYFORGE_DATA_BASE_URL DATALENS_OLLAMA_BASE_URL DATALENS_OLLAMA_MODEL; do
    value="$(env_value "${name}" "")"
    [[ -n "${value}" && "${value}" != replace-with-* ]] || {
      echo "Missing or placeholder configuration: ${name}" >&2
      exit 1
    }
  done
  for name in DATALENS_QUERYFORGE_ENDPOINT DATALENS_QUERYFORGE_DATA_BASE_URL DATALENS_OLLAMA_BASE_URL; do
    value="$(env_value "${name}" "")"
    [[ "${value}" == http://* || "${value}" == https://* ]] || {
      echo "Invalid HTTP(S) endpoint: ${name}" >&2
      exit 1
    }
  done
  compose config --quiet
  echo "Configuration preflight passed; dependency connectivity was not tested."
}

health() {
  local base key
  base="$(api_base)"
  key="$(api_key)"
  curl --fail-with-body --silent --show-error "${base}/v1/health"
  printf '\n'
  [[ -n "${key}" ]] || { echo "DATALENS_API_KEY is required for readiness." >&2; exit 1; }
  curl --fail-with-body --silent --show-error -H "x-api-key: ${key}" "${base}/v1/ready"
  printf '\n'
}

run_check() {
  local mode="$1" container_port
  container_port="$(env_value DATALENS_HTTP_PORT 8000)"
  compose exec -T datalens python - "http://127.0.0.1:${container_port}" "${mode}" <<'PY'
import json, os, sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

base, mode = sys.argv[1:]
api_key = os.environ["DATALENS_API_KEY"]
headers = {"x-api-key": api_key, "content-type": "application/json"}

def call(method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    try:
        with urlopen(Request(base + path, data=body, headers=headers, method=method), timeout=130) as response:
            raw = response.read()
            return None if not raw else json.loads(raw)
    except HTTPError as exc:
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}") from None

session = call("POST", "/v1/sessions", {})
session_id = session.get("session_id", "")
if not session_id.startswith("dls_"):
    raise RuntimeError("session creation returned an invalid DataLens session")
try:
    if mode == "smoke":
        print(json.dumps({"status": "ok", "session_id": session_id}))
    else:
        prompts = [
            "조회 가능한 테이블을 확인하고, 시계열 또는 파티션 테이블이 있다면 하나를 식별해 주세요.",
            "같은 세션의 앞선 선택을 사용해 최근 범위 데이터를 조회하고 핵심 내용을 요약해 주세요.",
            "현재 preview와 결과만 근거로 추가 조회가 필요한지 판단하고 이유를 설명해 주세요.",
        ]
        for turn, prompt in enumerate(prompts, 1):
            result = call("POST", f"/v1/sessions/{session_id}/messages", {"message": prompt})
            if result.get("session_id") != session_id or result.get("status") != "completed":
                raise RuntimeError(f"turn {turn} returned an invalid session or status")
            if not str(result.get("answer", "")).strip():
                raise RuntimeError(f"turn {turn} returned an empty answer")
            if turn == 2 and not result.get("datasets"):
                raise RuntimeError("turn 2 did not return a Dataset reference")
            print(json.dumps({"turn": turn, "status": "ok", "datasets": len(result.get("datasets", []))}))
finally:
    call("DELETE", f"/v1/sessions/{session_id}")
PY
}

command="${1:-help}"
case "${command}" in
  preflight) preflight ;;
  up) preflight; compose up -d --no-build ;;
  down) compose down ;;
  restart) compose restart ;;
  rebuild) preflight; compose build --pull; compose up -d ;;
  status) compose ps ;;
  logs) compose logs --tail=200 -f datalens ;;
  health) health ;;
  smoke) run_check smoke ;;
  agent-check) run_check agent-check ;;
  *) echo "Usage: $0 {preflight|up|down|restart|rebuild|status|logs|health|smoke|agent-check}" >&2; exit 2 ;;
esac
