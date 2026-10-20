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

#: 폐기한 명령. **조용히 사라지면 다음 사람이 옛 메모를 보고 그대로 친다.**
#: 왜 없어졌는지와 무엇을 쓸지 말하며 멈춘다 (graphio `gp.sh` 와 같은 모양).
retired() {
  local cmd="$1" why="$2" instead="$3"
  echo "✗ ${cmd} 는 폐기됐다 — ${why}" >&2
  echo "  대신: ${instead}" >&2
  exit 2
}

#: 인자로 받은 서비스 이름이 실제로 있는지 본다.
#:
#: **오타를 그냥 넘기면 "전체가 떴다"로 끝난다.** `docker compose up -d qeuryforge`
#: 는 "no such service" 로 죽지만, 우리가 인자를 그대로 넘기지 않고 가공하는
#: 자리가 생기면 조용히 빠질 수 있다. 여기서 먼저 막고 **무엇을 쓸 수 있는지**
#: 말한다.
check_services() {
  [[ $# -eq 0 ]] && return 0
  local known name
  known="$(compose config --services 2>/dev/null)"
  for name in "$@"; do
    grep -qx -- "${name}" <<<"${known}" || {
      echo "그런 서비스가 없다: ${name}" >&2
      echo "  쓸 수 있는 것: $(tr '\n' ' ' <<<"${known}")" >&2
      exit 1
    }
  done
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

운영  ※ [서비스] 를 주면 그것만 — 생략하면 전부 (queryforge | datalens)
  preflight              설정 검증 (.env 필수값, compose 문법)
  start   [서비스]       기동 (이미지가 있으면 그대로 쓴다)
  restart [서비스]       **재빌드 + 재기동** — 소스를 고친 뒤 쓴다
  stop    [서비스]       정지 + 제거 (네트워크·볼륨은 남는다)
  status  [서비스]       컨테이너 상태
  logs    [서비스]       로그 follow (기본 datalens)
  sh      [서비스]       컨테이너 셸 (기본 datalens)

  ※ graphio 의 ./gp.sh 와 같은 말이다 — 한쪽을 배우면 다른 쪽도 안다
  ※ start datalens 는 queryforge 도 함께 띄운다 (depends_on)
    정말 한 컨테이너만: docker compose up -d --no-deps <서비스>

폐기  up | down | rebuild   치면 이유와 대안을 말한다

점검
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
  $0 start                       # 전부 띄운다
  $0 restart queryforge          # QueryForge 소스·설정을 고쳤을 때
  $0 logs queryforge
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

  # ── 서비스 관리 — **graphio `gp.sh` 와 같은 말을 쓴다** (2026-10-20) ──
  #
  # 전에는 `up`·`restart` 가 **둘 다** `up -d --build` 였다. 이름이 둘인데
  # 하는 일이 같으니 "재빌드 없이 띄우는" 방법이 아예 없었고, `rebuild` 는
  # 빌드만 서비스를 가리고 기동은 전체였다. 어느 것이 무엇을 하는지 매번
  # 다시 읽어야 했다.
  #
  #     start   [서비스]   띄운다 (이미지가 있으면 그대로 쓴다)
  #     restart [서비스]   **재빌드 + 재기동** — 소스를 고친 뒤 쓴다
  #     stop    [서비스]   정지 + 제거
  #
  # 대상을 생략하면 전부다. 두 스크립트가 같은 말을 쓰면 한쪽을 배우면
  # 다른 쪽도 안다 — `gp.sh` 쪽이 먼저 이 모양으로 갔다.
  #
  # **의존 방향은 그대로 산다.** `start datalens` 는 queryforge 를 함께
  # 띄운다(compose 가 `depends_on: service_healthy` 를 지킨다). 정말 한
  # 컨테이너만 손대려면 `docker compose up -d --no-deps <서비스>` 인데,
  # 그 경우는 드물어서 명령으로 만들지 않았다.
  start)
    preflight; shift; check_services "$@"
    echo "기동"
    compose up -d "$@"
    compose ps ;;

  # `--build` 를 붙이는 것이 `start` 와의 유일한 차이다. 두 서비스 모두
  # 소스를 이미지에 굽기 때문에, 코드를 고쳤으면 이쪽이어야 반영된다.
  restart)
    preflight; shift; check_services "$@"
    echo "재기동 — 이미지를 다시 빌드한다"
    compose up -d --build "$@"
    compose ps ;;

  # **네트워크·볼륨은 남긴다.** `down` 은 그것까지 지우는데 `datalens-memory`
  # 볼륨에 임베딩이 들어 있어 한 번 날리면 다시 만드는 데 시간이 걸린다.
  # 전부 정리해야 할 때만 `docker compose down` 을 직접 친다.
  stop)
    shift; check_services "$@"
    echo "정지"
    compose stop "$@"
    compose rm -f "$@" ;;

  status) shift; check_services "$@"; compose ps "$@" ;;

  # 기본은 datalens 다 — 평소 보는 것이 그쪽이라서. 인자를 주면 그것만 본다.
  logs) shift; check_services "$@"; compose logs --tail=200 -f "${@:-datalens}" ;;

  # 컨테이너 셸. 기본은 datalens.
  sh) shift; check_services "$@"; docker exec -it "pp-${1:-datalens}" sh ;;

  # ── 폐기 — 치면 이유와 대안을 말한다 ────────────────────────────────
  #
  # 조용히 사라지면 다음 사람이 옛 문서·메모를 보고 그대로 친다.
  # `--pull` 은 기본이 아니다 — 베이스를 매번 받으면 Docker Hub rate
  # limit(429)에 걸린다. 필요하면 compose 를 직접 부른다.
  up)      retired "up" \
             "\`start\`(빌드 없이 기동)와 \`restart\`(재빌드)로 갈랐다" \
             "./dl.sh start [서비스]   — 재빌드가 필요하면 restart" ;;
  down)    retired "down" \
             "네트워크·볼륨까지 지워 datalens-memory 임베딩이 날아간다" \
             "./dl.sh stop [서비스]    — 정말 전부 정리하려면 docker compose down" ;;
  rebuild) retired "rebuild" \
             "빌드는 서비스를 가리고 기동은 전체여서 어긋났다" \
             "./dl.sh restart [서비스] — 베이스까지 받으려면 docker compose build --pull" ;;
  health) health ;;
  smoke) run_check smoke ;;
  agent-check) run_check agent-check ;;
  chat|ask|raw|new|end|trace|tables|columns|rows|meta) shift; chat "${command}" "$@" ;;
  *) usage ;;
esac
