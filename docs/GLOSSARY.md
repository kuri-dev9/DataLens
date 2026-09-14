# QueryForge — 용어집 및 ID 체계

| 항목 | 내용 |
|---|---|
| 문서 ID | GLOSSARY |
| 버전 | 0.7 |
| 최종 수정 | 2026-09-08 |
| 적용 범위 | QueryForge와 표준 MCP Consumer 연동 |

이 문서는 QueryForge 설계 문서와 코드 식별자에서 사용하는 용어의 정본이다. 상위 애플리케이션 이름은
통합 예시일 뿐 QueryForge의 런타임 의존성을 뜻하지 않는다.

---

## 1. 제품 / 프로젝트 명칭

| 용어 | 정의 |
|---|---|
| **DataLens** | 전체 시스템의 제품명. REST API Server + LLM Agent + MCP Client + 세션/컨텍스트 관리를 포함하는 상위 애플리케이션 |
| **QueryForge** | 독립 설치 가능한 제품성 범용 MySQL Data MCP Server. DataLens는 주요 Consumer 중 하나이며 Claude, GPT 등 표준 MCP Client에서도 동일한 안전 경계로 사용한다. 도메인 지식을 갖지 않는다 |
| **Domain Pack** | 특정 배포 대상의 도메인 지식을 담은 버전 관리 아티팩트. alias, 관계 보강, 파티션 선언, 로케일별 표기, few-shot 예시를 포함한다. 코드가 아니다 |

---

## 2. 아키텍처 용어

| 용어 | 정의 |
|---|---|
| **Agent** | 사용자 의도를 해석하고 "무엇을 할지" 판단하는 주체. LLM + 제어 루프의 결합 |
| **Agent Loop** | DataLens Agent가 Tool을 호출하고 결과를 확인해 다음 행동을 결정하는 반복 구조. Phase 0 기본 Tool 호출 상한은 설정값 3회이며 QueryForge 제품의 제약이 아니다 |
| **MCP Tool** | Agent가 호출 가능한 표준화된 기능 단위. v1 활성 Tool은 5개이며 영구 불변조건은 아니다 |
| **MCP Transport Session** | Streamable HTTP 연결·요청 전달을 위한 transport 상태. 재연결될 수 있으며 Dataset의 소유권 기준이 아니다 |
| **DataLens Application Session** | DataLens가 외부 `POST /v1/sessions`로 발급하는 conversation/application 수명과 소유권 단위. 외부 API의 `session_id`는 이것만 의미한다 |
| **QueryForge Application Session** | QueryForge가 내부 `session_id`로 식별하는 Dataset 접근 namespace. MCP 연결·DataLens conversation과 독립되며 Dataset 격리 기준이지만 retention을 결정하지 않는다. Phase 0에서는 DataLens Session과 내부 1:1 mapping한다 |
| **QueryForgeClient** | DataLens 내부에서 공식 MCP SDK로 QueryForge Streamable HTTP 연결, capability 확인, Tool 호출, QueryForge Application Session mapping 및 오류 정규화를 담당하는 adapter 경계 |
| **Conversation Lifecycle** | Client 대화의 수명. Dataset lifecycle과 독립이며 대화 종료는 Dataset 즉시 삭제를 뜻하지 않는다 |
| **Dataset Lifecycle** | DatasetStore가 TTL·last access·lineage·retention·disk pressure로 관리하는 생성, publish, cache, cleanup, expiry의 수명 |
| **Capability** | QueryForge가 정책·credential·실행 경계를 갖추어 활성화하는 기능 집합. v1은 Read Capability만 활성화한다 |
| **Read Capability** | QuerySpec 기반 SELECT/EXPLAIN/Catalog 조회와 Dataset 생성. v1/PoC의 유일한 활성 DB 실행 Capability |
| **Controlled Mutation** | Future Capability. INSERT/UPDATE/DELETE를 Preview → Approval → Revalidation → Execute 경계로 수행한다. TRUNCATE와 DDL은 포함하지 않는다 |
| **MutationPlan** | Future controlled Mutation의 검증·승인·실행 정보를 담는 application-session 소유 객체. 상세 스키마와 외부 Tool 계약은 미확정 |
| **Policy Enforcement** | Client·생성 주체와 무관하게 허용 대상, 행/크기/시간 한도, Capability, 승인 상태를 실행 직전에 강제하는 application 경계 |
| **Tool Call** | Agent가 특정 Tool을 인자와 함께 호출하는 1회 행위 |
| **Query Specification** | Agent가 생성하는 구조화된 조회 명세. SQL 문자열이 아니다 |
| **Transform Specification** | 기존 Dataset에 적용할 연산 파이프라인 명세 |
| **Query Plan** | Query Specification을 검증·정규화하여 실행 가능한 형태로 변환한 중간 표현 |
| **Semantic Layer** | 업무 개념과 실제 DB 구조를 연결하는 지식 계층. 자연어를 JSON으로 변환하는 계층이 **아니다** |
| **Semantic Provider** | Semantic Layer의 구현 교체를 위한 인터페이스. 시그니처 4개 동결 (ADR-010) |
| **Catalog Snapshot** | DB 메타데이터를 사전 수집해 로컬에 보관한 사본. 매 호출 시 DB를 조회하지 않기 위한 것 |
| **Locale Profile** | 국가 코드로부터 파생되는 언어·타임존·표기 형식의 묶음 |

