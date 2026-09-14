# QueryForge — Architecture Decision Records

| 항목 | 내용 |
|---|---|
| 문서 ID | ADR-INDEX |
| 버전 | 0.8 |
| 최종 수정 | 2026-09-08 |
| 적용 범위 | QueryForge (일부 초기 DataLens 연계 결정은 역사적 배경으로 유지) |

## 이 문서의 목적

본 프로젝트는 착수 시점에 미확정 사항이 다수 존재한다. 미확정 항목을 문서 본문에 "TBD"로만
남기면 결정이 누락되거나 구현 단계에서 임의로 확정된다. 이를 방지하기 위해 모든 주요 기술 결정을
ADR로 분리하여 **결정 상태 / 대안 / 판단 기준 / 재검토 시점**을 명시적으로 관리한다.

### 상태 정의

| 상태 | 의미 |
|---|---|
| `ACCEPTED` | 확정. 변경 시 ADR 개정 필요 |
| `PROVISIONAL` | 잠정 결정. 지정된 시점에 실측 결과로 재검토 |
| `OPEN` | 미결정. 결정 기한과 필요 정보 명시 |
| `DEFERRED` | PoC 범위 외. 인터페이스만 확보 |
| `SUPERSEDED` | 다른 ADR로 대체됨 |

## ADR-001 — LLM 모델 및 서빙 스택

**상태**: `SUPERSEDED` — QueryForge 제품 범위 밖

### 맥락

초기 통합 구상에는 LLM 모델과 서빙 스택 선택이 포함되어 있었다. QueryForge가 독립 MCP Server로
분리되면서 모델 실행은 MCP Client 또는 상위 Agent의 책임이 되었다.

### 결정

- QueryForge는 특정 모델, GPU 또는 LLM 서빙 런타임에 의존하지 않는다.
- Client는 `tools/list`로 공개되는 JSON Schema를 사용해 tool call을 생성한다.
- 모델 선정과 컨텍스트 예산은 QueryForge 외부 배포 구성에서 결정한다.

---

## ADR-002 — Query 생성 경로

**상태**: `ACCEPTED` (재검토 S2)

### 결정

**Structured Query Specification을 유일한 1차 경로로 한다.**

- Spec의 중첩 깊이는 **최대 2단계**로 제한한다. 필터 기본형은 flat 리스트(암묵적 AND)이며,
  OR가 필요한 경우 명시적 그룹 1단계까지만 허용한다.
- operator는 자유 문자열이 아닌 **enum**으로 고정하고, 값은 타입을 명시한다.
- JOIN은 모델이 테이블/컬럼을 직접 기술하지 않는다. `relationship` tool이 반환한
  **relationship id를 참조**하는 방식만 허용한다.
- Spec 생성 시 **constrained decoding으로 JSON Schema를 강제**한다. 프롬프트로 포맷을 요청하지 않는다.

### 근거

소형 로컬 모델의 실패는 대부분 깊은 중첩 JSON에서 발생한다. 스키마를 얕게 유지하고 디코더 레벨에서
형식을 강제하면 이 실패 모드가 구조적으로 제거된다. JOIN을 relationship id로 간접화하면 모델이
스키마를 암기할 필요가 없어져 오류율이 크게 감소하고, FK가 없는 DB에서 Semantic Layer로 관계를
보강하는 경로와도 자연스럽게 연결된다.

### 안전 경계

생성 주체와 무관하게 **최종 실행되는 SQL은 예외 없이 AST Validator를 통과**한다.
문자열 정규식 기반 검사는 사용하지 않는다.

### Raw SQL 비도입

`query_sql` 및 raw WHERE 입력은 도입하지 않는다. v1 외부 계약은 기존 5 Tool이며 QuerySpec → Planner →
SQL Generator → AST Validator 경로를 유일한 조회 경로로 유지한다.

### 재검토

S2 종료 시 golden set으로 QuerySpec 경로의 정답률·스텝 수를 재검토한다.

---

## ADR-003 — MCP Transport

**상태**: `ACCEPTED`

### 결정

**Streamable HTTP**를 채택한다. stdio는 사용하지 않는다.

### 근거

QueryForge는 DataLens 전용 부속이 아니라 Claude, GPT 등 표준 MCP Client가 독립 설치해 사용하는
**제품성 범용 MySQL Data MCP Server**다. stdio는 클라이언트
프로세스가 서버를 자식으로 기동하는 모델이라 수명이 종속되고, Dataset을 서버에 상주시키는 구조와
맞지 않는다. HTTP는 별도 컨테이너 배포, 독립 재기동, 다중 클라이언트, 헬스체크가 모두 자연스럽다.
docker-compose 기반 폐쇄망 배포와도 정합한다.

### 결과

- QueryForge는 독립 컨테이너로 기동한다.
- 인증은 ADR-011을 따른다.
- 네트워크 경계가 생기므로 MCP Response 크기 제한(ADR-017)이 더 중요해진다.
- 공식 MCP SDK의 Streamable HTTP session과 protocol header를 사용한다. transport session은
  protocol 협상에 필요하지만 Dataset 소유권이나 승인 상태의 기준으로 사용하지 않는다.
- Dataset 접근 namespace와 future MutationPlan 격리는 명시적 `session_id`로 식별한다. Dataset retention은 별도 Store 정책이다.

---

## ADR-004 — QueryForge 프로세스 모델 및 Dataset 공유

**상태**: `ACCEPTED`

### 맥락

Dataset을 프로세스 메모리에 보관하는 구조에서 멀티 워커로 기동하면, 후속 요청이 다른 워커로 라우팅될 때
`ds_001`을 찾지 못한다. 이는 발견 시점이 늦을수록 되돌리기 비싼 종류의 결함이다.

### 결정

PoC는 **단일 워커 프로세스 + Persistent Local DatasetStore**로 고정한다.
`--workers 1`을 기동 스크립트에 명시하고, 다중 워커 기동 시 **기동 자체를 실패**시킨다
(설정 검증 단계에서 명시적 에러).

QueryForge Application Session은 MCP Transport Session 및 DataLens Application Session과 구분한다. 동일 Client가 재연결하거나
다른 표준 MCP Client가 호출해도 `session_id` 기반 접근 격리는 동일하며 TTL/retention은 Store가 관리한다.

### 확장 경로

향후 동시성이 필요하면 다음 중 하나를 택한다. 인터페이스는 `DatasetStore` 추상화로 미리 분리해 둔다.

1. 단일-writer coordination을 둔 로컬 store 공유
2. 공유/object Dataset 저장소

### 근거

1인 개발·단일 고객사 PoC 규모에서 동시 세션 수가 적고, 조기 분산화는 검증 부담만 키운다.
다만 "왜 단일 워커인가"가 명시되지 않으면 배포 단계에서 무심코 워커를 늘려 장애가 발생하므로,
기동 시 강제 검증을 함께 둔다.

---

## ADR-005 — 대상 DBMS

**상태**: `ACCEPTED` (2026-08-10 MySQL 8 확정). 부속 확인 항목 일부 `OPEN`

### 결정

**MySQL 8.x를 1차 대상으로 확정**한다. Adapter 구현체는 MySQL 하나만 작성하며,
`DatabaseAdapter` 인터페이스는 다중 DBMS를 전제로 설계한다.

### 버전 확정에 따른 결과

MySQL 8.0 이상이 확정되었으므로 다음이 성립한다. SQL Generator의 5.7 분기는 작성하지 않는다.

| 기능 | 가용 | 활용 |
|---|---|---|
| 윈도우 함수 (`RANK`, `ROW_NUMBER`, `LAG`/`LEAD`) | 가능 | 대용량 rank/전기 대비 연산을 DB에서 처리 가능 |
| CTE (`WITH`) | 가능 | 다단계 집계 SQL의 가독성·검증 용이성 향상 |
| `MAX_EXECUTION_TIME` 힌트 | 가능 | 쿼리 타임아웃 구현 (ADR-012) |
| `information_schema.PARTITIONS` | 가능 | 파티션 메타데이터 수집 (ADR-016) |
| 파티션 프루닝 검증 (`EXPLAIN`) | 가능 | 프루닝 적용 여부 자동 테스트 |
| utf8mb4 기본 | 대체로 가능 | 일본어·한국어 처리 (콜레이션은 확인 필요) |

**단, PoC 1차 구현에서는 윈도우 함수 사용을 보류한다.** rank·delta·pct_change 계열은
Polars에서 처리한다(Dataset이 이미 축소된 상태이므로 비용이 낮고 검증이 쉽다).
DB 측 윈도우 함수는 대용량 rank 요구가 실제로 발생할 때 Query Planner의 비용 판단으로 도입한다.
가용성이 확보되었다는 사실만 기록해 둔다.

### 잔여 확인 사항 (액션 아이템)

1. FK 제약 존재 여부 (통계성 스키마는 미설정인 경우가 많음 → ADR-009와 연결)
2. 문자셋/콜레이션 (utf8mb4 여부, 정렬 규칙 — 일본어 정렬에 영향)
3. 읽기 전용 계정 발급 가능 여부

### 구현 메모

- 쿼리 타임아웃은 MySQL의 `MAX_EXECUTION_TIME` 힌트로 구현한다 (표준 타임아웃과 상이).
- 파티션 메타데이터는 `information_schema.PARTITIONS`에서 수집한다.

---

