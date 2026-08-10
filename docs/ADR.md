# DataLens / QueryForge — Architecture Decision Records

| 항목 | 내용 |
|---|---|
| 문서 ID | ADR-INDEX |
| 버전 | 0.3 |
| 최종 수정 | 2026-08-10 |
| 적용 범위 | DataLens, QueryForge 공통 |

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

### 마일스톤 참조

| ID | 기간 | 내용 |
|---|---|---|
| S0 | 2주 | 문서 확정 + Tool Schema 동결 + LLM 스파이크 |
| S1 | 4주 | 최소 수직 슬라이스 (schema / query / transform / describe → 단일턴 E2E) |
| S2 | 4주 | join·집계, 멀티턴 참조 해소, Dataset lineage |
| S3 | 3주 | Semantic 최소본, 오류 복구 루프, 파티션 최적화 |
| S4 | 2주 | 로케일 전환, 리소스 제한, 설치 리허설 |

---

## ADR-001 — LLM 모델 및 서빙 스택

**상태**: `PROVISIONAL` — S0 종료 시 실측으로 확정

### 맥락

가용 하드웨어는 NVIDIA **A2 15.3GB** 2장(HP 서버 2대에 각 1장), 시스템 RAM 128GB.
A2는 저전력 엣지 추론 카드로 메모리 대역폭이 약 200GB/s 수준이다. LLM 토큰 생성 속도는 대체로
메모리 대역폭에 지배되므로, 이 카드는 모델 크기와 컨텍스트 길이 양쪽에 강한 상한을 건다.

Gemma 4는 2026년 4월 공개되었고 Apache 2.0 라이선스, native function calling, 128K/256K 컨텍스트를
지원한다. 해외 고객사 납품에서 이전 세대의 커스텀 라이선스가 유발하던 법무 검토 리스크가 없다.

현재 개발 환경에서는 `gemma4:26b`(26B A4B, MoE / 활성 4B)를 사용 중이다.

### 용량 분석

| 후보 | Q4 가중치 추정 | KV 캐시 여유 | 비고 |
|---|---|---|---|
| Gemma 4 12B Unified | 약 8GB | 약 6GB | 16~32K 컨텍스트 안정 |
| Gemma 4 26B A4B (MoE) | 약 14GB | 약 1GB | 컨텍스트 8K 이하 필수. 오프로드 위험 |
| Gemma 4 E4B | 약 3GB | 충분 | 라우팅/분류 보조용 |

MoE(26B A4B)는 토큰당 활성 파라미터가 4B이므로, **VRAM에 완전히 상주하기만 하면** 대역폭 대비
생성 속도는 12B dense보다 유리할 수 있다. 반대로 상주에 실패해 CPU 오프로드가 발생하면 속도가 급락한다.
즉 이 결정은 이론이 아니라 실측 대상이다.

### 결정

- **기본안 (A)**: Gemma 4 **12B Unified**, Q4 양자화, 컨텍스트 상한 16K
- **대안 (B)**: Gemma 4 **26B A4B**, Q4 양자화, 컨텍스트 상한 8K
- S0 스파이크에서 A/B를 동일 golden set으로 비교한 뒤 확정한다.
- 서빙 스택: 1차 **Ollama**(설치 단순, 폐쇄망 이미지화 용이, JSON Schema 기반 structured output 지원).
  대안으로 **vLLM**(guided decoding, 동시성 우위)을 S0에서 병행 평가.
- 모델은 교체 가능한 부품으로 취급한다. `LLMProvider` 인터페이스(chat / tool_call / structured_output /
  count_tokens)를 두고 모델명·엔드포인트는 전부 설정값으로 외부화한다.

### 판단 기준 (S0 측정 항목)

1. Tool call 유효율 (well-formed JSON 비율)
2. 스텝당 평균 지연 (prefill / decode 분리 측정)
3. 실사용 컨텍스트에서 CPU 오프로드 발생 여부
4. golden set 단일턴 정답률

### 재검토

S0 종료 시. 또한 고객사 납품 사양 협의 시 **NVIDIA L4(24GB, 약 300GB/s, 72W)** 로의 상향을
검토 항목으로 제기한다. A2와 동일한 저전력·싱글슬롯 계열이므로 서버 개조 없이 교체 가능한 경우가 많다.

### 서버 2대 배치

GPU가 서버당 1장이므로 텐서 병렬은 적용하지 않는다.