### 2.1 Semantic 관련 용어 (ADR-020 ~ ADR-027)

혼동 시 설계 원칙 위반으로 직결되므로 정의를 고정한다.

| 용어 | 정의 |
|---|---|
| **Semantic Resolution** | 사용자 표현을 Semantic 개념 후보로 해석하는 **처리**. Semantic Layer(지식 계층)와 혼동하지 않는다 |
| **Declared Semantic** | Domain Pack에 사람이 작성·리뷰·커밋한 의미 정의. **Query 생성에 직접 사용할 수 있는 유일한 상태** |
| **Semantic Candidate** | 추론·대화·프로파일링으로 얻은 의미 후보. Query 생성에 직접 사용하지 않으며, 사용 시 응답에 공개한다. 승격은 사람 승인으로만 |
| **Candidate Store** | Semantic Candidate를 모아 두는 저장소. Domain Pack diff 제안의 원천이며 **Semantic Truth가 아니다** |
| **Semantic Index** | Domain Pack에서 파생되는 재생성 가능한 인메모리 조회 구조. 독립된 영속 저장소가 아니다 |
| **Surface Form Normalization** | 표기 정규화. NFKC·공백 제거·비율/개수 접미사·조사 제거로 표현 변형을 정규형으로 수렴시키는 결정적 처리 (ADR-021) |
| **Clarify-Confirm-Log** | 미해소 용어를 추측하지 않고 후보 제시 → 사용자 확인 → 세션 반영 + 후보 기록으로 처리하는 루프 (ADR-020) |
| **해석 공개 (Disclosure)** | Pack 미선언 용어를 해석했거나 `confirmed`가 아닌 관계를 사용했을 때 응답에 근거를 명시하는 의무 (ADR-020) |
| **Interaction Log** | 발화·정규화형·해소 결과·QuerySpec·실행 메타를 남기는 구조화 로그. Golden set 확장과 표현 분포 관측의 원천 (ADR-024) |
| **Identifier Column** | PK·FK·relationship key 또는 `*_ID`/`*_KEY`/`*_CODE`/`*_NO` 패턴 컬럼. 숫자형이어도 수치 집계 대상이 아니다 (ADR-022) |
| **Relationship Status** | 관계 선언의 검증 상태. `confirmed` / `structural` / `partial` / `unresolved` / `deferred`. **사람이 실데이터로 검증해 선언한 값이며 자동 산출 confidence가 아니다** (ADR-027) |
| **문맥 전파 관계** | JOIN이 아니라 필터 문맥을 후속 Query에 전달하는 관계 유형. 1:N 확장으로 인한 Metric 중복을 구조로 방지한다 (ADR-027 R-6) |

---

## 3. 데이터 용어