## ADR-006 — Persistent Local Dataset Storage

**상태**: `ACCEPTED`

### 결정

**로컬 영속 DatasetStore**를 기본 구현한다. Dataset data는 Parquet, metadata·lineage·수명 상태·ID 발급 상태는 SQLite에 저장한다. 메모리는 hot cache이고 디스크는 cold storage이자 재기동 복원의 기준이다. Dataset lifecycle은 conversation lifecycle과 분리한다.

| 파라미터 | 초기값 | 비고 |
|---|---|---|
| Dataset TTL | 30분 | 설정 가능 |
| 세션당 최대 Dataset | 20개 | 초과 시 LRU 축출 |
| 단일 Dataset 최대 행 수 | 1,000,000 | 초과 시 `RESULT_TOO_LARGE` |
| 전체 Dataset 메모리 상한 | 8GB | hot cache 상한, 설정 가능 |
| 전체 Dataset 디스크 상한 | 배포 설정 | cleanup/eviction policy 수행 |
| Retention | 배포 설정 | TTL, last-access, lineage 보존 정책 |

### 근거

`DatasetStore`는 `put/get/evict/stats/recover`를 제공한다. 임시 Parquet 작성·fsync·검증 후 rename하고 SQLite transaction에서 published 상태를 커밋하여 atomic publish한다. 중단 시 미완성 파일과 preparing record를 복구 절차에서 정리한다.

### ID와 소유권

`dataset_id`는 SQLite-backed sequence 또는 충돌 저항적 ULID/UUID 기반으로 발급하며 재기동·cleanup 뒤에도 재사용하지 않는다. `session_id`는 접근 격리를 위한 namespace이지 보존 기한이 아니다. 대화 종료 통지는 cleanup hint다.

---

## ADR-007 — SQL AST Parser

**상태**: `PROVISIONAL` — S1에서 확정

### 결정

**sqlglot**(MySQL dialect)을 1순위 후보로 평가한다.

### 판단 기준

1. MySQL 8 문법(윈도우 함수, CTE, 파티션 힌트) 파싱 정확도
2. AST 순회로 금지 구문(DML/DDL/multi-statement) 탐지 가능 여부
3. 폐쇄망 설치 가능 여부 (순수 Python, 외부 바이너리 의존 없음)
4. 라이선스 (MIT 확인)

### 대비책

특정 구문 파싱에 실패할 경우, 파서를 교체하는 것이 아니라 **파싱 실패 자체를 거부 사유로 처리**한다
(fail-closed). 파싱할 수 없는 SQL은 실행하지 않는다.

---

## ADR-008 — Session Store

**상태**: `ACCEPTED` (PoC), 확장 `DEFERRED`

### 결정

PoC는 **in-memory 세션 저장 + SQLite 영속화 옵션**으로 한다.
`SessionStore` 인터페이스를 분리하고 기본 구현은 in-memory, 재기동 시 대화 이력 보존이 필요하면
SQLite 구현체를 활성화한다.

### 근거

외부 저장소(Redis 등)는 폐쇄망 배포 구성요소를 늘린다. 단일 워커(ADR-004) 전제에서 in-memory로 충분하고,
SQLite는 추가 컨테이너 없이 영속화를 얻을 수 있는 유일한 선택지다.

---

## ADR-009 — Semantic Store 형식 및 Domain Pack

**상태**: `ACCEPTED`

> **개정 있음 (2026-08-10)** — 「개정된 결정」절의 *ADR-009 개정 — Semantic Source of Truth 재확인* 참조.
> Domain Pack의 Source of Truth 지위를 재확인했고, `semantic.yaml`의 alias 관리 방식과
> `relationships.yaml` 스키마 요구(ADR-027)가 갱신되었다.

### 맥락

Core를 domain-agnostic으로 유지한다는 원칙과, 실제로 정확히 동작해야 한다는 요구는 그대로 두면 충돌한다.
해법은 도메인 지식을 **코드가 아닌 배포 아티팩트**로 분리하는 것이다.

### 결정

**Domain Pack**을 1급 개념으로 정의한다. DataLens 리포에 위치하며 버전 관리된다.

```
packs/<pack_name>/
├── pack.yaml            # 이름, 버전, 대상 스키마 fingerprint, 호환 범위
├── semantic.yaml        # alias, entity mapping, metric/KPI 정의
├── relationships.yaml   # FK 부재 시 관계 보강 선언
├── partitions.yaml      # 테이블별 파티션 키, 기본 조회 기간, 최대 조회 범위
├── locale/
│   ├── ko.yaml          # 로케일별 alias (한국어 표기)
│   └── ja.yaml          # 로케일별 alias (일본어 표기)
└── examples.jsonl       # few-shot 예시
```

- QueryForge Core에는 도메인 개념이 존재하지 않는다. `SemanticProvider` 인터페이스로만 소비한다.
- 배포별 업무 지식은 **오직 Domain Pack 안에만** 존재한다.
- **CI 검사**: 설정된 금지 도메인 용어가 QueryForge Core에서 검출되면 빌드 실패한다.

### 근거

물리적 경계와 자동 검사가 없으면, 마감 압박 상황에서 도메인 조건이 Core로 유입되는 것을 막을 수 없다.
1인 개발 체제에서는 리뷰어가 없으므로 CI 검사가 유일한 방어선이다.

### FK 부재 대응

MySQL 통계 스키마는 FK를 설정하지 않는 경우가 흔하다. 이 경우 `relationship` tool이 아무것도 반환하지
못하므로, `relationships.yaml`은 **선택 사항이 아니라 PoC 필수 산출물**로 취급한다.
관계 정보는 `FK 자동 수집 ∪ 선언 보강`으로 병합하며, 출처(`source: fk | declared`)를 함께 반환한다.

---

## ADR-010 — Vector DB / RAG

**상태**: `DEFERRED`

> **개정 있음 (2026-08-10)** — 「개정된 결정」절의 *ADR-010 개정 — Vector / RAG 결정의 분할* 참조.
> 본 결정은 ADR-010a(Schema Retrieval) / 010b(Query Memory Retrieval) / 010c(문서 RAG)로 분할되었고,
> 각각의 재평가 게이트가 명시되었다. `SemanticProvider` 시그니처는 동결이며 확장은 반환 타입으로 한다.

### 결정

PoC 범위에서 제외한다. `SemanticProvider` 인터페이스와
`resolve_term / find_entity / find_metric / find_relationship` 시그니처만 확정하고,
구현체는 `DictionarySemanticProvider`(Domain Pack YAML 기반) 하나만 작성한다.

향후 `RAGSemanticProvider`, `LearnedSemanticProvider`를 추가 삽입할 수 있도록 인터페이스를 고정한다.

### 재검토

PoC 종료 후. 임베딩 모델 구동은 서버 #2의 잔여 GPU를 사용한다(ADR-001).

---

## ADR-011 — 인증 / 인가

**상태**: `ACCEPTED` (PoC), 확장 `OPEN`

### 결정

- **DataLens API**: 정적 API Key 헤더 인증. 세션은 발급된 `session_id`로 격리하며,
  타 세션의 `dataset_id` 접근은 거부한다.
- **QueryForge MCP**: 내부 네트워크 전용. 공유 시크릿 헤더 인증. 외부 노출 금지. DataLens 외 Client에도
  동일한 인증·Application Session·Policy Enforcement를 적용한다.
- **DB 계정**: v1은 읽기 전용 계정 1개를 서비스 계정으로 사용한다. Future controlled Mutation은
  별도 최소권한 credential을 사용하며 read credential과 공유하지 않는다.

### 미결 사항 (OPEN)

최종 시스템에서 사용자별 데이터 접근 제어가 요구되는지 여부는 고객사 요구사항 확인 필요.
요구될 경우 `사용자 → 허용 스키마/테이블 목록` 매핑을 Domain Pack이 아닌 별도 정책 계층으로 도입한다.

---

## ADR-012 — 장시간 쿼리 응답 방식

**상태**: `ACCEPTED` (PoC)

### 결정

PoC는 **동기 응답 + 타임아웃**으로 한다.

| 구간 | 상한 |
|---|---|
| DB 쿼리 | 30초 (`MAX_EXECUTION_TIME`) |
| Transform | 10초 |
| Agent 턴 전체 | 120초 |

Agent 턴 전체 120초 deadline은 **DataLens API/Application 계층이 소유**하며, API 진입 시 생성한 하나의
wall-clock deadline을 LLM Provider와 QueryForgeClient에 남은 시간으로 전달한다. DB 쿼리 30초와
Transform 10초는 QueryForge 내부 실행 제한이며 외부 120초 deadline을 대체하지 않는다.

응답 스트리밍(SSE)과 job + polling 방식은 API 계약에만 예약 필드를 두고 구현하지 않는다.

### 근거

A2 환경에서 LLM 추론 지연이 전체 응답 시간을 지배할 가능성이 높다(ADR-001).
스트리밍은 체감 개선 효과가 크지만, 실측 지연을 확인하기 전에 구현하면 최적화 대상을 잘못 잡게 된다.
S0 실측 후 재검토한다.

---

## ADR-013 — DB Credential 관리

**상태**: `ACCEPTED`

### 결정