- 서버 #1: LLM 추론 전용 (Ollama/vLLM)
- 서버 #2: API Server + QueryForge + 카탈로그 저장소. 잔여 GPU는 향후 임베딩 모델(RAG 확장)용으로 예약

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

### 탈출구

Text-to-SQL은 Core 밖의 별도 tool `query_sql`로 분리하고 **기본 비활성**(config flag)으로 둔다.
활성화 시에도 동일한 Validator와 동일한 리소스 제한을 거친다.
PoC에서는 인터페이스·flag·Validator 경로까지만 구현하고 본체는 미구현으로 남긴다.

### 재검토

S2 종료 시 golden set으로 Spec 경로와 `query_sql` 경로의 정답률·스텝 수를 비교한다.

---

## ADR-003 — MCP Transport

**상태**: `ACCEPTED`

### 결정

**Streamable HTTP**를 채택한다. stdio는 사용하지 않는다.

### 근거

QueryForge는 DataLens 전용 부속이 아니라 **재사용 가능한 독립 서버**가 목표다. stdio는 클라이언트
프로세스가 서버를 자식으로 기동하는 모델이라 수명이 종속되고, Dataset을 서버에 상주시키는 구조와
맞지 않는다. HTTP는 별도 컨테이너 배포, 독립 재기동, 다중 클라이언트, 헬스체크가 모두 자연스럽다.
docker-compose 기반 폐쇄망 배포와도 정합한다.

### 결과

- QueryForge는 독립 컨테이너로 기동한다.
- 인증은 ADR-011을 따른다.
- 네트워크 경계가 생기므로 MCP Response 크기 제한(ADR-017)이 더 중요해진다.

---

## ADR-004 — QueryForge 프로세스 모델 및 Dataset 공유

**상태**: `ACCEPTED`

### 맥락

Dataset을 프로세스 메모리에 보관하는 구조에서 멀티 워커로 기동하면, 후속 요청이 다른 워커로 라우팅될 때
`ds_001`을 찾지 못한다. 이는 발견 시점이 늦을수록 되돌리기 비싼 종류의 결함이다.

### 결정

PoC는 **단일 워커 프로세스 + in-process Dataset Manager**로 고정한다.
`--workers 1`을 기동 스크립트에 명시하고, 다중 워커 기동 시 **기동 자체를 실패**시킨다
(설정 검증 단계에서 명시적 에러).

### 확장 경로

향후 동시성이 필요하면 다음 중 하나를 택한다. 인터페이스는 `DatasetStore` 추상화로 미리 분리해 둔다.

1. Sticky session 라우팅 (세션 → 워커 고정)
2. 공유 Dataset 저장소 (Arrow IPC 파일 / Redis 등)

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

## ADR-006 — Dataset Storage

**상태**: `ACCEPTED` (PoC 범위), 확장은 `DEFERRED`

### 결정

**in-memory Polars DataFrame + TTL + LRU eviction**만 구현한다.

| 파라미터 | 초기값 | 비고 |
|---|---|---|
| Dataset TTL | 30분 | 설정 가능 |
| 세션당 최대 Dataset | 20개 | 초과 시 LRU 축출 |
| 단일 Dataset 최대 행 수 | 1,000,000 | 초과 시 `RESULT_TOO_LARGE` |
| 전체 Dataset 메모리 상한 | 8GB | 시스템 RAM 128GB 중 보수적 배분 |

### 근거

시스템 RAM이 128GB로 충분하고, PoC 동시 세션 수가 적다. Parquet spill / hybrid는 구현·검증 비용 대비
이득이 없다. 다만 `DatasetStore` 인터페이스(`put` / `get` / `evict` / `stats`)는 분리해 두어
향후 spill 구현체를 교체 삽입할 수 있게 한다.

### 확장 경로 (DEFERRED)

Arrow IPC 또는 Parquet 임시 파일 기반 spill 구현체 추가.

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
- PoC용 팩 이름은 `poc_telecom`이며, PGW/HSS/CELL 관련 지식은 **오직 이 디렉터리 안에만** 존재한다.
- **CI 검사**: QueryForge 리포 전체에서 `PGW|HSS|CELL|기지국` 문자열이 검출되면 빌드 실패.

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
- **QueryForge MCP**: 내부 네트워크 전용. 공유 시크릿 헤더 인증. 외부 노출 금지.
- **DB 계정**: 읽기 전용 계정 1개를 서비스 계정으로 사용한다. 사용자별 DB 권한 매핑은 PoC 범위 외.

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

### 근거

