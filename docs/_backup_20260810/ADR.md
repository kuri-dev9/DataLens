# DataLens / QueryForge — Architecture Decision Records

| 항목 | 내용 |
|---|---|
| 문서 ID | ADR-INDEX |
| 버전 | 0.2 |
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

## 확인 필요 액션 아이템 요약

| # | 항목 | 관련 ADR | 기한 | 영향 |
|---|---|---|---|---|
| 1 | ~~MySQL 버전~~ **해소: 8.x 확정 (2026-08-10)** | ADR-005 | 완료 | 5.7 분기 불필요, 윈도우 함수 가용 |
| 2 | FK 제약 설정 여부 | ADR-005, 009 | S1 착수 전 | relationships.yaml 필수 여부 |
| 3 | 일자 경계 / 적재 타임존 | ADR-015 | S1 착수 전 | "어제" 해석이 하루 틀어질 수 있음 |
| 4 | 테이블 수 / 최대 테이블 행 수 | ADR-006, 017 | S1 중 | Dataset 상한, 스키마 컨텍스트 전략 |
| 5 | 읽기 전용 계정 발급 가능 여부 | ADR-011, 013 | S1 착수 전 | 보안 설계 |
| 6 | 사용자별 접근 제어 요구 여부 | ADR-011 | S3 | 정책 계층 도입 여부 |
| 7 | GPU 상향(L4 등) 가능성 | ADR-001 | 납품 사양 협의 시 | 모델 크기 상한 |

---

## 개정 이력

| 버전 | 일자 | 내용 |
|---|---|---|
| 0.1 | 2026-08-10 | 초안. ADR-001~019 등록 |
| 0.2 | 2026-08-10 | ADR-005 확정 (MySQL 8.x). 액션 아이템 #1 해소 |