- 소스 코드 및 일반 설정 파일에 평문 저장 **금지**.
- docker-compose의 `secrets` 또는 컨테이너 주입 환경변수를 사용한다.
- 설정 파일에는 참조 키만 기록한다 (`password_ref: DB_PASSWORD`).
- 로그·에러 메시지·MCP 응답에 접속 문자열이 노출되지 않도록 마스킹 필터를 적용한다.
- Read와 Future Mutation credential은 별도 참조·회전·권한 정책으로 구성할 수 있어야 한다.

### 근거

폐쇄망에서는 외부 Secret Manager 사용이 불가능하다. 컨테이너 주입 방식이 추가 인프라 없이
평문 저장을 회피할 수 있는 현실적 최선이다.

---

## ADR-014 — 배포 패키징

**상태**: `ACCEPTED`

### 결정

- **docker-compose** 기반 독립 배포를 사용한다. production compose는 `build:` 없이 versioned
  `queryforge:<version>` image만 실행한다.
- 폐쇄망 반입은 `scripts/package.sh`가 생성하는 **오프라인 image tar + compose + 환경 template + 설정 + checksum** bundle을 사용한다.
- QueryForge는 독립 설치 가능한 wheel과 컨테이너 이미지를 자체 산출물로 만들 수 있으나, DataLens
  runtime은 QueryForge Python package를 import하거나 wheel에 직접 의존하지 않는다. 두 제품의 runtime
  경계는 versioned Streamable HTTP MCP contract다.
- 인터넷 접근을 전제로 하는 설치 절차(pip install from PyPI 등)를 런타임에 포함하지 않는다.

### 반입 산출물 목록

1. `queryforge-<version>.tar` (Docker image)
2. `docker-compose.yml` + `.env.example` + `queryforge.yaml`
3. `README.md` + `THIRD_PARTY_LICENSES.md` + `SHA256SUMS`

---

## ADR-015 — 로케일 및 타임존

**상태**: `ACCEPTED`

### 결정

**단일 국가 코드 환경변수**에서 언어·타임존·표기 형식을 모두 파생시킨다.

```yaml
environment:
  DATALENS_COUNTRY: JP     # KR | JP
```

`locales/JP.yaml`이 language(`ja`), timezone(`Asia/Tokyo`), 날짜 표기, 주 시작 요일, 숫자 표기를 정의한다.
API Server와 QueryForge가 동일한 환경변수를 공유한다.

### 책임 분리

| 컴포넌트 | 로케일 사용 |
|---|---|
| QueryForge | **타임존만** 사용(날짜 경계 계산). 언어를 모른다. 오류는 **코드**로만 반환 |
| DataLens | 오류 코드와 결과를 로케일에 맞춰 문장으로 렌더링 |

### 구현 규칙

1. 프롬프트 템플릿과 사용자 대면 문자열은 코드에서 분리한다 (`prompts/ko/`, `prompts/ja/`).
2. 응답 언어는 추론하지 않는다. 세션 속성으로 결정된다.
3. Alias 해석 시 **NFKC 정규화 + 대소문자 통일**을 적용한다.
   일본어의 전각/반각·카타카나 표기 흔들림 대응이며, 한국어에도 무해하다.
4. Golden set 항목에 `locale` 필드를 처음부터 포함한다.

### 근거

PoC 최종 타깃은 일본어이나 개발 기준은 한국어다. 로케일 분리는 초기에 넣으면 비용이 거의 없고,
나중에 넣으면 프롬프트·에러 메시지·alias 사전 전반을 소급 수정해야 한다.

### 미결 사항

일자 경계(day boundary) 기준 — 데이터 적재가 UTC 기준인지 현지시 기준인지, 일배치 경계가 몇 시인지
확인 필요. **"어제"의 해석이 하루 틀어질 수 있는 항목이므로 S1 착수 전 확인.**
`partitions.yaml`에 `timezone`과 `day_boundary`를 명시 필드로 둔다.

---

## ADR-016 — 파티션 프루닝 강제 정책

**상태**: `ACCEPTED`

### 맥락

대상 DB는 일단위 대용량 데이터가 파티션으로 분할되어 있으며, **본 시스템이 관리하는 자산이 아니다.**
시간 범위 필터가 누락된 쿼리 하나가 운영 DB에 심각한 부하를 유발할 수 있다.

### 결정

**Query Planner는 파티션 키에 대한 범위 조건이 없는 쿼리를 실행 전에 거부한다.**

1. Domain Pack의 `partitions.yaml`에 테이블별 파티션 키(시간 컬럼)와 최대 조회 범위를 선언한다.
2. 조건 누락 시 `MISSING_TIME_RANGE` 에러와 함께 해당 테이블의 파티션 키·기본 기간을 힌트로 반환한다.
3. Agent 세션은 `active_period`를 유지하며, 기본 기간(예: 어제)을 항상 주입한다.
4. 선언된 최대 조회 범위를 초과하면 `TIME_RANGE_TOO_WIDE`로 거부한다.
5. 개발 단계에서 `EXPLAIN`으로 파티션 프루닝 적용 여부를 검증하는 테스트를 유지한다.

### v1 DB 취급 원칙 (금지사항)

QueryForge는 대상 DB에 대해 다음을 **수행하지 않는다**.

- DDL (CREATE / ALTER / DROP)
- 인덱스 생성·변경·힌트 강제
- 파티션 조작
- 통계 갱신 (ANALYZE TABLE 등)
- 임시 테이블 생성
- v1 Read Capability에서 쓰기 권한 계정 사용

v1/PoC에서 허용되는 것은 `SELECT`, `EXPLAIN`(읽기 전용), `information_schema` 조회뿐이다.
이는 현재 활성 Capability의 제한이며 QueryForge의 영구 Read-only 제품 정의가 아니다. Future controlled
Mutation은 ADR-028의 별도 경계를 따라 INSERT/UPDATE/DELETE만 확장할 수 있고, TRUNCATE와 DDL은 계속 금지한다.

### 메타데이터 수집 (Catalog Snapshot)

매 호출마다 DB를 조회하지 않는다. `information_schema`의
`TABLES` / `COLUMNS` / `STATISTICS` / `KEY_COLUMN_USAGE` / `PARTITIONS`를 사전 수집하여
로컬 카탈로그로 보관하고, fingerprint(해시)로 변경을 감지한다.
갱신은 명시적 CLI 명령(`queryforge catalog refresh`)으로만 수행한다.

---

## ADR-017 — 컨텍스트 토큰 예산

**상태**: `PROVISIONAL` — ADR-001 확정 후 수치 조정

### 맥락

A2 15.3GB 환경에서 실사용 컨텍스트 상한은 8K~16K다(ADR-001). 이는 "권장 사항"이 아니라
**하드 제약**이며, MCP Response 설계와 Agent Loop 설계를 직접 구속한다.

### 결정 (16K 기준 배분)

| 항목 | 상한 | 비고 |
|---|---|---|
| 시스템 프롬프트 + Tool 정의 | 2,000 | Phase 0는 v1 Read Tool 5개만 노출 |
| 스키마 컨텍스트 | 2,500 | 선택된 테이블만 |
| 대화 이력 (압축본) | 2,000 | 원문 누적 금지 |
| Dataset 메타 + preview | 1,500 | DataLens가 `preview_rows`로 요청 예산 관리 |
| Semantic 컨텍스트 | 1,000 | alias, metric 정의 |
| 작업 여유 + 생성 | 나머지 | |

### 구조적 제약 (설계에 반영)

1. `schema` tool은 **전체 스키마 반환을 구조적으로 금지**한다.
   `list_tables` → `describe_table` 2단계 탐색만 허용한다.
2. MCP Response의 `preview`는 요청 단위 `preview_rows`로 조절한다(생략 시 기본 5행). 실제 반환량은
   serialization 및 `mcp.max_response_bytes` 서킷브레이커에 의해 더 작아질 수 있다. DataLens는 자신의
   컨텍스트 예산에 맞춰 요청값을 정하며 전체 데이터는 `dataset_id`로만 참조한다.
3. 대화 이력은 원문 누적이 아니라 **구조화 요약**(의도, 사용된 dataset_id, 결과 요약)으로 압축한다.
4. **DataLens Phase 0 Agent의 Tool 호출 상한 기본값은 3회**다. 이는
   `DATALENS_AGENT_MAX_TOOL_CALLS`로 관리하는 Phase 0 정책이며 QueryForge의 영구 제약이 아니다.
   초과 시 사용자에게 명확화 질문을 반환한다.
5. DataLens Phase 0는 QueryForge v1 Read contract의 5개 Tool(`schema`, `relationship`, `query`,
   `transform`, `describe`)만 allowlist로 노출한다. 이는 현재 v1 동결 계약이며 영구 불변조건은 아니다.
   Tool 추가·변경에는 별도 호환성·보안 검토가 필요하다.

### 근거

에이전트 루프의 성공률은 스텝당 신뢰도의 곱으로 감쇠한다. 스텝당 95%라도 8스텝이면 약 66%다.
따라서 스텝 수를 줄이는 설계가 곧 정확도 설계다. transform이 여러 operation을 하나의 pipeline으로
받는 구조는 이 목적에 직접 기여한다.

---

## ADR-018 — 개발 수행 모델 및 검증 전략

**상태**: `ACCEPTED`

### 맥락

구현은 전량 코딩 에이전트(Claude / Codex)가 수행하고, 사람은 설계·감독·방향성 평가만 담당한다.
이 체제에서 병목은 코드 작성이 아니라 **검증**이다.

