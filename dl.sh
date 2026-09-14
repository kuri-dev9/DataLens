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

chat() {
  local script="${ROOT_DIR}/scripts/chat.py"
  [[ -f "${script}" ]] || { echo "Missing: ${script}" >&2; exit 1; }
  DATALENS_ENV_FILE="${ENV_FILE}" python3 "${script}" "$@" || true
}

usage() {
  cat >&2 <<USAGE
Usage: $0 <command> [args]

운영
  preflight              설정 검증 (.env 필수값, compose 문법)
  up | down | restart    기동 / 정지 / 재시작
  rebuild                재빌드 후 기동
  status | logs          컨테이너 상태 / 로그 follow
  health                 health + ready 확인
  smoke                  세션 생성 검증
  agent-check            3턴 시나리오 회귀 검증

대화 테스트
  new [-l ko|ja]         새 세션 생성
  ask "질문" [-d] [-s]   질문 (세션 유지, 멀티턴)
  raw "질문"             질문 + 전체 응답 및 로그
  chat [-d] [-s]         대화형 반복 모드
  tables [pattern] [--limit N] [--stream]
                         카탈로그 테이블 목록
  columns <table> [pattern] [--stream]
                         테이블 컬럼 목록 (pattern은 로컬 출력 필터)
  rows <dataset_id> [--offset N] [--limit N] [--stream] [--json]
                         Dataset 행 조회
  meta <dataset_id>      Dataset 메타데이터
  trace                  DataLens / Ollama / QueryForge 로그 추적
  end                    세션 종료

옵션
  -d, --detail           응답 전문 JSON + 3계층 로그
  -l, --locale ko|ja     세션 로케일 (기본 ko)
  -t, --timeout <초>     요청 타임아웃 (기본 600)
  -s, --stream           Accept: text/event-stream으로 스트리밍
  --limit <건수>         tables/rows 조회 상한 (기본 1000)
  --offset <위치>        rows 시작 위치 (기본 0)
  --json                 가공하지 않은 전체 JSON 출력

예시
  $0 up
  $0 new -l ko
  $0 ask "PM_ENB_KPI_1M 컬럼 알려줘" --detail
  $0 tables PM_ --limit 1000
  $0 rows ds_000000003 --stream
  $0 chat
USAGE
  exit 2
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
  chat|ask|raw|new|end|trace|tables|columns|rows|meta) shift; chat "${command}" "$@" ;;
  *) usage ;;
esac