| 용어 | 정의 |
|---|---|
| **Dataset** | QueryForge가 소유하는 조회/변환 결과의 논리 단위. Parquet data와 SQLite metadata·lineage가 영속 기준이고 memory는 hot cache다. DataLens는 opaque ID와 참조용 metadata만 보유한다 |
| **dataset_id** | 재기동·cleanup 뒤에도 충돌하거나 재사용되지 않는 opaque Dataset 식별자 |
| **Dataset Lineage** | Dataset 간 parent-child 관계. 어떤 Dataset에서 어떤 연산으로 파생되었는지의 기록 |
| **Preview** | MCP Response에 포함되는 Dataset 표본. 요청 단위 파라미터(`preview_rows`)로 크기를 지정하며(생략 시 기본값 5행), `mcp.max_response_bytes`(서버 자원 보호용 서킷브레이커, 기본 10MB) 안에서 자동으로 줄어들 수 있다. 이 예산은 Agent 컨텍스트 보호가 아니라 서버 자체 보호가 목적이며(ADR-017 개정), 소비자(DataLens 등)가 자신의 컨텍스트 예산을 `preview_rows` 요청값으로 스스로 관리한다 |
| **Summary** | Dataset의 요약 통계. 행 수, 컬럼 목록, 타입, 선택적 기초 통계 |
| **Partition Key** | 대상 테이블이 분할된 기준 컬럼. 통상 시간 컬럼이며 조회 시 범위 조건이 강제된다 |
| **Active Period** | 세션이 유지하는 현재 조회 기간. 사용자가 명시하지 않으면 기본값이 주입된다 |
| **Active Dataset** | 세션이 유지하는 현재 참조 대상 Dataset. "그중", "이것들" 등의 지시 대상 |
| **Reference Resolution** | "그중", "첫 번째", "아까 C" 같은 지시 표현을 실제 Dataset/엔티티로 확정하는 처리 |
| **Type Mapping** | MySQL 드라이버 값을 QueryForge 논리 타입으로 정규화하고 Dataset/Polars/MCP 표현으로 안전하게 직렬화하는 명시적 변환 계약 |
| **MySQL Type Mapping** | MySQL driver 값을 QueryForge internal logical type으로 정규화하는 계약. MCP 표현과 분리된다 |
| **Preview Serialization Policy** | internal Dataset 값을 MCP preview로 표현할 때 text/binary/JSON/value/response 한도와 warning을 정하는 계약 |
| **Hot Cache / Cold Storage** | 최근 Dataset의 memory cache / Parquet data와 SQLite metadata·lineage로 구성된 영속 local store |
| **Atomic Publish** | data와 metadata가 모두 완성된 뒤에만 Dataset이 조회 가능해지는 commit 절차 |
| **Query Cost Guard** | hard safety와 비용 경고를 분리해 `NORMAL`/`CONFIRMATION_REQUIRED`/`BLOCKED`를 판정하는 경계 |
| **Confirmation Token** | query digest와 policy snapshot에 바인딩된 짧은 수명의 일회성 실행 확인 토큰 |

---

## 4. ID 체계

문서·코드·테스트·에이전트 지시문에서 동일 ID를 사용한다.

| 접두어 | 대상 | 예시 | 정의 위치 |
|---|---|---|---|
| `FR-` | 기능 요구사항 | `FR-012` | 연동 시스템 요구사항 |
| `NFR-` | 비기능 요구사항 | `NFR-004` | 연동 시스템 요구사항 |
| `UC-` | Use Case | `UC-003` | 연동 시스템 요구사항 |
| `ADR-` | 아키텍처 결정 | `ADR-016` | ADR.md |
| `CMP-` | 컴포넌트 | `CMP-QF-PLANNER` | 각 문서 |
| `TOOL-` | MCP Tool | `TOOL-003` | QueryForge-DDD |
| `P-` | 설계 원칙 | `P-11` | ADR / DDD |
| `N-` | 금지사항 | `N-15` | ADR / DDD |
| `OP-` | Transform Operation | `OP-012` | QueryForge-DDD |
| `ERR-` | 에러 코드 | `ERR-MISSING_TIME_RANGE` | QueryForge-DDD |
| `AC-` | 인수 기준 | `TOOL-003/AC-2` | 각 기능 정의에 종속 |
| `GQ-` | Golden Question | `GQ-041` | evals/golden.jsonl |

### AC 참조 규칙

인수 기준은 소속 기능 ID에 종속된다. 전역 유일 참조는 `<기능ID>/AC-<n>` 형식을 사용한다.
테스트 함수명은 `test_<기능ID소문자>_ac<n>` 규칙을 따른다.

예: `TOOL-003/AC-2` → `tests/query/test_group_by.py::test_tool003_ac2`

---

## 5. MCP Tool 목록 (v1)

v1 활성 Tool은 아래 5개로 유지한다. 이는 현재 계약의 동결 대상이지 QueryForge Architecture의
영구 불변조건이 아니다. 새로운 Tool 추가 또는 책임 재분배는 호환성·보안 경계·Client 영향을 검토하는
ADR 개정을 요한다. Future controlled Mutation의 Tool 이름과 Schema는 아직 확정하지 않는다.

| ID | 이름 | 책임 |
|---|---|---|
| `TOOL-001` | `schema` | DB 메타데이터 탐색. 전체 반환 금지, 2단계 탐색만 허용 |
| `TOOL-002` | `relationship` | 테이블 간 관계 탐색. FK 자동 수집 ∪ 선언 보강 |
| `TOOL-003` | `query` | Query Specification으로 Dataset 생성 |
| `TOOL-004` | `transform` | 기존 Dataset에 연산 파이프라인 적용 |
| `TOOL-005` | `describe` | Dataset 메타데이터 및 기초 통계 반환 |

`query_sql`과 raw WHERE 입력은 도입하지 않는다. 조회 외부 계약은 위 5개 Tool과 QuerySpec 경로다.

---

## 6. 책임 경계 (한 문장 정의)

혼동이 잦은 지점이므로 고정 문구로 유지한다.