### 결정

**모든 기능은 "인수 기준(Acceptance Criteria) + 검증 테스트"와 한 쌍으로만 정의한다.**

```
TOOL-003 query / OP-012 group_by
  입력 스키마: schemas/query_spec.schema.json#/definitions/groupBy
  동작: ...
  인수 기준:
    AC-1  group_by 컬럼이 select에 없으면 INVALID_QUERY_SPEC 반환
    AC-2  파티션 키 필터 부재 시 MISSING_TIME_RANGE 반환
    AC-3  생성 SQL이 EXPLAIN에서 partition pruning 적용됨
  테스트: tests/query/test_group_by.py::{ac1,ac2,ac3}
```

- 감독자는 코드를 읽지 않고 **인수 기준과 테스트 통과 여부만으로** 진척을 판정한다.
- 코딩 에이전트에 대한 지시는 "AC-2 실패 중, 수정하라" 형태가 된다.
- 설계 문서는 읽기용 문서가 아니라 **실행 명세**로 작성한다.

### 기능 범위 원칙

기능 목록은 합의된 범위 안에서 포함하고 **구현 깊이를 단계화**한다. Raw SQL/Text-to-SQL은 이 원칙의
대상이 아니며 도입하지 않는다. 확장점은 안전 경계와 실제 채택 결정이 있는 기능에만 둔다.

### 근거

PoC 종료 후에도 사용할 제품이 목표이므로 인터페이스 축소는 부채가 된다.
반면 검증되지 않은 구현은 감독자 1인 체제에서 위험 부담이 크다. 인터페이스는 완비하고
구현은 순차 확장하는 방식이 두 요구를 동시에 만족한다.

---

## ADR-019 — 리포지토리 분리

**상태**: `ACCEPTED`

### 결정

QueryForge와 DataLens를 **물리적으로 분리된 리포지토리**로 관리한다.

```
workspace/QueryForge/     # 독립 semver, 독립 CI
workspace/DataLens/       # QueryForge MCP contract의 Consumer
```

- QueryForge와 DataLens는 별도 프로세스·별도 deployable이다. DataLens는 QueryForge의 versioned
  Streamable HTTP MCP contract에 의존하며 QueryForge Python package를 import하지 않는다.
- QueryForge의 공개 tool schema와 contract fixture는 DataLens의 mock/contract test 입력으로 재사용할 수
  있으나 runtime source dependency로 만들지 않는다.
- CI 검사: QueryForge 리포에서 도메인 용어 검출 시 빌드 실패 (ADR-009).

### 근거

단일 리포에서는 마감 압박 시 도메인 로직이 Core로 유입되는 것을 막을 방법이 없다.
1인 개발 체제에는 리뷰어가 없으므로 물리적 경계와 자동 검사가 유일한 방어선이다.
또한 독립 versioned image와 명시적 MCP contract는 폐쇄망에서도 두 제품을 독립 검증·교체할 수 있게 한다
(ADR-014).

---

## ADR-020 — Semantic Truth 경계 및 학습 정책

**상태**: `ACCEPTED` (재검토 PoC 종료 시)

### 맥락

`SEMANTIC_ARCHITECTURE.md`와 `WRENAI_DATALENS_ARCHITECTURE_REVIEW.md`는 Semantic Layer를
런타임에 자동 학습·자동 승격되는 시스템으로 확장할 것을 제안했다. 제안의 문제 인식 — alias를 사람이
계속 등록하는 구조는 확장되지 않는다 — 은 타당하다. 그러나 자동 학습에는 이 시스템 고유의 위험이 있다.

**의미 오류는 fail-closed로 잡히지 않는다.** 존재하지 않는 컬럼, 시간 범위 누락, 잘못된 JOIN은
Validator와 Planner가 거부한다(ADR-002, ADR-016). 그러나 `소통률 → attach_success_rate`라는
잘못된 지표 매핑은 **유효한 SQL과 유효한 Dataset과 그럴듯한 문장을 만든다.** 어떤 계층도 거부할 근거가
없으며, 사용자는 숫자만 보고 판별할 수 없다. 판별할 수 있었다면 이 시스템이 필요 없다.

제안된 confidence 메커니즘은 이 위험을 막지 못한다. "반복 사용에서 성공"을 증거로 삼는데, 의미 오류는
**항상** 에러 없이 실행되므로 이 신호는 정확성과 무상관이다. 결과적으로 **침묵이 긍정 증거로 계수되어**
틀린 매핑의 confidence가 상승한다. 또한 12B급 로컬 모델의 self-reported confidence는 캘리브레이션이
나빠 0.72와 0.82 사이에 임계값을 걸 근거가 없다.

### 결정

**Semantic의 Source of Truth는 Domain Pack이다. 런타임 학습물은 Truth가 될 수 없다.**

1. **상태는 두 가지뿐이다.**

   | 상태 | 출처 | 사용 |
   |---|---|---|
   | `declared` | Domain Pack (사람이 작성·리뷰·커밋) | Query 생성에 직접 사용 |
   | `candidate` | 추론·대화·프로파일링 | **공개 없이 사용 금지.** 승격은 사람 승인으로만 |

   `DISCOVERED / VERIFIED / TRUSTED` 같은 다단계 lifecycle과 confidence decay는 도입하지 않는다.

2. **해석 공개 의무.** Pack에 선언되지 않은 용어를 해석했다면 최종 응답에 반드시 명시한다.
   예: "'소통률'을 성공률(SUCCESS_CNT / ATTEMPT_CNT)로 해석했습니다."
   이것 없이는 사용자가 부정 신호를 낼 경로 자체가 없으므로, 어떤 학습 메커니즘도 원리적으로 작동하지 않는다.

3. **침묵은 증거가 아니다.** `usage_count`, 무이의, 실행 성공은 confidence에 기여하지 않는다.
   **명시적 사용자 확인·교정만** 증거로 인정한다.

4. **자동 승격 금지.** `candidate` → `declared` 승격은 Domain Pack diff에 대한 사람 승인으로만 이루어진다.

5. **기본 격리 범위는 세션이다.** 확인된 매핑도 우선 `session_aliases`(FR-005)에 들어가며,
   다른 세션·사용자에게 자동 전파되지 않는다. Scope는 `pack` / `session` 2단계만 둔다.

6. **미해소 용어는 추측하지 않고 되묻는다.** Semantic Provider가 용어를 해소하지 못하면
   후보를 제시하고 명확화를 요청한다(UC-010, FR-016). 확인된 답은 `session_aliases`에 즉시 반영하고
   Candidate Store에 기록한다.

이 6개가 결합된 흐름을 **Clarify-Confirm-Log 루프**라 부른다.

```
미해소 용어 → 후보 제시(did_you_mean) → 사용자 확인
                                          ├→ session_aliases (즉시 반영)
                                          └→ Candidate Store (Pack diff 후보)
```

### 근거

이 루프는 추론된 매핑이 아니라 **사용자가 확정한 매핑**을 얻는다. 신규 컴포넌트가 0개이고
(`did_you_mean`·`session_aliases`·Candidate Store는 이미 설계에 존재한다), 추가 LLM 호출이 없으며,
컨텍스트 증가가 후보 목록 수준이다. P-4(결정적으로 확정 가능한 것을 LLM에 맡기지 않는다)와
P-6(fail-closed)에 정합한다.

비용도 결정 근거다. 제안된 자동 학습 구조는 confidence 정책·승격 기준·충돌 해소·scope 정책 등
**착수 전 결정해야 할 항목을 10건 이상 만든다.** 본 결정은 그중 대부분을 소멸시킨다.

또한 ADR-018의 검증 체제와 관련이 있다. `confidence 0.95 초과 시 승격`은 테스트할 수 있지만
**승격된 의미가 옳은지는 검증할 오라클이 없다.** 감독자 1인 체제에서 "테스트는 통과하는데 조용히 틀려가는"
기능은 가장 위험한 종류다.

### 대가

첫 만남에서 명확화 1턴을 소비한다. 이는 UC-010에 이미 정의된 정상 동작이며, 두 번째부터는
`session_aliases`로 0턴이다.

### 재검토

PoC 종료 시. Interaction Log(ADR-024)로 수집된 실제 표현 분포를 근거로,
자동 해소 비율을 높일 필요가 있는지 판단한다.

---

## ADR-021 — 표기 정규화 (Surface Form Normalization)

**상태**: `ACCEPTED`

### 맥락

제안 문서는 범용 유의어 사전(General Lexicon)을 도입해 표현 다양성을 흡수하자고 제안했다.
조달처로 "공개 사전, 동의어 데이터, WordNet 계열의 ETL"이 명시되었다.

이는 세 가지 문제가 있다. 첫째, 한국어 WordNet 계열은 연구 목적 조건이 붙는 경우가 있어
**상용 납품 전 법무 검토가 필요**하다. ADR-001이 Gemma 4를 택한 이유가 정확히 그 리스크의 회피였다.
둘째, 실제 문제 표현(`소통률`, `개통률`, `호성공률`)은 업계 은어이며 **어떤 범용 사전에도 없다.**
셋째, 사전이 실제로 효과를 내는 구간은 사전이 필요 없다.

제안 문서가 든 예시를 분류해 보면 드러난다.