폐쇄망에서는 외부 Secret Manager 사용이 불가능하다. 컨테이너 주입 방식이 추가 인프라 없이
평문 저장을 회피할 수 있는 현실적 최선이다.

---

## ADR-014 — 배포 패키징

**상태**: `ACCEPTED`

### 결정

- **docker-compose** 기반 배포. 구성: `api`(DataLens), `queryforge`, `llm`(Ollama/vLLM).
- 폐쇄망 반입은 **오프라인 이미지 tar**(`docker save`) + 모델 가중치 파일 + Domain Pack.
- QueryForge는 **wheel로 빌드**하여 DataLens가 버전 핀으로 의존한다.
- 인터넷 접근을 전제로 하는 설치 절차(pip install from PyPI 등)를 런타임에 포함하지 않는다.

### 반입 산출물 목록

1. `datalens-images.tar` (전체 컨테이너 이미지)
2. `gemma4-<size>-<quant>.gguf` 또는 상응 가중치
3. `packs/<pack_name>/` (Domain Pack)
4. `docker-compose.yml` + `.env.example`
5. 설치 절차서

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

### DB 취급 원칙 (금지사항)

QueryForge는 대상 DB에 대해 다음을 **수행하지 않는다**.

- DDL (CREATE / ALTER / DROP)
- 인덱스 생성·변경·힌트 강제
- 파티션 조작
- 통계 갱신 (ANALYZE TABLE 등)
- 임시 테이블 생성
- 쓰기 권한 계정 사용

허용되는 것은 `SELECT`, `EXPLAIN`(읽기 전용), `information_schema` 조회뿐이다.

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
| 시스템 프롬프트 + Tool 정의 | 2,000 | Tool 5개 고정 |
| 스키마 컨텍스트 | 2,500 | 선택된 테이블만 |
| 대화 이력 (압축본) | 2,000 | 원문 누적 금지 |
| Dataset 메타 + preview | 1,500 | preview 5행 고정 |
| Semantic 컨텍스트 | 1,000 | alias, metric 정의 |
| 작업 여유 + 생성 | 나머지 | |

### 구조적 제약 (설계에 반영)

1. `schema` tool은 **전체 스키마 반환을 구조적으로 금지**한다.
   `list_tables` → `describe_table` 2단계 탐색만 허용한다.
2. MCP Response의 `preview`는 **5행 고정 상한**. 전체 데이터는 `dataset_id`로만 참조한다.
3. 대화 이력은 원문 누적이 아니라 **구조화 요약**(의도, 사용된 dataset_id, 결과 요약)으로 압축한다.
4. **Agent Loop 스텝 상한 3회.** 초과 시 사용자에게 명확화 질문을 반환한다.
5. Tool 개수를 5개(`schema`, `relationship`, `query`, `transform`, `describe`)로 고정한다.
   로컬 모델의 tool 선택 정확도는 tool 개수에 민감하므로 임의 증설을 금지한다.

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