| 컴포넌트 | 책임 | 하지 않는 것 |
|---|---|---|
| **Agent** | 무엇을 할지 판단 | 데이터를 직접 계산하지 않는다 |
| **MCP** | 사용 가능한 기능을 표준 인터페이스로 제공 | 판단 엔진이 아니다 |
| **QueryForge** | 요청받은 데이터 작업을 안전하게 실행 | 업무적 의미를 판단하지 않는다 |
| **DB / Polars** | 실제 계산 수행 | — |
| **Semantic Layer** | 업무 개념과 DB 구조를 연결 | 자연어를 파싱하지 않는다 |

**판정 예시**
- "평균 성공률을 계산한다" → QueryForge 책임 ✅
- "이 설비의 품질이 나쁘다" → QueryForge 책임 아님 ❌ (Agent / Semantic Layer)

---

## 7. Dataset 소유권 규약

혼동 시 장애로 직결되는 항목이므로 명시한다.

| 항목 | 소유 |
|---|---|
| Dataset 실제 데이터 | **QueryForge** (단일 진실 원천) |
| dataset_id, 스키마, 행 수, 요약 메타 | DataLens가 사본 보유 (참조용) |
| Dataset TTL / 축출 | **QueryForge** |
| Dataset 접근 격리 단위 | **QueryForge Application Session** (`session_id`) |
| Dataset retention·cleanup | **QueryForge DatasetStore 정책** |
| MCP 연결 수명 | MCP Client와 Transport (DataLens/QueryForge Application Session과 독립) |
| DataLens 대화 세션 수명 | **DataLens** |

DataLens는 Dataset 데이터를 보관하지 않는다. 대화/세션 종료 시 release hint를 보낼 수 있으나 즉시 삭제 계약이 아니며, 실제 회수는 QueryForge retention·cleanup·disk-limit 정책이 담당한다.

---

## 8. 금지 용어 / 표기 규칙

| 금지 | 대체 | 사유 |
|---|---|---|
| QueryForge 코드 내 배포별 업무 용어 | Domain Pack으로 이동 | CI 검사로 강제 (ADR-009) |
| "MCP가 판단한다" | "Agent가 판단한다" | 책임 경계 혼동 |
| "Semantic Layer가 자연어를 변환한다" | "Semantic Layer가 개념과 구조를 연결한다" | 정의 오용 |
| Dataset을 "결과 JSON"으로 지칭 | "Dataset" | ID 참조 모델이 흐려짐 |
| "Semantic을 자동 학습한다" | "Candidate를 기록하고 사람이 승인한다" | 자동 승격 금지 (N-15) |
| "Semantic Catalog가 Source of Truth다" | "Domain Pack이 Source of Truth이고 Index는 파생물이다" | ADR-009 개정 |
| "쿼리가 성공했으니 해석이 맞다" | "실행 성공은 의미 정확성의 증거가 아니다" | N-16. 의미 오류는 항상 에러 없이 실행됨 |
| "Vector 검색 결과를 사용한다" | "Vector 검색 결과는 후보이며 구조화 검증으로 확정한다" | P-13 |
| Relationship Status를 "confidence"로 지칭 | "status" | 사람이 검증한 상태와 자동 산출 신뢰도의 혼동 (ADR-027) |

---

## 9. 로케일 코드

| 국가 코드 | language | timezone | 비고 |
|---|---|---|---|
| `KR` | `ko` | `Asia/Seoul` | 개발 기준 |
| `JP` | `ja` | `Asia/Tokyo` | PoC 최종 타깃 |

alias 비교 시 **NFKC 정규화 + 대소문자 통일**을 적용한다 (ADR-015).

---

## 개정 이력

| 버전 | 일자 | 내용 |
|---|---|---|
| 0.1 | 2026-08-10 | 초안 |
| 0.2 | 2026-08-10 | Semantic 용어 13종 등재(2.1절), 금지 표현 5건 추가, ID 체계에 `P-`/`N-` 추가 |
| 0.3 | 2026-08-21 | 독립 제품 정의, Capability·Application Session·Policy·Type Mapping·Future Mutation 용어와 v1 Tool 정책 반영 |
| 0.4 | 2026-08-21 | Dataset lifecycle/session 분리, persistent store, atomic publish, serialization, Query Cost Guard 용어 반영 |
| 0.5 | 2026-08-26 | (번호 유지, 이전 기록 없음) |
| 0.6 | 2026-08-30 | Preview 정의 갱신 — 5행 고정 상한 → 요청 단위 `preview_rows` + 서버 자원 보호용 `mcp.max_response_bytes`(ADR-017 개정 반영) |
| 0.7 | 2026-09-08 | DataLens Application Session, QueryForge Application Session, MCP Transport Session과 QueryForgeClient 정의 분리; Dataset 저장·Agent limit 소유권 갱신 |