```
성공 / 성공하다 / 성공한 / 성공됨   → 활용형. 형태 정규화로 처리
비율 / 율 / 백분율 / 퍼센트          → 표기·접미사 변형. 정규화로 처리
원인 / 이유 / 사유 / 요인            → 진짜 유의어. 그러나 지표명에 거의 쓰이지 않음
```

### 결정

**범용 유의어 사전을 도입하지 않는다. 결정적 표기 정규화기를 도입한다.**

ADR-015의 NFKC 정규화 요구를 확장하여 다음 규칙 집합을 로케일별로 정의한다.

| 단계 | 규칙 | 예 |
|---|---|---|
| 1 | NFKC 정규화, 대소문자 통일 (ADR-015 기존) | 전각/반각 통일 |
| 2 | 공백·구두점 제거 | `성공 률` → `성공률` |
| 3 | 비율 접미사 정규화 | `률\|율\|비율\|백분율\|퍼센트\|%` → `:RATE` |
| 4 | 개수 접미사 정규화 | `수\|건수\|개수\|카운트` → `:COUNT` |
| 5 | 조사·한정 접미사 제거 | `별\|의\|를\|은\|는\|당` |

결과적으로 `성공률 / 성공율 / 성공 비율 / 성공퍼센트`가 전부 `성공:RATE`로 수렴한다.
**Domain Pack에는 정식 명칭 하나만 등록한다.**

- 규칙은 코드가 아니라 `locales/<CC>.yaml`에 선언한다(ADR-015의 로케일 분리 원칙).
- 정규화는 Semantic 조회 이전에 적용하며, 원문은 보존한다(Interaction Log 기록용).
- 정규화로 해소되지 않으면 ADR-020의 Clarify-Confirm-Log 루프로 넘어간다.

### 근거

Alias 폭발 문제의 상당 부분이 유의어가 아니라 **표기 변형**이다. 결정적 규칙은 사전보다 정확하고,
디버깅 가능하고, 폐쇄망 반입물이 늘지 않고, 라이선스 문제가 없고, AC 작성이 자명하다
(입력 → 기대 정규형 테이블 테스트). 구현 규모는 규칙 테이블 + 함수 수십 줄이다.

### 재검토

PoC 종료 시. Interaction Log에서 정규화 후에도 미해소된 표현의 비율을 측정한다.

---

## ADR-022 — 식별자 컬럼 집계 안전 규칙

**상태**: `ACCEPTED`

### 맥락

`NODE_ID`, `EQUIP_ID`, `MTSO_ID` 등은 숫자형이거나 숫자 문자열이지만 합산 대상이 아니다.
`SUM(NODE_ID)`는 구문상 유효하고 실행되며 **의미 없는 숫자를 반환한다.** ADR-020에서 지적한
"조용한 오류"의 전형이다.

### 결정

**QueryForge Validation에 결정적 규칙으로 둔다.** LLM에 대한 힌트로 처리하지 않는다.

1. 식별자 성격 컬럼에 `SUM` / `AVG` / `STDDEV` / `VARIANCE` / `MEDIAN` / `QUANTILE`을 적용하면
   `INVALID_AGGREGATION`으로 거부한다.
2. 허용 집계는 `COUNT`, `COUNT DISTINCT`, `MIN`, `MAX`이다.
3. 오류 응답에 허용 집계 목록을 힌트로 동봉하여 Agent 자가 교정 경로(SAD 17.2)를 탄다.
4. 식별자 판정 근거:

   ```
   PK ∪ FK ∪ relationship key ∪ 명명 패턴(*_ID, *_KEY, *_CODE, *_NO)
   ```

5. Domain Pack에서 컬럼 단위 예외를 선언할 수 있다(오탐 대비).

### 근거

이 규칙은 **도메인 지식이 아니라 구조 지식**이므로 N-1 및 ADR-009의 도메인 용어 금지에 저촉되지 않는다.
QueryForge의 domain-neutral 원칙과 양립한다. PK/FK는 Catalog Snapshot에서 이미 수집하므로
추가 데이터 수집이 없다(ADR-016). 파티션 프루닝 강제와 동일한 성격의 안전 규칙이며 같은 위치에 놓는 것이 일관적이다.

무엇보다 **결정적**이므로 P-4에, **fail-closed**이므로 P-6에 정합하고, AC 작성이 자명하다.

---

## ADR-023 — Schema Profiling 수집 정책

**상태**: `ACCEPTED`

### 맥락

Catalog Snapshot에 distinct count, sample values, cardinality 등 프로파일 정보를 추가하면
컬럼의 역할(categorical dimension / metric / identifier) 추론에 도움이 된다. 제안 문서가 이를 요청했다.

그러나 제안 문서는 **수집 방법을 언급하지 않았다.** 대상 테이블은 일단위 대용량 파티션 테이블이며
외부 자산이다. `SELECT COUNT(DISTINCT category_code) FROM partitioned_fact`를 무심코 실행하면
운영 DB 풀스캔이며 N-8 위반이자 NFR-010(프루닝 미적용 쿼리 0건) 즉시 실패다.

### 결정

프로파일 정보를 Catalog Snapshot에 추가하되 **수집 경로를 다음으로 제한한다.**

1. **`information_schema` 우선.** `STATISTICS.CARDINALITY`로 충족되는 항목은 추가 쿼리를 발생시키지
   않는다. 근사값이지만 "categorical 후보인가" 판단에는 충분하다.
2. **실데이터 접근이 필요한 항목은 단일 파티션 한정 + `LIMIT` 강제.** 전체 스캔을 금지한다.
3. **런타임 수집 금지.** `queryforge catalog refresh --profile` CLI에서만 수행한다
   (ADR-016의 명시적 갱신 원칙, N-14와 정합).
4. `MAX_EXECUTION_TIME`을 동일하게 적용한다.
5. 수집 결과는 `declared`가 아니라 **`candidate` 근거 자료**다(ADR-020). 프로파일링 결과가
   곧바로 Semantic Truth가 되지 않는다.

### 근거

프로파일 정보의 가치는 인정하되, 그것을 얻는 과정이 PoC 성공 판정 기준(프루닝 미적용 쿼리 0건)을
위반해서는 안 된다. 대상 DB는 본 시스템이 관리하지 않는 외부 자산이라는 원칙(P-8)이 프로파일링에도 적용된다.

---

## ADR-024 — Interaction Log 및 Query 기록 정책

**상태**: `ACCEPTED`

### 맥락

제안 문서는 NL ↔ QuerySpec 쌍을 Vector Memory에 저장하고 유사 질의를 검색하자고 했다.
검색 부분은 PoC에서 가치가 없다 — 저장된 이력이 0건이기 때문이다.

그러나 **저장 부분은 PoC에서 가장 가치가 높은 항목 중 하나다.** ADR-018은 이 프로젝트의 병목이
검증이라고 선언하고, NFR-007/008은 golden set 정답률로 판정된다. 그런데 golden set은 1인이 손으로 만든다.

또한 제안 문서의 핵심 통찰 — **SQL이 아니라 QuerySpec을 기록해야 한다** — 은 타당하다.
QuerySpec은 DBMS·dialect·물리 스키마에 종속되지 않으므로 장기 재사용성이 SQL보다 높다.

### 결정

성공·실패한 상호작용을 **JSONL 구조화 로그로 기록한다. 인덱싱·검색은 구현하지 않는다.**

```jsonl
{"ts":"...","session_id":"...","locale":"ko",
 "utterance":"서비스별 성공률 보여줘",
 "utterance_normalized":"SERVICE:RATE...",
 "resolution":[{"term":"성공률","concept":"success_rate","source":"pack","status":"declared"}],
 "intent":"new_query","spec":{...QuerySpec...},"tool":"query",
 "outcome":"ok","rows":42,"steps":2,"latency_ms":8300,"error_code":null}
```

**기록 정책:**

- 발화 원문, 정규화형, 해소 결과, QuerySpec, 실행 결과 메타를 기록한다.
- **행 데이터 값은 기록하지 않는다**(NFR-011). 필터 리터럴은 마스킹 정책을 적용한다.
- 보존 기간은 설정값으로 두고 기본 90일. 세션 종료와 무관하게 유지된다(Session Store와 별개).
- 폐쇄망 내 로컬 파일에만 기록한다.

**활용:**

| 용도 | 설명 |
|---|---|
| Golden set 확장 | 실사용 발화를 GQ- 항목으로 승격. 손으로 지어내지 않는다 |
| 표현 분포 관측 | alias 폭발 문제의 실재성을 데이터로 판정 (ADR-020 재검토 근거) |
| 회귀 탐지 | 동일 발화의 QuerySpec 변화 감지 |
| NFR 실측 | steps, latency가 그대로 지표 |
| 미래 검색 학습 데이터 | 나중에 벡터화할 때 이미 데이터가 존재 |

### 근거

비용이 사실상 0이다. SAD 20(감사)과 21.3(관측)이 이미 Tool Call 로그와 실행 SQL 기록을 요구하므로,
`resolution`과 `spec` 필드를 추가하는 정도다.

순서에도 근거가 있다. **데이터는 소급 생성이 불가능하지만 인덱스는 언제든 소급 생성이 가능하다.**
따라서 저장을 먼저 하고 검색을 나중에 붙이는 것이 옳다.

### 재검토

PoC 종료 시. 축적된 이력의 양과 패러프레이즈 빈도를 근거로 검색 계층 도입 여부를 ADR-010 개정에서 판단한다.