기능 목록은 전부 포함하되 **구현 깊이를 단계화**한다.
예: Text-to-SQL은 tool 정의 · flag · Validator 경로까지 구현하고 본체는 `NotImplementedError` +
테스트 skip 마커로 둔다. 이후 "켜는" 작업이 아니라 "채우는" 작업만 남는다.

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
/Users/kuri/proj/vscode/QueryForge/     # 독립 semver, 독립 CI
/Users/kuri/proj/vscode/DataLens/       # QueryForge를 wheel 의존
```

- QueryForge는 wheel로 빌드되고, DataLens는 버전을 핀으로 고정해 의존한다.
- QueryForge의 `tests/fixtures/`는 DataLens의 mock 서버로 재사용한다.
- CI 검사: QueryForge 리포에서 도메인 용어 검출 시 빌드 실패 (ADR-009).

### 근거

단일 리포에서는 마감 압박 시 도메인 로직이 Core로 유입되는 것을 막을 방법이 없다.
1인 개발 체제에는 리뷰어가 없으므로 물리적 경계와 자동 검사가 유일한 방어선이다.
또한 wheel 의존 구조는 폐쇄망 배포 패키징(ADR-014)을 자연스럽게 만든다.

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
외부 자산이다. `SELECT COUNT(DISTINCT NODE_TYPE) FROM PM_EPC_KPI_1M`을 무심코 실행하면
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
 "utterance":"MME별 성공률 보여줘",
 "utterance_normalized":"MME:RATE...",
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

`DB_PoC_Relationship.md` 분석 결과, PoC 대상 관계가 단일 컬럼 조인으로 표현되지 않는다는 것이 확인되었다.

```text
PM_EPC_KPI_1M.NODE_TYPE + NODE_ID  ↔  CM_EPC_INFO.EQUIP_TYPE + EQUIP_ID
```

단일 컬럼 조인만 지원하도록 구현되면 UC-003(관계 확장 조회)이 EPC 데이터에서 동작하지 않으며,
이는 PoC 성공 판정 기준 1번의 실패를 의미한다.

### 결정

`relationships.yaml` 스키마는 최소한 다음을 표현할 수 있어야 한다.

| # | 요구 | 근거 |
|---|---|---|
| R-1 | **복합 키** — 2개 이상 컬럼 쌍을 하나의 relationship id로 선언 | `DB_PoC_Relationship.md` §5 |
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
선언한 상태**이며, `DB_PoC_Relationship.md`에서 이미 실무적으로 사용되고 있다. 검증 가능하고 감사 가능하다.

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

`DB_PoC.md` 확정 결과 **PoC Core Scope는 11개 테이블**이다.

```
PM_EPC_KPI_1M / PM_EPC_CAUSE_1M / PM_EPC_ROOT_CAUSE_1M / PM_EPC_DETACH_DETAIL_1M
PM_LINK_EPC_KPI_1M / PM_LINK_EPC_ROOT_CAUSE_1M / PM_LINK_EPC_DETACH_DETAIL_1M
CM_EPC_INFO / CL_MME / CL_SGW / CL_PGW
```

11개 테이블의 메타데이터는 ADR-017의 스키마 컨텍스트 예산 2,500 토큰 안에 충분히 들어간다.
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

## 확인 필요 액션 아이템 요약

| # | 항목 | 관련 ADR | 기한 | 영향 |
|---|---|---|---|---|
| 1 | ~~MySQL 버전~~ **해소: 8.x 확정 (2026-08-10)** | ADR-005 | 완료 | 5.7 분기 불필요, 윈도우 함수 가용 |
| 2 | FK 제약 설정 여부 | ADR-005, 009, 027 | S1 착수 전 | relationships.yaml 필수 여부 |
| 3 | 일자 경계 / 적재 타임존 | ADR-015 | S1 착수 전 | "어제" 해석이 하루 틀어질 수 있음 |
| 4 | ~~테이블 수~~ **해소: Core 11개 확정 (`DB_PoC.md`)** | ADR-010, 017 | 완료 | Schema Retrieval 불필요 판정 근거 |
| 5 | 읽기 전용 계정 발급 가능 여부 | ADR-011, 013 | S1 착수 전 | 보안 설계 |
| 6 | 사용자별 접근 제어 요구 여부 | ADR-011 | S3 | 정책 계층 도입 여부 |
| 7 | GPU 상향(L4 등) 가능성 | ADR-001 | 납품 사양 협의 시 | 모델 크기 상한 |
| 8 | **TOOL-002 응답에 관계 상태·문맥전파 유형 필드 필요 여부** | ADR-027 | **S0 내 (Tool Schema 동결 전)** | 동결 후 추가 시 개정 필요 |
| 9 | **WrenAI 참조 대상 파일별 라이선스 확인** | ADR-025 | **구현 착수 전** | 상용 납품 가능 여부 |
| 10 | **다중 장비(PGW/SGW) 실데이터 확보 가능 여부** | ADR-005, SAD 3.1 | S1 착수 전 | 현재 MME 0016 단일 노드. 엔티티 비교 시나리오 검증 범위 |
| 11 | 대상 테이블 최대 행 수 (운영 규모) | ADR-006, 017 | S1 중 | Dataset 상한. 현재 PoC 표본 140행은 운영 대표성 없음 |
| 12 | 일본어 콜레이션 / 정렬 규칙 | ADR-005, 015 | S3 | 로케일 전환 시 정렬 결과 |

---

## 개정 이력

| 버전 | 일자 | 내용 |
|---|---|---|
| 0.1 | 2026-08-10 | 초안. ADR-001~019 등록 |
| 0.2 | 2026-08-10 | ADR-005 확정 (MySQL 8.x). 액션 아이템 #1 해소 |
| 0.3 | 2026-08-10 | Semantic 제안 검토(SEM-REVIEW-001) 반영. ADR-020~027 신규 등록. ADR-009 개정(Domain Pack SoT 유지), ADR-010 개정(3분할 및 게이트 명시). 액션 아이템 #4 해소, #8~12 신규 등록 |