---

## ADR-025 — 외부 오픈소스 참조 및 라이선스 경계

**상태**: `ACCEPTED`

### 맥락

`SEMANTIC_ARCHITECTURE.md` §17·§26은 WrenAI(Canner)의 구체적 파일 목록을 구현 참고 대상으로 제시한다
(`store.py`, `schema_indexer.py`, `embeddings.py`, `seed_queries.py` 등). 아이디어 참조로는 타당하다.

그러나 WrenAI는 **혼합 라이선스** 구조다. 저장소는 역사적으로 AGPL-3.0이었고 이후 core/SDK/skills가
Apache-2.0으로 정리되었으나 일부 모듈에 AGPL-3.0이 유보되어 있다는 설명이 있다.
DataLens는 고객사 납품 상용 제품이 목표이므로(SAD 1.3), AGPL-3.0 코드가 유입되면
네트워크 서비스 제공 시 소스 공개 의무가 발생할 수 있다. **ADR-001이 회피한 것과 같은 종류의 리스크다.**

### 결정

1. **설계 참고는 허용, 코드 파생은 라이선스 확인 전 금지.**
   동작 원리·자료구조·처리 순서를 이해하고 자체 구현하는 것은 허용한다.
   소스를 복사·이식·번역하거나 구조를 그대로 옮기는 것은 금지한다.
2. **파일 단위 확인 의무.** 저장소 루트 `LICENSE`만 보고 판단하지 않는다.
   참조 대상 파일 각각의 라이선스 헤더를 개별 확인하고 결과를 기록한다.
3. **코딩 에이전트 지시문에 명시한다.** 에이전트는 참고 코드를 그대로 옮겨 쓰는 경향이 있다.
   ADR-009·ADR-019가 전제하듯 1인 체제에는 리뷰어가 없으므로, 지시문에 명시하는 것이 방어선이다.
4. 이 정책은 WrenAI에 한정되지 않고 **모든 외부 오픈소스 참조에 적용**한다.
   특히 언어 자원(WordNet 계열 등)에도 동일하게 적용한다(ADR-021).

### 근거

납품 단계에서 발견되는 라이선스 문제는 되돌리는 비용이 가장 비싸다. ADR-001이 모델 선정에서
같은 판단을 이미 내렸으므로, 코드와 데이터에 대해서도 동일한 기준을 적용하는 것이 일관적이다.

---

## ADR-026 — 평가 재현성 (Semantic Pinning)

**상태**: `ACCEPTED`

### 맥락

ADR-018은 감독자가 **인수 기준과 테스트 통과 여부만으로** 진척을 판정한다고 규정한다.
그런데 Semantic이 런타임에 변화하면 동일한 golden question이 시점에 따라 다른 QuerySpec을 만든다.
어제 통과한 GQ-041이 오늘 실패하는데 코드는 그대로인 상황이 발생하면 이 체제는 성립하지 않는다.

### 결정

1. **평가 실행은 Semantic 버전을 핀으로 고정한다.** golden set 실행 시 사용한 Domain Pack 버전과
   Pack fingerprint를 결과에 함께 기록한다.
2. **`session_aliases`와 Candidate Store는 평가 경로에서 비활성화한다.** 평가는 `declared` Semantic만 사용한다.
3. 평가 결과 리포트에 `pack_version`, `pack_fingerprint`, `catalog_fingerprint`를 포함한다.

### 근거

ADR-020이 자동 승격을 금지하므로 드리프트 위험은 이미 낮다. 그러나 세션 학습(FR-005)과
향후 Candidate 승격은 여전히 Semantic을 변화시키므로, 평가 경로의 격리를 명시적으로 못박아 둔다.
비용이 거의 없고, 나중에 넣으면 이미 축적된 평가 이력의 신뢰성을 소급 판단할 수 없다.

---

## ADR-027 — Relationship 선언 스키마

**상태**: `ACCEPTED` — 세부 필드는 S1에서 확정

### 맥락

ADR-002는 JOIN을 relationship id 참조로만 허용하고, ADR-009는 FK 부재 시 `relationships.yaml`을
"선택 사항이 아니라 PoC 필수 산출물"로 규정한다. 그러나 **그 파일의 스키마가 어느 문서에도 정의되어
있지 않았다.**

복합 키 fixture 분석 결과, 관계가 단일 컬럼 조인으로 표현되지 않을 수 있음이 확인되었다.

```text
partitioned_fact.source_kind + source_id  ↔  dimension_entity.target_kind + target_id
```

단일 컬럼 조인만 지원하도록 구현되면 UC-003(관계 확장 조회)이 복합 키 관계에서 동작하지 않으며,
이는 PoC 성공 판정 기준 1번의 실패를 의미한다.

### 결정

`relationships.yaml` 스키마는 최소한 다음을 표현할 수 있어야 한다.

| # | 요구 | 근거 |
|---|---|---|
| R-1 | **복합 키** — 2개 이상 컬럼 쌍을 하나의 relationship id로 선언 | 복합 관계 fixture |
| R-2 | **관계 상태** — `confirmed / structural / partial / unresolved / deferred` | 동 §2. 사람이 선언·검증한 값이며 자동 산출값이 아니다 |
| R-3 | **비활성 관계** — 선언은 하되 Query Plan에 자동 삽입되지 않는 상태 | 동 §14.1 규칙 5 (CEI) |
| R-4 | **대칭 endpoint 탐색** — 동일 논리 관계가 `NODE1` 또는 `NODE2` 어느 쪽에도 성립 | 동 §9.3 |
| R-5 | **조건부 Join** — endpoint type이 특정 값 집합일 때만 Join 허용 | 동 §9.4 (VLAN endpoint를 Master에 Join 금지) |
| R-6 | **문맥 전파 관계** — JOIN이 아니라 필터 문맥을 후속 Query에 전달하는 관계 유형 | 동 §7.1, §14.3 (중복 집계 방지) |
| R-7 | **출처** — `fk` / `declared` (ADR-009 기존 요구) | ADR-009 |

**R-6은 특히 중요하다.** KPI Fact와 Cause/Link Detail은 1:1 FK 관계가 아니며, 무조건 JOIN하면
1:N 확장으로 Metric이 중복된다. 따라서 `relationship` tool(TOOL-002)은 **"JOIN 가능한 관계"와
"문맥을 전달할 관계"를 구분해서 반환**해야 하며, Agent는 후자에 대해 JOIN이 아니라 후속 Query를 생성한다.

`status`가 `confirmed`가 아닌 관계를 사용할 때는 그 사실을 응답에 표시한다(ADR-020의 해석 공개 원칙과 동일 취지).

### 근거

R-2는 ADR-020이 기각한 "자동 confidence"와 성격이 다르다. **사람이 실데이터로 검증하고 근거와 함께
선언한 상태**이며, 관계 fixture와 계약 테스트로 검증된다. 검증 가능하고 감사 가능하다.

R-6이 없으면 대표 시나리오의 Cause drill-down에서 Metric 중복이 발생하는데, 이는 조용한 오류이므로
사후 발견이 어렵다. 구조로 막는 것이 옳다.

### 재검토

S1에서 실제 YAML 스키마와 TOOL-002 응답 스키마를 확정한다. Tool Schema 동결(S0) 이후이므로
**TOOL-002의 응답 필드 추가가 필요한지 S0 내에 판단해야 한다.**

---

## 개정된 결정 (2026-08-10)

기존 ADR의 본문은 보존하고, 재검토 결과를 아래에 개정 절로 덧붙인다.
**충돌 시 개정 절이 우선한다.**

---

### ADR-009 개정 — Semantic Source of Truth 재확인

**대상**: ADR-009 (Semantic Store 형식 및 Domain Pack) · **상태**: `ACCEPTED` 유지

#### 재검토 배경

`SEMANTIC_ARCHITECTURE.md` §15가 다음을 제안했다.

> Semantic Source of Truth는 Catalog가 담당한다. `semantic.yaml`은 교환/배포/백업 포맷으로 사용한다.

#### 판정: **기각. ADR-009의 결정을 유지한다.**

Domain Pack의 가치는 내용이 아니라 **형식**에 있다. 런타임 가변 저장소로 옮기면 다음이 소실된다.

| Domain Pack 속성 | Catalog(런타임 상태)로 이전 시 |
|---|---|
| Git 버전 관리 · diff 리뷰 | 소실 |
| **CI 도메인 용어 검사** | **소실 — ADR-009/019가 명시한 유일한 방어선** |
| 폐쇄망 반입 산출물 (ADR-014 #3) | 백업·마이그레이션 대상으로 전환 |
| "다음 고객사엔 팩만 새로 작성" (FR-043) | 붕괴 |
| 롤백 | 소실 |

추가로, 제안은 Catalog를 Source of Truth로 삼으면서 **충돌 해소 규칙은 후순위로 연기**한다.
진실의 원천을 지정하면서 충돌 semantics를 나중에 정하겠다는 것은 순서가 뒤집힌 것이다.

실무적 이유도 있다. 제안된 Semantic Catalog는 테이블 7종을 두며 그중 `semantic_feedback` 등은
런타임 쓰기가 발생한다. 즉 **배포 구성요소가 하나 늘어난다.** ADR-008이 "외부 저장소는 폐쇄망 배포
구성요소를 늘린다"는 이유로 회피한 것과 같은 문제이며, 운영 인력이 없는 고객사 환경에서 백업·복구·
마이그레이션 대상이 늘어나는 것은 실질적 리스크다.

#### 유지되는 구조

```
Domain Pack (Git, Source of Truth)
      │ 로드
      ▼
Semantic Index (파생, 재생성 가능, 인메모리 — 신규 저장소 아님)
      ▲
      │ 승인된 diff
Candidate Store (학습 후보, Query 생성에 사용하지 않음)
      ▲
      │ 확인된 매핑만
Clarify-Confirm-Log 루프 (ADR-020)
```

Semantic Catalog라는 명칭을 사용할 경우 **Domain Pack에서 파생되는 인메모리 인덱스**를 의미하며,
독립된 영속 저장소를 의미하지 않는다.

#### 함께 갱신되는 사항

- `semantic.yaml`의 대규모 alias 나열은 ADR-021(표기 정규화)로 상당 부분 불필요해진다.
  Pack에는 **정식 명칭과 필요한 최소 alias만** 등록한다.
- `semantic.yaml`에 `enrich-context` 계열 필드를 추가 검토한다:
  enum 의미, 단위, NULL 의미, magic value, soft delete 규칙, timezone, cross-system identifier.
  이는 **스키마만으로 알 수 없고 사람이 아는 정보**이므로 Pack에 두는 것이 옳다.
- `relationships.yaml` 스키마는 ADR-027을 따른다.

---

### ADR-010 개정 — Vector / RAG 결정의 분할

**대상**: ADR-010 (Vector DB / RAG) · **상태**: `DEFERRED` 유지, 결정을 3개로 분할

#### 재검토 배경

제안 문서는 Schema Memory / Query Memory / Vector Retrieval을 PoC에 도입할 것을 제안했다
(`SEMANTIC_ARCHITECTURE.md` §32는 "PoC에서 최소한 Phase 1~3"을 명시).

또한 ADR-010이 "Vector DB / RAG"를 **하나의 결정으로 묶어 둔 것**은 부분 도입 논의를 불가능하게
만든다는 지적이 있었다. 이 지적은 타당하다.

#### 판정: **`DEFERRED` 유지. 단, 결정을 3개로 분할한다.**

| 분할 결정 | 상태 | 재평가 게이트 |
|---|---|---|
| **ADR-010a — Schema Retrieval (벡터 스키마 검색)** | `DEFERRED` (PoC 도입 검토 대상 아님) | 대상 테이블 수가 수백 규모로 증가할 때 |
| **ADR-010b — Query Memory Retrieval (유사 질의 검색)** | `DEFERRED` | Interaction Log(ADR-024) 3~6개월 축적 후, 패러프레이즈 빈도 실측 |
| **ADR-010c — 문서 RAG (운영 매뉴얼·KPI 설명)** | `DEFERRED` | 기존 계획대로 PoC 이후 (SAD 24 우선순위 3) |

#### ADR-010a를 PoC에서 제외하는 근거 (신규)

초기 검증 범위의 소규모 schema profile 메타데이터는 ADR-017의 스키마 컨텍스트 예산
2,500 토큰 안에 충분히 들어간다.
**벡터 검색으로 후보를 좁힐 대상 자체가 없다.** 액션 아이템 #4(테이블 수)는 이로써 해소된다.

부수적 근거로, 컬럼명은 `SUCCESS_CNT` 같은 **식별자이지 산문이 아니므로** 임베딩이 잘 다루는 입력이
아니다. 명칭 매칭에는 정규화(ADR-021) + 역색인이 더 정확하고 디버깅이 쉽다.

#### ADR-010b를 PoC에서 제외하는 근거

PoC 종료 시점에 축적된 질의 이력이 사실상 0건이므로 검색 대상이 없다. 구현 비용만 발생하고 효용이 없다.
**대신 기록은 ADR-024로 PoC에서 수행한다.**

#### 공통 근거 — 컨텍스트 및 지연 예산

제안된 Semantic Retrieval Pipeline을 ADR-017 예산에 대입하면 Semantic 컨텍스트 배분(1,000 토큰)을
초과한다. Query Memory 상위 3건만으로 항목당 150~300 토큰씩 450~900 토큰을 소비하며,
General Lexicon 후보와 Catalog 정의를 더하면 850~1,650 토큰이 된다.

ADR-001의 대안(B)인 26B/8K가 확정되면 예산이 약 절반이 되므로 **구조적으로 실행 불가능**하다.

지연도 문제다. 제안 파이프라인은 Spec 생성 이전에 독립적인 LLM Resolution 호출을 추가하여
턴당 LLM 호출이 3회에서 4회로 늘어난다. A2 환경에서 약 33% 지연 증가이며 NFR-001(P50 30초)을 위협한다.

#### 유지되는 사항

- `SemanticProvider` 인터페이스와 4개 시그니처(`resolve_term` / `find_entity` / `find_metric` /
  `find_relationship`)를 **동결 유지**한다. S0 exit criterion인 Tool Schema 동결과 일관된다.
- 확장은 인터페이스 추가가 아니라 **반환 타입에 `source`와 `status` 필드를 추가**하는 방식으로 한다
  (ADR-020의 `declared` / `candidate` 구분). `get_relationship_candidate()` 같은 신규 시그니처는
  미검증 관계를 fail-closed 경로에 노출하므로 두지 않는다.
- 임베딩 도입 시 반입 산출물(ADR-014)에 모델 가중치가 추가되고 NFR-012(설치 2시간)에 영향이 있다는 점을
  재평가 시 비용에 포함한다.

#### 원칙 추가 — Vector Retrieval은 Truth가 아니다

향후 어떤 형태로든 벡터 검색을 도입할 경우 다음을 지킨다. P-6의 확장이다.

```
Vector Similarity → Candidate → 구조화 검증 → QuerySpec → QueryForge Validation
```

벡터 검색 결과는 "어디를 찾아볼 것인가"를 정하는 보조 수단이며,
"실제 스키마·관계가 무엇인가"는 TOOL-001 / TOOL-002로 확인한다.

---

## ADR-028 — 제품 경계, Capability 분리 및 실행 품질 기준

**상태**: `ACCEPTED` (v1 Read), Future Mutation 상세 계약은 `DEFERRED`

### 맥락

QueryForge는 DataLens PoC의 내부 부속이 아니라 독립 설치해 Claude, GPT 등 표준 MCP Client에서도
실사용하는 제품성 범용 MySQL Data MCP Server다. 따라서 transport 연결 상태나 특정 Client의 Agent
구현을 신뢰 경계로 삼을 수 없으며, pool·lifecycle·type mapping·serialization·concurrency·security는
PoC 부가 기능이 아니라 Core 품질이다.

기존 `QuerySpec → Planner → SQL Generator → sqlglot AST Validator → Adapter → Dataset → Polars`,
Catalog Snapshot, Dataset model은 유지한다.

### 결정

1. **Capability 분리**: v1/PoC의 활성 Capability는 Read뿐이다. 영구 Read-only 전제는 제거한다.
   Future controlled Mutation은 INSERT/UPDATE/DELETE만 허용 가능하며 TRUNCATE와 DDL은 제외한다.
2. **Mutation 계약 유보**: Future 흐름은 `Preview → Approval → Revalidation → Execute`를 QueryForge가
   보장한다. 그러나 MutationSpec JSON Schema, MutationPlan 상세 필드, `mutate` 등 Tool 이름은 확정하지 않는다.
3. **실행 경계**: Read와 Future Mutation은 Executor/Adapter, DB credential, Policy, transaction,
   retry classification을 별도로 구성할 수 있어야 한다. Read allowlist를 완화해 Mutation을 수용하지 않는다.
4. **Session 분리**: MCP transport session은 protocol 계층에 한정한다. Dataset과 future MutationPlan은 MCP 연결이 아니라
   명시적 QueryForge Application Session이 소유한다.
5. **Policy Enforcement**: 인증과 별개로 Capability, schema/table/column, partition, row/value/response size,
   approval을 검증하며 Planning 전과 실행 직전에 fail-closed로 적용한다.
6. **데이터 경계**: MySQL driver value → QueryForge logical type → Polars dtype → MCP JSON 변환 책임을
   명시한다. text/binary/JSON 개별 값과 전체 response에 byte/depth 상한을 둔다.
7. **DB lifecycle**: Read는 명시적 read-only transaction에서 실행한다. pool 반환 전 rollback/cursor/session
   reset을 수행하고 실패 연결은 폐기한다. retry는 실행 전 일시 오류와 실행 후 불명확 오류를 분류하며,
   timeout/cancel 및 결과 불명확 실행은 자동 재시도하지 않는다. 종료 시 stop-accept → drain → cancel → close.
8. **결과·관계 정합성**: 외부 limit N은 내부 `limit+1`로 초과를 탐지한다. 복합 FK는 한 관계로 보존하고
   incoming/outgoing 방향과 cardinality를 Catalog/Relationship 계약에 포함한다.
9. **Tool 정책**: 현재 5 Tool은 v1에서 유지하되 영구 불변조건으로 두지 않는다. 신규 Tool은 ADR 변경과
   semver/Client/security 영향 검토를 요한다.
10. **검증**: concurrent MCP, security, property, 실제 MySQL lifecycle/graceful-shutdown 시험을 CI 필수축으로 둔다.

### 영향

- DataLens 전용 가정이 제거되어 Client별 특수 처리 없이 동일 Application/Policy 경계를 사용한다.
- v1 외부 Tool 계약과 Read 실행 흐름은 유지된다.
- Future Mutation은 확장 가능하지만 상세 ADR 전에는 config나 숨은 endpoint로 활성화할 수 없다.
- Adapter, 설정, 오류 모델, 테스트 fixture에 type mapping·reset·retry·size-limit 계약이 추가된다.

---

## ADR-029~032 — 2026-08-21 통합 결정

**상태**: `ACCEPTED`

### ADR-029 — Dataset 수명과 Application Session

QueryForge Application Session은 내부 `session_id` 기반 접근 namespace다. MCP Transport Session 및
DataLens Application Session과 독립되고 타 session 접근은 존재 여부를 감춰 거부한다. Phase 0에서는
두 Application Session을 DataLens 내부에서 1:1 mapping하되 외부에는 DataLens Session ID만 노출한다.
Dataset retention은 TTL·last-access·lineage·disk-pressure 정책으로 결정하며 release는 cleanup hint다.
즉 **session-scoped access + store-managed lifecycle**이다.

### ADR-030 — MySQL Type Mapping과 Preview Serialization

`MySQLTypeMapper`(driver value → internal logical type)와 `MCPSerializer`(internal value → MCP JSON)를 분리한다. signedness, DECIMAL precision/scale, BIT/BOOLEAN, text/binary, date/time/timestamp, YEAR, ENUM/SET, JSON, NULL, zero-date를 명시적으로 매핑한다. Preview는 text/binary/JSON/value/row/column/response 한도를 적용하며 절단·생략은 machine-readable `warnings[]`로 공개한다. 원본 Dataset은 변경하지 않는다.

### ADR-031 — DB Capability Detection과 Graceful Shutdown

설치/startup에서 MySQL version, read-only transaction, EXPLAIN, partition metadata, MAX_EXECUTION_TIME, charset/collation, information_schema access, server-side cursor/cancel을 탐지한다. 필수 capability 부재는 기동 실패, 선택 capability 부재는 degraded 상태다. lazy DB startup은 도입하지 않는다. 종료는 stop-accept → drain(deadline) → cancel → cursor/transaction 정리 → Dataset publish 완료/abort → SQLite checkpoint/close → pool close 순서다.

### ADR-032 — Retry와 Query Cost Guard

자동 retry는 SELECT 계열의 분류 가능한 transient error에만 제한적으로 적용한다. timeout/cancel, 결과 수신 시작 후 오류, 결과 불명확 실행은 재시도하지 않으며 mutation 자동 retry는 금지한다.

Cost Guard는 hard safety와 advisory cost를 분리한다. 파티션 키 누락·허용 범위/명시적 hard limit 위반은 `BLOCKED`다. full scan, filesort, 큰 estimated rows 자체는 자동 차단하지 않는다. EXPLAIN/catalog statistics는 partition safety와 diagnostics에 사용한다. 설정 가능한 n rows 또는 n partitions threshold를 넘으면 `CONFIRMATION_REQUIRED`, 아니면 `NORMAL`이다. 확인은 query digest와 policy snapshot에 바인딩된 short-lived single-use token 방향으로 확장하며 상세 수치는 config에 둔다.

---

## ADR-017 개정 — MCP 응답 바이트 상한의 재정의

**대상**: ADR-017 (컨텍스트 토큰 예산) · **상태**: `PROVISIONAL` 유지, `mcp.max_response_bytes`/Preview 관련 결정만 정정

### 재검토 배경

Preview 상한을 요청 단위 파라미터(`preview_rows`)로 열고 HTTP bulk 경로(`/data/datasets/{id}/rows`)의
페이지 크기 상한을 완화하는 작업을 진행하면서, `mcp.max_response_bytes`(32,768)는 그대로 두었다.
이후 재검토에서, 이 값이 애초부터 **특정 소비자(DataLens가 구동하는 A2 15.3GB 로컬 모델, 8K~16K 컨텍스트)의
컨텍스트 예산을 보호하기 위해 QueryForge 서버 자체에 심어진 상한**이라는 점이 문제로 지적되었다.
QueryForge는 ADR-003·ADR-028이 이미 "Claude, GPT 등 표준 MCP Client에서도 동일한 안전 경계로 사용하는
범용 제품"으로 규정한 서버인데, 그 경계를 특정 Client의 모델 용량 기준으로 잡아둔 것은 ADR-003·028의
원칙과 어긋난다.

### 판정

`mcp.max_response_bytes`의 목적을 **"응답을 소비하는 Agent의 컨텍스트 보호"에서 "서버 자신의 자원 보호
(서킷브레이커)"로 재정의한다.** 값 자체도 이에 맞춰 상향한다.

- ADR-017의 원래 배분표("Dataset 메타 + preview: 1,500 토큰")는 **DataLens 자신이 QueryForge를 호출할 때
스스로 지켜야 하는 예산**으로 재해석한다. DataLens는 이미 `preview_rows` 요청 파라미터(001~005)로 이
예산을 스스로 관리할 수 있다(예: 주로 `preview_rows=5` 근처로 요청). **QueryForge 서버가 이 예산을
대신 강제하지 않는다.**
- `mcp.max_response_bytes` 기본값을 32,768 → **10,485,760(10MB)**로 상향한다. 이 값은 Agent 컨텍스트
크기와 무관하며, 순수하게 "단일 MCP 응답 하나가 서버 프로세스 메모리·네트워크를 위협하지 않는
상한"이다. 정확한 값은 실측이 아니라 잠정치이며, 운영 중 조정 가능한 config 값이다.
- HTTP bulk 경로(`/data/datasets/{id}/rows`)는 애초부터 이런 상한이 없었다(005). 이번 정정으로 MCP 경로도
같은 철학(서버는 관대하게 서빙하고, 소비자가 자기 능력에 맞게 요청 크기를 조절한다)으로 통일된다.
- Agent Loop 스텝 상한(3회)과 다른 ADR-017 항목(스키마 컨텍스트 2,500, 대화 이력 압축 등)은 이
개정의 대상이 아니다 — 이들은 DataLens 자신의 Agent Loop 설계이지 QueryForge 서버 계약이 아니라는 점이
이미 확인되었다.

### 근거

Preview 생성 비용은 서버 자원(Polars slicing, JSON 직렬화) 관점에서 저렴하다 — 늘어나는 자원 비용이
거의 없는데 상한만 낮게 잡아 둘 이유가 없다. 진짜 비용은 그 응답을 "읽는" 쪽에서 발생하며, 그건
QueryForge가 통제할 수도 없고 통제해서도 안 되는 영역이다(Claude/GPT 같은 대형 컨텍스트 Client에게는
32KB가 오히려 불필요하게 인색한 제약이었다).

### 함께 갱신되는 사항

`preview.max_cell_bytes`(1,024) < `mcp.max_response_bytes` 교차 검증(코드)은 여전히 성립한다.
`tests/contract/test_s1_contract.py`의 고정값 assert도 새 값으로 갱신한다.

---

## 현재 재검토 항목

| 항목 | 상태 |
|---|---|
| MySQL 외 Database Adapter | v1 범위 밖. Adapter 계약을 유지하고 별도 구현 시 재검토 |
| Transform process isolation | 인터페이스만 존재하며 thread isolation이 기본 |
| 공유/object Dataset Store와 다중 worker | v1 범위 밖. 현재 workers=1 고정 |
| 외부 Semantic Provider | 선택 확장. Core는 Catalog와 구조화 계약만으로 동작 |
| Controlled Mutation | 미구현·비노출. 별도 ADR, credential, policy, approval 계약 없이는 추가하지 않음 |

---

## 개정 이력

| 버전 | 일자 | 내용 |
|---|---|---|
| 0.1 | 2026-08-10 | 초안. ADR-001~019 등록 |
| 0.2 | 2026-08-10 | ADR-005 확정 (MySQL 8.x). 액션 아이템 #1 해소 |
| 0.3 | 2026-08-10 | Semantic 제안 검토(SEM-REVIEW-001) 반영. ADR-020~027 신규 등록. ADR-009 개정(Domain Pack SoT 유지), ADR-010 개정(3분할 및 게이트 명시). 액션 아이템 #4 해소, #8~12 신규 등록 |
| 0.4 | 2026-08-21 | ADR-028 추가. 독립 제품·Capability/session/policy/type mapping/lifecycle/Future Mutation 경계 반영 |
| 0.5 | 2026-08-21 | ADR-006 개정, ADR-029~032 통합. 영속 DatasetStore, lifecycle 분리, serialization, capability detection, shutdown, retry, Query Cost Guard 반영 |
| 0.6 | 2026-08-26 | 공식 MCP SDK session, 독립 versioned image 배포, 현재 구현·재검토 항목 반영 |
| 0.7 | 2026-08-30 | ADR-017 개정 — mcp.max_response_bytes 재정의(Agent 컨텍스트 보호 → 서버 자원 보호), 기본값 32,768 → 10,485,760 |
| 0.8 | 2026-09-08 | Phase 0 consistency gate. DataLens의 QueryForge wheel runtime 의존 제거, 현재 Dataset/Preview 계약 반영, 세 Session 및 Agent/deadline 소유권 명확화 |
