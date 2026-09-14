# QueryForge — 상세 설계 문서 (DDD)

| 항목 | 내용 |
|---|---|
| 문서 ID | QueryForge-DDD |
| 버전 | 0.2 |
| 최종 수정 | 2026-09-08 |
| 선행 문서 | GLOSSARY, ADR (ADR-001 ~ ADR-027), DataLens-SAD, DB.sql, DB_PoC, DB_PoC_Relationship |
| 적용 리포 | QueryForge (독립 semver, ADR-019) |
| 목적 | **Tool Schema 동결** — S0 exit criterion |

## 문서 사용법

DataLens-SAD가 **무엇을 왜 만드는가**를 정의한다면, 본 문서는 **인터페이스가 정확히 어떤 모양인가**를 정의한다.
ADR-018에 따라 본 문서는 읽기용이 아니라 **실행 명세**다. 모든 기능은 인수 기준(AC)과 한 쌍으로 기술되며,
감독자는 코드를 읽지 않고 AC 통과 여부로 진척을 판정한다.

`TOOL-` / `OP-` / `ERR-` ID의 정의 위치는 본 문서다(GLOSSARY 4장).

> **동결 대상**: 3장(공통 규약), 6~10장(Tool 입출력), 11장(ERR 코드).
> 이 범위의 변경은 ADR 개정을 요한다. 12장(OP 카탈로그)은 operation 추가가 Tool 증설이 아니므로
> 동결 대상이 아니다(SAD 9장 — "기능 추가는 새 Tool이 아니라 기존 Tool의 operation 확장").

---

## 1. 책임 경계

### 1.1 QueryForge가 하는 것

| 책임 | 설명 |
|---|---|
| 안전한 실행 | 검증되지 않은 SQL을 실행하지 않는다. AST Validator 필수 통과 (ADR-007) |
| Dataset 소유 | 조회·변환 결과의 단일 진실 원천 (GLOSSARY 7장) |
| 메타데이터 제공 | Catalog Snapshot 기반 스키마·관계 정보 |
| 계산 수행 | SQL 및 Polars 기반 집계·통계 |
| 자원 보호 | 타임아웃, 행 수 제한, 파티션 프루닝 강제 |
| 구조적 안전 규칙 | 식별자 집계 금지(ADR-022), 시간 필터 위치 강제(ADR-016 개정) |

### 1.2 QueryForge가 하지 않는 것

| 비책임 | 어디로 |
|---|---|
| 업무 의미 판단 | DataLens Agent / Semantic Layer |
| 자연어 해석 | DataLens Agent |
| 지표 이름 해석 (`success_rate` → 수식) | DataLens Agent가 전개하여 QuerySpec에 넣는다 (4.3) |
| 응답 문장 생성 | DataLens Agent |
| 오류 메시지의 로케일 렌더링 | DataLens. QueryForge는 **코드만** 반환 (ADR-015) |
| Semantic 학습·기억 | DataLens (ADR-020) |

### 1.3 도메인 중립성 (ADR-009 / N-1)

QueryForge 리포 전체에서 `PGW|HSS|CELL|기지국` 등 도메인 용어가 검출되면 CI 빌드가 실패한다.
본 문서의 예시에 등장하는 테이블·컬럼명(`PM_EPC_KPI_1M`, `NODE_TYPE` 등)은 **설명을 위한 예시이며
코드·테스트 픽스처에 하드코딩되지 않는다.** 픽스처는 중립 명칭(`FACT_A`, `DIM_B`)을 사용한다.

> 본 문서는 DataLens 리포의 `docs/`에 위치하지만 QueryForge 구현의 명세다.
> QueryForge 리포에 사본을 두거나 서브모듈로 참조한다. **두 사본이 갈라지면 안 된다.**

---

## 2. 내부 계층

```
MCP Interface  ──→ Tool Handler ──→ Validation ──→ Planning ──→ Execution ──→ Dataset Manager
                                         │              │            │
                                         │              │            ├─→ DB Adapter ─→ 대상 DB
                                         │              │            └─→ Polars Engine
                                         │              └─→ Catalog Snapshot / Domain Pack
                                         └─→ SQL AST Validator (sqlglot)
```

| ID | 컴포넌트 | 책임 |
|---|---|---|
| `CMP-QF-MCP` | MCP Interface | Streamable HTTP 종단, 인증, 요청/응답 직렬화 |
| `CMP-QF-TOOL` | Tool Handler | Tool 라우팅, 입력 스키마 검증 |
| `CMP-QF-VALID` | Validation | 11장 파이프라인 수행. 모든 거부 판단의 단일 지점 |
| `CMP-QF-PLANNER` | Query / Transform Planner | 검증된 Spec → 실행 계획. 파티션 프루닝 결정 |
| `CMP-QF-EXEC` | Execution | SQL 실행, Polars 파이프라인 실행, 타임아웃 관리 |
| `CMP-QF-DSM` | Dataset Manager | Dataset 등록·조회·축출·lineage |
| `CMP-QF-ADP` | DB Adapter | DBMS별 SQL 생성·접속. PoC는 MySQL 하나 |
| `CMP-QF-CAT` | Catalog | Catalog Snapshot 수집·보관·fingerprint |
| `CMP-QF-PACK` | Pack Reader | `partitions.yaml` / `relationships.yaml` 로드·검증 |

---

## 3. 공통 규약 (동결)

### 3.1 Transport / 인증

| 항목 | 값 | 근거 |
|---|---|---|
| Transport | Streamable HTTP | ADR-003 |
| 기동 | 독립 컨테이너, `--workers 1` 강제 | ADR-004 |
| 인증 | 공유 시크릿 헤더 `X-QueryForge-Token` | ADR-011 |
| 세션 식별 | 모든 Tool 입력에 `session_id` 필수 | ADR-011 (세션 격리) |
| 헬스체크 | `GET /health` (MCP 외부 경로) | FR-033 |
| 행 조회 | `GET /datasets/{dsid}/rows?offset&limit` (MCP 외부 경로, LLM 미경유) | FR-031, SAD 6.3 |

**멀티 워커 기동은 설정 검증 단계에서 실패시킨다**(N-13, ADR-004).

### 3.2 응답 봉투

모든 Tool 응답은 다음 두 형태 중 하나다. **예외 없다.**

```json
{
  "ok": true,
  "result": { },
  "warnings": [ { "code": "WARN-...", "detail": { } } ],
  "meta": { "duration_ms": 1830, "catalog_fingerprint": "sha256:ab12…" }
}
```

```json
{
  "ok": false,
  "error": {
    "code": "ERR-MISSING_TIME_RANGE",
    "detail": { "table": "FACT_A", "partition_key": "EVENT_TIME" },
    "hint": { "did_you_mean": ["EVENT_TIME"], "default_period_days": 1 },
    "retryable": true
  },
  "meta": { "duration_ms": 12 }
}
```

**규칙**

1. 사람이 읽는 문장은 반환하지 않는다. `code`와 구조화된 `detail`만 반환한다(ADR-015).
2. `hint`는 Agent 자가 교정을 위한 것이다(SAD 17.2). 교정 가능한 오류는 반드시 `hint`를 채운다.
3. `retryable`은 "동일 요청을 고쳐서 다시 보내면 성공할 여지가 있는가"를 뜻한다.
   자원 고갈·타임아웃은 `false`, 스펙 오류는 `true`다.
4. 부분 성공을 반환하지 않는다. 성공이면 완전한 Dataset, 실패면 Dataset 미생성이다.

### 3.3 응답 크기 제한 (ADR-017)

| 항목 | 상한 | 강제 방식 |
|---|---|---|
| `preview` 행 수 | 요청 단위 `preview_rows`, 생략 시 기본 5행 | serialization/byte budget에 따라 요청값보다 작을 수 있음 |
| `preview` 셀 문자열 | `serialization.max_text_bytes` (현재 기본 65,536 bytes) | 초과 시 절단 warning |
| `columns` 배열 | `preview.max_columns` (현재 기본 50) | 설정 기반 |
| 단일 MCP 응답 총 크기 | `mcp.max_response_bytes` (현재 기본 10,485,760 bytes) | 서버 자원 보호용 circuit breaker |
| `list_tables` 반환 | Domain Pack allowlist 범위만 | ADR-017 개정, N-22 |

`mcp.max_response_bytes`는 DataLens Agent의 컨텍스트 예산을 대신 관리하지 않는다. DataLens가
`preview_rows`로 자신의 token budget을 관리한다. **Dataset 전체 행은 어떤 Tool 응답에도 포함되지
않으며**(N-5), `dataset_id`와 별도 인증된 Dataset HTTP API로 참조한다.

### 3.4 식별자 규칙

| 대상 | 형식 | 예 |
|---|---|---|
| Dataset | `ds_` + 6자리 0패딩 일련번호 (세션 무관 전역 증가) | `ds_000001` |
| Relationship | `rel_` + snake_case | `rel_fact_a_to_dim_b` |
| QueryForge Application Session | QueryForge가 발급하는 Dataset 접근 namespace | `sess_…` |
| MCP Transport Session | 공식 MCP SDK가 관리하는 재연결 가능한 protocol session | opaque |
| Catalog fingerprint | `sha256:` + hex | `sha256:ab12…` |

### 3.5 시간·타임존 (ADR-015)

- QueryForge는 **언어를 모른다.** 타임존만 사용한다.
- 모든 시각 입출력은 ISO-8601 문자열이며 타임존 오프셋을 포함한다.
- 날짜 경계 계산은 `DATALENS_COUNTRY`에서 파생된 타임존을 따른다.
- `partitions.yaml`의 `timezone` / `day_boundary` 선언이 있으면 그것이 우선한다.

---

## 4. Domain Pack 소비 스키마

QueryForge가 읽는 Pack 파일은 **`partitions.yaml`과 `relationships.yaml` 둘뿐이다.**
`semantic.yaml`, `locale/`, `examples.jsonl`은 DataLens가 소비한다(1.2).

### 4.1 `pack.yaml` (allowlist 부분)

ADR-017 개정에 따라 조회 허용 테이블을 선언한다.

```yaml
name: poc_telecom
version: 0.1.0
target_schema_fingerprint: "sha256:…"
tables:
  - FACT_A
  - FACT_A_DETAIL
  - DIM_B
```

- 선언되지 않은 테이블은 **존재하지 않는 것으로 취급**한다.
- Catalog 갱신 시 선언되었으나 실재하지 않는 테이블은 팩 검증 오류로 보고한다.

### 4.2 `partitions.yaml`

ADR-016 및 그 개정을 그대로 구현한다.

```yaml
tables:
  FACT_A:
    partitioned: true
    partition_key: EVENT_TIME
    partition_key_type: datetime
    partition_granularity: hour          # hour | day  (액션 아이템 #13)
    secondary_time_columns: [CDATE, CHOUR, CTIME]
    default_period: { days: 1 }
    max_range: { days: 31 }
    timezone: Asia/Seoul
    day_boundary: "00:00"

  DIM_B:
    partitioned: false                    # Master. 프루닝 강제 대상 아님
    max_rows: 100000                      # 대신 행 수 상한을 둔다
```

| 필드 | 필수 | 의미 |
|---|---|---|
| `partitioned` | ✅ | `false`면 시간 범위 강제 대상에서 제외 |
| `partition_key` | `partitioned: true`일 때 | **시간 범위 조건이 걸려야 하는 유일한 컬럼** |
| `partition_granularity` | 권장 | `max_range` 기본값 산정 근거. 인덱스를 전제하지 않는다(SC-3) |
| `secondary_time_columns` | 권장 | 여기 선언된 컬럼은 **단독으로 시간 범위를 지정할 수 없다**(SC-1, N-21) |
| `default_period` | ✅ | Agent가 기간을 주지 않았을 때 주입할 기본값 |
| `max_range` | ✅ | 초과 시 `ERR-TIME_RANGE_TOO_WIDE` |

**미선언 테이블은 조회를 거부한다**(`ERR-PARTITION_POLICY_MISSING`). 선언 누락과 Master 테이블을
구분하기 위함이며, 조용히 통과시키면 SC-1 계열 사고가 재발한다.

### 4.3 `relationships.yaml` — ADR-027 구현

ADR-027의 R-1~R-7을 표현하는 스키마를 다음으로 확정한다.

```yaml
relationships:
  # R-1 복합 키 + R-2 상태 + R-7 출처
  - id: rel_fact_a_to_dim_b
    kind: join                    # join | context     ← R-6
    status: confirmed             # confirmed | structural | partial | unresolved | deferred
    source: declared              # fk | declared
    active: true                  # R-3
    from: FACT_A
    to: DIM_B
    on:                           # 복합 키. 순서 유의미
      - { left: NODE_TYPE, right: EQUIP_TYPE }
      - { left: NODE_ID,   right: EQUIP_ID }
    cardinality: many_to_one
    evidence: "실데이터 일치 확인 (표본 1건)"

  # R-4 대칭 endpoint — 동일 논리 관계의 두 분기
  - id: rel_fact_a_to_link_ep1
    kind: join
    status: structural
    source: declared
    active: true
    from: FACT_A
    to: FACT_LINK
    on:
      - { left: NODE_TYPE, right: NODE1_TYPE }
      - { left: NODE_ID,   right: NODE1_ID }
    symmetric_group: grp_fact_a_link        # 같은 그룹 = 논리적으로 하나의 관계
    cardinality: many_to_many

  - id: rel_fact_a_to_link_ep2
    kind: join
    status: confirmed
    source: declared
    active: true
    from: FACT_A
    to: FACT_LINK
    on:
      - { left: NODE_TYPE, right: NODE2_TYPE }
      - { left: NODE_ID,   right: NODE2_ID }
    symmetric_group: grp_fact_a_link
    cardinality: many_to_many

  # R-5 조건부 Join — endpoint type이 특정 값일 때만 허용
  - id: rel_link_ep_to_dim_b
    kind: join
    status: structural
    source: declared
    active: true
    from: FACT_LINK
    to: DIM_B
    on:
      - { left: NODE1_TYPE, right: EQUIP_TYPE }
      - { left: NODE1_ID,   right: EQUIP_ID }
    guard:                                   # 이 조건이 거짓이면 Join을 생성하지 않는다
      - { column: NODE1_TYPE, op: in, value: ["TYPE_X", "TYPE_Y", "TYPE_Z"] }
    cardinality: many_to_one

  # R-6 문맥 전파 — JOIN 아님
  - id: rel_fact_a_to_detail
    kind: context                            # ← JOIN 생성 금지
    status: structural
    source: declared
    active: true
    from: FACT_A
    to: FACT_A_DETAIL
    carry:                                   # 후속 Query에 전달할 필터 문맥
      - { left: NODE_TYPE, right: NODE_TYPE }
      - { left: NODE_ID,   right: NODE_ID }
      - { left: EVENT_TIME, right: EVENT_TIME, mode: time_window }
    reason: "Grain이 다르므로 JOIN 시 Metric 중복"

  # 비활성 — 선언은 하되 사용 금지
  - id: rel_cei_to_dim_c
    kind: join
    status: unresolved
    source: declared
    active: false                            # ← 사용 시 ERR-RELATIONSHIP_NOT_ACTIVE
    from: FACT_CEI
    to: DIM_C
    on:
      - { left: C_ID, right: C_ID }
    evidence: "ID namespace 불일치. 매핑 규칙 미확인"
```

#### 4.3.1 `kind`의 의미 — 가장 중요한 필드

| `kind` | Planner 동작 | 사용처 |
|---|---|---|
| `join` | `QuerySpec.joins`에서 참조 가능. SQL `JOIN` 생성 | 차원 확장, Master 해석 |
| `context` | **`joins`에서 참조하면 `ERR-RELATIONSHIP_NOT_JOINABLE`.** `carry` 규칙으로 필터 문맥만 반환 | Grain이 다른 Detail 조회 |

`kind: context`가 필요한 이유는 중복 집계다. Fact와 Detail을 무조건 `JOIN`하면 1:N 확장으로
Fact의 Metric이 행 수만큼 중복된다. 이 오류는 **유효한 SQL을 만들고 그럴듯한 숫자를 반환**하므로
fail-closed 안전망에 걸리지 않는다(SAD 8.4·ADR-020이 다루는 "조용한 오류"와 같은 부류다).
따라서 **선언 단계에서 JOIN 가능성 자체를 제거**한다.

#### 4.3.2 `status`와 `active`의 구분

- `status`는 **검증 수준**이다. 사람이 실데이터로 확인하고 근거와 함께 선언한다(ADR-027 R-2).
  자동 산출 confidence가 아니다(GLOSSARY 8장 금지 표현).
- `active`는 **사용 허용 여부**다. `status: unresolved`여도 `active: true`일 수 있고 그 반대도 가능하다.
- `active: false`인 관계는 TOOL-002가 **반환은 하되** `usable: false`로 표시하고,
  `joins`에서 참조하면 거부한다. 존재를 숨기지 않는 이유는 Agent가 "관계가 없다"와
  "관계가 있으나 미검증이다"를 구분해 사용자에게 설명할 수 있어야 하기 때문이다.
- `status != confirmed`인 관계를 실제로 사용한 경우 응답 `warnings`에 기록한다.
  DataLens는 이를 받아 해석 공개 의무(SAD 8.4 규칙 5)를 이행한다.

#### 4.3.3 `symmetric_group` (R-4)

같은 `symmetric_group` 값을 가진 관계들은 **논리적으로 하나의 관계**다.
TOOL-002는 그룹 단위로 묶어 반환하며, Agent가 "연결된 것"을 물으면 그룹 전체를 후보로 제시한다.

Planner는 `symmetric_group`을 `UNION ALL`이 아니라 **분기 선택**으로 처리한다.
즉 QuerySpec은 그룹이 아니라 **개별 `relationship_id`를 지정**해야 한다.
양쪽을 모두 조회해야 하면 Agent가 두 번 질의하고 `transform`으로 합친다.

> 근거: `OR` 조인은 MySQL에서 인덱스를 무력화한다. 대상 테이블에는 애초에 인덱스가 없으므로(SC-3)
> 두 분기를 각각 프루닝된 범위에서 실행하는 편이 안전하고, 무엇보다 **생성 SQL이 단순해져
> AST 검증과 `EXPLAIN` 프루닝 확인이 쉬워진다.**

#### 4.3.4 Pack 로드 시 검증 (`ERR-PACK_INVALID`)

| # | 검증 | 실패 시 |
|---|---|---|
| 1 | `id` 전역 유일 | 기동 실패 |
| 2 | `from`/`to`가 `pack.yaml` allowlist에 존재 | 기동 실패 |
| 3 | `on`/`carry`의 컬럼이 Catalog에 존재 | 기동 실패 |
| 4 | `on` 양쪽 컬럼의 타입 호환 (길이 불일치는 경고 — SC-7) | 경고 |
| 5 | `kind: context`에 `on`이 있으면 오류 (`carry`를 써야 함) | 기동 실패 |
| 6 | `kind: join`에 `carry`가 있으면 오류 | 기동 실패 |
| 7 | `symmetric_group` 구성원의 `from`/`to`가 동일 | 기동 실패 |

**4번은 경고에 그친다.** 실측에서 조인 컬럼 길이가 실제로 다르기 때문이다
(`varchar(30)` ↔ `varchar(4)`, SC-7). 이는 스키마의 사실이므로 거부하면 PoC가 성립하지 않는다.
대신 경고를 남기고, **값 변형을 금지**한다(N-23).

---

## 5. Catalog Snapshot

### 5.1 수집 대상 (ADR-016)

`information_schema`의 `TABLES` / `COLUMNS` / `STATISTICS` / `KEY_COLUMN_USAGE` / `PARTITIONS`.
갱신은 **명시적 CLI 명령으로만** 수행한다(N-14).

```
queryforge catalog refresh              # 메타데이터만
queryforge catalog refresh --profile    # 프로파일 포함 (ADR-023)
```

### 5.2 프로파일 수집 제약 (ADR-023)

| 규칙 | 내용 |
|---|---|
| P-1 | `information_schema.STATISTICS.CARDINALITY` 우선. 충족되면 추가 쿼리 0 |
| P-2 | 실데이터 접근은 **단일 파티션 한정 + `LIMIT` 강제**. 전체 스캔 금지 |
| P-3 | 런타임 수집 금지. CLI에서만 |
| P-4 | `MAX_EXECUTION_TIME` 동일 적용 |
| P-5 | 결과는 `candidate` 근거 자료이며 Semantic Truth가 아니다(ADR-020) |

프로파일 필드: `distinct_count_est`, `null_ratio`, `min` / `max`, `sample_values`(최대 20),
`role_hint`(`identifier` / `dimension` / `metric` / `temporal`).

**`role_hint`는 힌트일 뿐이다.** 집계 안전은 `role_hint`가 아니라 11장 검증 규칙이 보장한다.

### 5.3 Fingerprint와 변경 대응

```
fingerprint = sha256( 정규화된 (table, column, type, nullable, partition_key) 목록 )
```

- 응답 `meta.catalog_fingerprint`에 항상 포함한다.
- `pack.yaml`의 `target_schema_fingerprint`와 불일치하면 기동 시 경고하고
  영향 분석 리포트를 출력한다(SAD 18.2). **기동을 막지는 않는다** — 컬럼 추가는 무해하기 때문이다.
- 삭제·타입 변경으로 Pack 선언이 깨진 경우에만 해당 관계·테이블을 `active: false`로 자동 강등하고
  경고를 남긴다.

---

## 6. TOOL-001 `schema`

DB 메타데이터 탐색. **전체 반환을 구조적으로 금지**한다(ADR-017).

### 6.1 입력

```json
{
  "session_id": "sess_…",
  "operation": "list_tables",
  "table": null,
  "columns": null
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `operation` | enum: `list_tables` \| `describe_table` | ✅ | 2단계 탐색만 허용 |
| `table` | string | `describe_table`일 때 ✅ | allowlist 검사 대상 |
| `columns` | string[] | ✗ | 지정 시 해당 컬럼만 반환 (응답 축소용) |

### 6.2 출력 — `list_tables`

```json
{
  "ok": true,
  "result": {
    "tables": [
      { "name": "FACT_A",        "kind": "fact",   "partitioned": true,  "column_count": 32, "comment": null },
      { "name": "FACT_A_DETAIL", "kind": "fact",   "partitioned": true,  "column_count": 16, "comment": null },
      { "name": "DIM_B",         "kind": "master", "partitioned": false, "column_count": 8,  "comment": null }
    ],
    "scope": "domain_pack_allowlist",
    "total_in_scope": 11
  }
}
```

- **`scope`는 항상 `domain_pack_allowlist`다.** DB 전체 목록을 반환하는 모드는 존재하지 않는다(N-22).
- `kind`는 `partitions.yaml`의 `partitioned` 값에서 파생한다. 도메인 지식이 아니다.

### 6.3 출력 — `describe_table`

```json
{
  "ok": true,
  "result": {
    "table": "FACT_A",
    "partitioned": true,
    "partition_key": "EVENT_TIME",
    "secondary_time_columns": ["CDATE", "CHOUR", "CTIME"],
    "default_period": { "days": 1 },
    "max_range": { "days": 31 },
    "columns": [
      { "name": "EVENT_TIME",  "type": "datetime",    "nullable": false,
        "role": "temporal",   "aggregatable": false, "is_partition_key": true },
      { "name": "CDATE",       "type": "varchar(8)",  "nullable": true,
        "role": "temporal",   "aggregatable": false, "is_secondary_time": true },
      { "name": "NODE_TYPE",   "type": "varchar(4)",  "nullable": true,
        "role": "dimension",  "aggregatable": false },
      { "name": "NODE_ID",     "type": "varchar(30)", "nullable": true,
        "role": "identifier", "aggregatable": false },
      { "name": "BRANCH_ID",   "type": "int",         "nullable": true,
        "role": "identifier", "aggregatable": false, "role_source": "declared" },
      { "name": "SUCCESS_CNT", "type": "int",         "nullable": true,
        "role": "metric",     "aggregatable": true }
    ]
  }
}
```

#### `aggregatable` 필드 — ADR-022의 사전 노출

`aggregatable: false`인 컬럼에 `sum`/`avg`/`stddev` 등을 적용하면 거부된다(11장 V-5).
이 값을 **describe 단계에서 미리 노출**하는 이유는 Agent가 잘못된 Spec을 만들 확률 자체를 낮추기 위함이다.
거부는 안전망이고, 노출은 예방이다.

`role` 결정 우선순위:

```
1. Domain Pack의 declared_role          ← role_source: "declared"
2. 명명 패턴 (*_ID, *_KEY, *_CODE, *_NO 접미사)  ← role_source: "pattern"
3. 타입 기반 추정 (숫자 → metric, 날짜 → temporal)  ← role_source: "type"
```

**PK·FK는 판정 근거에서 사실상 빠진다.** 대상 스키마에 존재하지 않기 때문이다(SC-2, SC-4).
따라서 1·2단계가 유일한 방어선이며, Pack 선언 누락은 팩 검증 경고로 보고한다(ADR 액션 아이템 #14).

### 6.4 인수 기준

| ID | 내용 |
|---|---|
| `TOOL-001/AC-1` | `list_tables`가 allowlist 외 테이블을 반환하지 않는다 |
| `TOOL-001/AC-2` | allowlist 외 테이블에 `describe_table` 요청 시 `ERR-TABLE_NOT_IN_SCOPE` |
| `TOOL-001/AC-3` | 존재하지 않는 테이블 요청 시 `ERR-UNKNOWN_TABLE` + `did_you_mean` |
| `TOOL-001/AC-4` | 식별자 성격 컬럼의 `aggregatable`이 `false`다 |
| `TOOL-001/AC-5` | `describe_table` 응답에 `partition_key`와 `secondary_time_columns`가 포함된다 |
| `TOOL-001/AC-6` | 응답 총 크기가 설정된 `mcp.max_response_bytes`를 넘지 않는다 |

---

## 7. TOOL-002 `relationship`

### 7.1 액션 아이템 #8 결론 — 응답 필드 확정

> **결론: 필요하다. 아래 5개 필드를 Tool Schema에 포함하여 동결한다.**
>
> | 필드 | 없으면 발생하는 문제 |
> |---|---|
> | `kind` (`join` \| `context`) | Agent가 Grain이 다른 Detail을 JOIN하여 **Metric이 조용히 중복**된다. 사후 탐지 불가 |
> | `status` | 미검증 관계를 검증된 것처럼 사용하고, 해석 공개 의무(SAD 8.4)를 이행할 근거가 없다 |
> | `usable` | `active: false` 관계를 Agent가 시도 → DataLens의 제한된 Tool-call budget을 낭비한다 |
> | `symmetric_group` | 대칭 endpoint 중 한쪽만 조회하여 **결과가 조용히 누락**된다 |
> | `carry` | 문맥 전파 관계에서 무엇을 후속 Query에 넘길지 Agent가 알 수 없다 |
>
> 동결 후 추가하려면 ADR 개정이 필요하므로(GLOSSARY 5장) **S0 내에 포함하는 것이 정당하다.**
> 다섯 필드 모두 값 집합이 작아 컨텍스트 비용은 관계당 30~50 토큰 수준이다.

### 7.2 입력

```json
{
  "session_id": "sess_…",
  "operation": "find",
  "from": "FACT_A",
  "to": null,
  "include_inactive": true
}
```

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `operation` | enum: `find` \| `get` | ✅ | `get`은 `relationship_id`로 단건 조회 |
| `from` | string | `find`일 때 ✅ | 출발 테이블 |
| `to` | string | ✗ | 미지정 시 `from`에서 도달 가능한 모든 관계 |
| `relationship_id` | string | `get`일 때 ✅ | |
| `include_inactive` | bool | ✗ (기본 `true`) | `false`면 `usable: true`만 반환 |

### 7.3 출력

```json
{
  "ok": true,
  "result": {
    "relationships": [
      {
        "id": "rel_fact_a_to_dim_b",
        "kind": "join",
        "usable": true,
        "status": "confirmed",
        "source": "declared",
        "from": "FACT_A",
        "to": "DIM_B",
        "on": [
          { "left": "NODE_TYPE", "right": "EQUIP_TYPE" },
          { "left": "NODE_ID",   "right": "EQUIP_ID" }
        ],
        "cardinality": "many_to_one",
        "symmetric_group": null,
        "carry": null,
        "guard": null,
        "notes": null
      },
      {
        "id": "rel_fact_a_to_detail",
        "kind": "context",
        "usable": true,
        "status": "structural",
        "source": "declared",
        "from": "FACT_A",
        "to": "FACT_A_DETAIL",
        "on": null,
        "cardinality": "one_to_many",
        "symmetric_group": null,
        "carry": [
          { "left": "NODE_TYPE",  "right": "NODE_TYPE",  "mode": "equals" },
          { "left": "NODE_ID",    "right": "NODE_ID",    "mode": "equals" },
          { "left": "EVENT_TIME", "right": "EVENT_TIME", "mode": "time_window" }
        ],
        "guard": null,
        "notes": "grain_mismatch"
      },
      {
        "id": "rel_cei_to_dim_c",
        "kind": "join",
        "usable": false,
        "status": "unresolved",
        "source": "declared",
        "from": "FACT_CEI",
        "to": "DIM_C",
        "on": [ { "left": "C_ID", "right": "C_ID" } ],
        "cardinality": null,
        "symmetric_group": null,
        "carry": null,
        "guard": null,
        "notes": "id_namespace_mismatch"
      }
    ],
    "symmetric_groups": [
      { "group": "grp_fact_a_link", "members": ["rel_fact_a_to_link_ep1", "rel_fact_a_to_link_ep2"] }
    ]
  },
  "warnings": []
}
```

**`notes`는 열거형 코드다.** 자유 문장이 아니다(ADR-015: QueryForge는 언어를 모른다).
값 집합: `grain_mismatch` / `id_namespace_mismatch` / `no_sample_data` / `type_length_mismatch` / `null`.

### 7.4 Agent 사용 규칙 (계약)

1. `kind: context` 관계는 `QuerySpec.joins`에 넣지 않는다. `carry`를 후속 QuerySpec의 `filters`로 옮긴다.
2. `usable: false`는 시도하지 않는다. 사용자에게 "관계가 확인되지 않았다"고 설명한다.
3. `symmetric_group`이 있으면 **양쪽 분기를 모두 조회한 뒤** `transform`으로 합치는 것을 기본으로 한다.
4. `status != confirmed`를 사용하면 최종 응답에 그 사실을 밝힌다(SAD 8.4 규칙 5).

### 7.5 인수 기준

| ID | 내용 |
|---|---|
| `TOOL-002/AC-1` | 복합 키 관계의 `on`이 2개 이상 컬럼 쌍을 순서대로 반환한다 |
| `TOOL-002/AC-2` | `kind: context` 관계는 `on`이 `null`이고 `carry`가 채워진다 |
| `TOOL-002/AC-3` | `active: false` 관계가 `usable: false`로 반환되고 목록에서 누락되지 않는다 |
| `TOOL-002/AC-4` | `symmetric_group` 구성원이 `symmetric_groups`에 묶여 반환된다 |
| `TOOL-002/AC-5` | `status`가 Pack 선언값 그대로 반환된다 (자동 산출하지 않는다) |
| `TOOL-002/AC-6` | `notes`가 열거형 코드이며 자연어 문장이 아니다 |

---

## 8. TOOL-003 `query`

Query Specification으로 Dataset을 생성한다. **본 시스템의 유일한 1차 조회 경로다**(ADR-002).

### 8.1 QuerySpec 설계 원칙

| # | 원칙 | 근거 |
|---|---|---|
| Q-1 | 중첩 최대 2단계. 필터 기본형은 flat 리스트(암묵적 AND), OR는 명시적 그룹 1단계 | ADR-002 |
| Q-2 | operator는 enum. 자유 문자열 금지 | ADR-002 |
| Q-3 | JOIN은 `relationship_id` 참조만. 테이블·컬럼 직접 기술 금지 | ADR-002 |
| Q-4 | **시간 범위는 `filters`가 아니라 최상위 `time_range` 필드다** | 아래 8.2 |
| Q-5 | 지표 이름(`success_rate`)은 들어가지 않는다. 전개된 구조만 들어간다 | 1.2, 8.4 |
| Q-6 | 생성은 JSON Schema constrained decoding으로 강제 | ADR-002 |

### 8.2 Q-4가 핵심이다 — SC-1의 구조적 차단

대상 스키마에는 시간 컬럼이 4개 있다(`EVENT_TIME`, `CDATE`, `CHOUR`, `CTIME`).
`CDATE = '20240529'`는 사람이 보기에 합리적이고 **결과도 정확하지만 파티션 프루닝이 걸리지 않는다.**
개발 중에는 드러나지 않고 운영에서 터진다(SAD 2.4 SC-1).

시간 범위를 `filters` 안에 두면 Planner는 "필터 목록을 훑어 파티션 키 조건이 있는지" 확인해야 하고,
이 검사는 구현이 느슨해지기 쉽다. 대신 **1급 필드로 분리**하면:

- `time_range`의 존재 여부만 보면 되므로 검사가 구조적이다.
- 어느 컬럼에 적용할지는 Spec이 아니라 **`partitions.yaml`이 결정**한다. LLM이 컬럼을 고르지 않는다.
- `filters`에 파티션 키나 보조 시간 컬럼이 나타나면 그 자체로 오류다(`ERR-INVALID_TIME_FILTER`).

즉 **LLM이 `CDATE`로 시간 조건을 걸 수 있는 경로 자체를 스키마에서 제거한다.** P-4의 적용이다.

### 8.3 입력

```json
{
  "session_id": "sess_…",
  "spec": {
    "version": "1",
    "source": "FACT_A",
    "time_range": { "from": "2024-05-29T00:00:00+09:00", "to": "2024-05-30T00:00:00+09:00" },
    "joins": [
      { "relationship_id": "rel_fact_a_to_dim_b", "type": "left" }
    ],
    "filters": [
      { "column": "NODE_TYPE", "op": "eq", "value": "TYPE_X", "type": "string" }
    ],
    "filter_groups": [
      { "any_of": [
        { "column": "CALL_TYPE", "op": "eq", "value": "ATTACH", "type": "string" },
        { "column": "CALL_TYPE", "op": "eq", "value": "PAGING", "type": "string" }
      ] }
    ],
    "group_by": ["NODE_ID"],
    "aggregations": [
      { "fn": "sum", "column": "SUCCESS_CNT", "as": "success_cnt" },
      { "fn": "sum", "column": "ATTEMPT_CNT", "as": "attempt_cnt" }
    ],
    "derived": [
      { "as": "success_rate", "expr": "ratio", "numerator": "success_cnt", "denominator": "attempt_cnt", "scale": "percent" }
    ],
    "select": ["NODE_ID", "success_cnt", "attempt_cnt", "success_rate"],
    "order_by": [ { "column": "success_rate", "dir": "desc" } ],
    "limit": 100
  }
}
```

### 8.4 필드 정의

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `version` | `"1"` | ✅ | Spec 버전. 변경은 ADR 개정 |
| `source` | string | ✅ | 시작 테이블. allowlist 검사 |
| `time_range` | `{from, to}` ISO-8601 | 조건부 | `partitioned: true` 테이블이 포함되면 필수. 미지정 시 `ERR-MISSING_TIME_RANGE` |
| `joins` | 객체 배열 | ✗ | 최대 3개. `relationship_id` + `type`(`inner`\|`left`) |
| `filters` | 객체 배열 | ✗ | 암묵적 AND. **시간 컬럼 사용 금지** |
| `filter_groups` | 객체 배열 | ✗ | `any_of` 1단계만. 그룹 간에는 AND |
| `group_by` | string[] | ✗ | 지정 시 `select`는 `group_by` ∪ 집계 결과여야 함 |
| `aggregations` | 객체 배열 | ✗ | `fn`은 enum. `as`는 필수 |
| `derived` | 객체 배열 | ✗ | 파생 계산. `expr`은 enum (8.6) |
| `select` | string[] | ✅ | 최종 컬럼. 순서가 결과 컬럼 순서 |
| `order_by` | 객체 배열 | ✗ | 최대 3개 |
| `limit` | int | ✗ | 기본 1000, 최대 100000 |

#### operator enum (Q-2)

```
eq  ne  gt  gte  lt  lte  in  not_in  is_null  is_not_null  between  contains  starts_with
```

`contains` / `starts_with`는 `LIKE`로 전개하되 **와일드카드를 이스케이프**한다.
정규식 연산자는 제공하지 않는다.

#### 집계 함수 enum

```
count  count_distinct  sum  avg  min  max  median  stddev  variance  quantile
```

`sum` / `avg` / `stddev` / `variance` / `median` / `quantile`은 `aggregatable: false` 컬럼에
적용 시 거부된다(ADR-022, 11장 V-5). `count` / `count_distinct` / `min` / `max`는 항상 허용된다.

### 8.5 Q-5 — 지표 이름이 QuerySpec에 없는 이유

`success_rate`는 업무 개념이다. QueryForge가 그 이름을 해석하면 도메인 지식이 Core로 유입되어
ADR-009와 N-1을 위반한다. 따라서 **DataLens Agent가 `SemanticProvider.find_metric()`으로
정의를 얻어 전개한 뒤** `aggregations` + `derived` 조합으로 넣는다.

```
Agent:  "성공률"
   → find_metric() → { numerator: SUCCESS_CNT, denominator: ATTEMPT_CNT, aggregation: ratio_of_sums }
   → QuerySpec.aggregations: [sum(SUCCESS_CNT) as success_cnt, sum(ATTEMPT_CNT) as attempt_cnt]
   → QuerySpec.derived:      [{ expr: "ratio", numerator: success_cnt, denominator: attempt_cnt }]
```

QueryForge는 `ratio`가 무엇인지만 알면 되고, 그것이 "성공률"인지는 알 필요도 알아서도 안 된다.

### 8.6 `derived.expr` enum

자유 수식을 받지 않는다(N-2). 열거된 형태만 지원한다.

| `expr` | 계산 | 비고 |
|---|---|---|
| `ratio` | `numerator / denominator` | 분모 0이면 `null`. `scale: percent`면 ×100 |
| `difference` | `left - right` | |
| `pct_change` | `(left - right) / right` | 분모 0이면 `null` |
| `share` | `value / SUM(value) OVER ()` | 전체 대비 비중. Polars에서 계산 |

**분모 0은 오류가 아니라 `null`이다.** 오류로 처리하면 한 행 때문에 전체 조회가 실패한다.
`null` 발생 건수는 `warnings`에 `WARN-DIVISION_BY_ZERO`로 보고한다.

### 8.7 출력

```json
{
  "ok": true,
  "result": {
    "dataset_id": "ds_000001",
    "row_count": 42,
    "truncated": false,
    "columns": [
      { "name": "NODE_ID",      "type": "string" },
      { "name": "success_cnt",  "type": "int64" },
      { "name": "attempt_cnt",  "type": "int64" },
      { "name": "success_rate", "type": "float64" }
    ],
    "preview": [
      { "_row": 0, "NODE_ID": "0016", "success_cnt": 1204, "attempt_cnt": 1310, "success_rate": 91.91 }
    ],
    "entity_labels": [
      { "index": 0, "label": "0016", "column": "NODE_ID", "value": "0016" }
    ],
    "summary": { "row_count": 42, "column_count": 4, "null_counts": { "success_rate": 0 } },
    "lineage": { "parent_dataset_id": null, "operation": "query", "spec_digest": "sha256:…" },
    "partition_pruning": { "applied": true, "partitions_scanned": 2, "partitions_total": 3 }
  },
  "warnings": [
    { "code": "WARN-RELATIONSHIP_UNCONFIRMED",
      "detail": { "relationship_id": "rel_fact_a_to_link_ep1", "status": "structural" } }
  ]
}
```

#### `entity_labels` — 참조 해소의 전제조건

SAD 17.1이 명시하듯 **`entity_labels`가 없으면 "첫 번째"가 불가능하다.**
`group_by`가 있으면 그 첫 컬럼, 없으면 `select`의 첫 비집계 컬럼을 라벨로 삼는다.
`index`는 `order_by` 적용 **이후**의 안정적 행 순서다.

#### `partition_pruning` — NFR-010의 증거

개발·테스트 모드에서 `EXPLAIN`으로 실제 프루닝 적용 여부를 확인해 채운다.
운영 모드에서는 Planner의 결정값을 그대로 기록한다.
**`applied: false`인 응답이 하나라도 나오면 PoC 판정 기준 4번 위반이다.**

### 8.8 인수 기준

| ID | 내용 |
|---|---|
| `TOOL-003/AC-1` | `group_by` 컬럼이 `select`에 없으면 `ERR-INVALID_QUERY_SPEC` |
| `TOOL-003/AC-2` | 파티션 테이블에 `time_range` 부재 시 `ERR-MISSING_TIME_RANGE` + 파티션 키 힌트 |
| `TOOL-003/AC-3` | 생성 SQL이 `EXPLAIN`에서 partition pruning 적용됨 |
| `TOOL-003/AC-4` | `filters`에 파티션 키 또는 보조 시간 컬럼 사용 시 `ERR-INVALID_TIME_FILTER` |
| `TOOL-003/AC-5` | `max_range` 초과 시 `ERR-TIME_RANGE_TOO_WIDE` |
| `TOOL-003/AC-6` | `aggregatable: false` 컬럼에 `sum` 적용 시 `ERR-INVALID_AGGREGATION` + 허용 집계 힌트 |
| `TOOL-003/AC-7` | `kind: context` 관계를 `joins`에 넣으면 `ERR-RELATIONSHIP_NOT_JOINABLE` |
| `TOOL-003/AC-8` | `usable: false` 관계 참조 시 `ERR-RELATIONSHIP_NOT_ACTIVE` |
| `TOOL-003/AC-9` | 복합 키 관계가 모든 컬럼 쌍을 `AND`로 결합한 JOIN을 생성한다 |
| `TOOL-003/AC-10` | `preview`가 요청한 `preview_rows` 이하이며 byte budget 축소 시 warning을 포함한다 |
| `TOOL-003/AC-11` | `entity_labels`의 `index`가 `order_by` 적용 후 순서와 일치한다 |
| `TOOL-003/AC-12` | `derived.ratio`의 분모가 0인 행이 `null`이고 `WARN-DIVISION_BY_ZERO`가 보고된다 |
| `TOOL-003/AC-13` | `filter_groups`가 2단계 이상 중첩되면 스키마 검증에서 거부된다 |
| `TOOL-003/AC-14` | 비파티션 Master 단독 조회에 `time_range` 없이 성공한다 |
| `TOOL-003/AC-15` | 결과가 `max_rows` 초과 시 `ERR-RESULT_TOO_LARGE` (부분 반환하지 않는다) |

---

## 9. TOOL-004 `transform`

기존 Dataset에 연산 파이프라인을 적용한다. **DB에 접근하지 않는다.**

### 9.1 입력

```json
{
  "session_id": "sess_…",
  "dataset_id": "ds_000003",
  "pipeline": [
    { "op": "sort",  "by": [ { "column": "success_rate", "dir": "asc" } ] },
    { "op": "limit", "n": 10 }
  ]
}
```

파이프라인은 **최대 8단계**다. 여러 operation을 한 번에 받는 구조는 Agent 스텝 수를 줄이기 위한 것이며,
이는 정확도 설계다(ADR-017 근거: 스텝당 신뢰도가 곱으로 감쇠).

### 9.2 출력

TOOL-003과 동일한 형태이며 `lineage.parent_dataset_id`가 입력 Dataset을 가리킨다.
`partition_pruning`은 `null`이다(DB 미접근).

### 9.3 인수 기준

| ID | 내용 |
|---|---|
| `TOOL-004/AC-1` | 파이프라인 실행 중 DB 접속이 발생하지 않는다 |
| `TOOL-004/AC-2` | `lineage.parent_dataset_id`가 입력 `dataset_id`와 일치한다 |
| `TOOL-004/AC-3` | 9단계 이상 파이프라인은 `ERR-INVALID_TRANSFORM_SPEC` |
| `TOOL-004/AC-4` | 만료된 `dataset_id` 사용 시 `ERR-DATASET_EXPIRED` (lineage 재생성 가능 여부 포함) |
| `TOOL-004/AC-5` | 타 세션 `dataset_id` 접근 시 `ERR-SESSION_MISMATCH` |
| `TOOL-004/AC-6` | 10초 초과 시 `ERR-TRANSFORM_TIMEOUT` |
| `TOOL-004/AC-7` | 원본 Dataset이 변경되지 않는다 (새 Dataset 생성) |

---

## 10. TOOL-005 `describe`

Dataset 메타데이터 및 기초 통계 반환.

### 10.1 입력

```json
{
  "session_id": "sess_…",
  "dataset_id": "ds_000004",
  "stats": ["count", "mean", "median", "min", "max", "stddev", "quantile"],
  "columns": ["success_rate"],
  "quantiles": [0.25, 0.5, 0.75]
}
```

### 10.2 출력

```json
{
  "ok": true,
  "result": {
    "dataset_id": "ds_000004",
    "row_count": 10,
    "columns": [ { "name": "success_rate", "type": "float64" } ],
    "stats": {
      "success_rate": { "count": 10, "mean": 88.4, "median": 89.1,
                        "min": 71.2, "max": 96.0, "stddev": 7.3,
                        "quantile": { "0.25": 84.0, "0.5": 89.1, "0.75": 93.2 } }
    },
    "lineage": { "parent_dataset_id": "ds_000003", "operation": "sort+limit" }
  }
}
```

### 10.3 인수 기준

| ID | 내용 |
|---|---|
| `TOOL-005/AC-1` | 비수치 컬럼에 `mean` 요청 시 `ERR-COLUMN_TYPE_MISMATCH` |
| `TOOL-005/AC-2` | `aggregatable: false` 컬럼의 통계 요청이 `count` / `count_distinct` 외에는 거부된다 |
| `TOOL-005/AC-3` | 전체 행 데이터가 응답에 포함되지 않는다 |

---

## 11. Validation 파이프라인

### 11.1 순서 (고정)

**싸고 결정적인 검사를 먼저 수행한다.** DB 접근은 마지막이다.

| # | 단계 | 검사 | 대표 오류 |
|---|---|---|---|
| V-1 | Schema | JSON Schema 검증. 중첩 깊이·enum·필수 필드 | `ERR-INVALID_QUERY_SPEC` |
| V-2 | Scope | 모든 테이블이 Pack allowlist에 있는가 | `ERR-TABLE_NOT_IN_SCOPE` |
| V-3 | Catalog | 테이블·컬럼 실재 여부, 타입 | `ERR-UNKNOWN_TABLE` / `ERR-UNKNOWN_COLUMN` |
| V-4 | Relationship | id 실재, `usable`, `kind` 적합성 | `ERR-RELATIONSHIP_NOT_ACTIVE` / `_NOT_JOINABLE` |
| V-5 | **Aggregation safety** | `aggregatable: false` 컬럼의 수치 집계 차단 | `ERR-INVALID_AGGREGATION` |
| V-6 | **Time filter placement** | `filters`에 파티션 키·보조 시간 컬럼 사용 여부 | `ERR-INVALID_TIME_FILTER` |
| V-7 | **Partition policy** | `time_range` 존재, `max_range` 이내, 정책 선언 여부 | `ERR-MISSING_TIME_RANGE` / `_TOO_WIDE` |
| V-8 | Semantic 정합 | `group_by` ⊆ `select`, `order_by` 컬럼 존재, `derived` 참조 유효 | `ERR-INVALID_QUERY_SPEC` |
| V-9 | Plan | 실행 계획 수립. JOIN 순서, 프루닝 결정 | — |
| V-10 | SQL 생성 | Adapter가 dialect SQL 생성 | — |
| V-11 | **AST 검증** | sqlglot 파싱 후 금지 구문 탐지 | `ERR-SQL_VALIDATION_FAILED` / `_PARSE_FAILED` |
| V-12 | 프루닝 확인 | `EXPLAIN`으로 partition pruning 검증 (개발·테스트 모드) | `ERR-PRUNING_NOT_APPLIED` |
| V-13 | 실행 | `MAX_EXECUTION_TIME` 적용 | `ERR-QUERY_TIMEOUT` |
| V-14 | 결과 크기 | 행 수 상한 | `ERR-RESULT_TOO_LARGE` |

**V-11은 생성 주체와 무관하게 예외 없이 수행한다**(ADR-002 안전 경계). 정규식 기반 검사는 사용하지 않는다(N-3).

### 11.2 V-11 금지 구문

파싱된 AST를 순회하여 다음이 하나라도 있으면 거부한다.

```
DML          INSERT / UPDATE / DELETE / REPLACE / MERGE / LOAD
DDL          CREATE / ALTER / DROP / TRUNCATE / RENAME
권한         GRANT / REVOKE / SET
다중 구문     세미콜론으로 구분된 2개 이상의 statement
서브쿼리 깊이  3단계 초과
주석          실행 SQL에 주석 포함 금지 (힌트 위장 방지)
파일          INTO OUTFILE / LOAD_FILE
시스템 함수    SLEEP / BENCHMARK / GET_LOCK
```

**파싱 실패 자체가 거부 사유다**(ADR-007 대비책). 파서를 교체하지 않는다.

### 11.3 V-12의 운영 모드 차이

| 모드 | 동작 |
|---|---|
| 개발·테스트 | 매 쿼리 `EXPLAIN` 수행. 프루닝 미적용 시 **실행하지 않고 거부** |
| 운영 | `EXPLAIN` 생략(왕복 비용). Planner 결정값을 `partition_pruning`에 기록 |

개발 모드에서 프루닝을 강제 검증하는 이유는 SC-1 계열 결함이 **결과가 정확하고 성능만 틀리기 때문**이다.
PoC 데이터 140행에서는 성능 차이가 관측되지 않으므로, 사람이 눈으로 잡을 수 없다.

---

## 12. Transform Operation 카탈로그 (OP-)

동결 대상이 아니다. operation 추가는 Tool 증설이 아니다(SAD 9장).

### 12.1 행·열 조작

| ID | op | 인자 | 설명 |
|---|---|---|---|
| `OP-001` | `select` | `columns[]` | 컬럼 선택 |
| `OP-002` | `rename` | `map{}` | 컬럼명 변경 |
| `OP-003` | `filter` | `filters[]`, `filter_groups[]` | QuerySpec과 동일 문법 |
| `OP-004` | `sort` | `by[{column,dir}]` | 다중 키 |
| `OP-005` | `limit` | `n`, `offset` | |
| `OP-006` | `distinct` | `columns[]` | |
| `OP-007` | `head` / `tail` | `n` | |

### 12.2 집계·파생

| ID | op | 인자 | 설명 |
|---|---|---|---|
| `OP-010` | `group_by` | `by[]` | 단독 사용 불가. `aggregate`와 쌍 |
| `OP-011` | `aggregate` | `aggs[{fn,column,as}]` | 집계 함수 enum은 8.4와 동일 |
| `OP-012` | `derive` | `expr` enum (8.6) | 자유 수식 금지 |
| `OP-013` | `rank` | `by`, `method`, `as` | `method`: `dense` \| `min` \| `ordinal` |
| `OP-014` | `pct_of_total` | `column`, `as` | |
| `OP-015` | `delta` | `column`, `by`, `as` | 이전 행 대비 차이 |
| `OP-016` | `pct_change` | `column`, `by`, `as` | 분모 0 → `null` |

**rank·delta·pct_change는 Polars에서 수행한다.** MySQL 8 윈도우 함수는 가용하지만
PoC 1차 구현에서는 보류한다(ADR-005). Dataset이 이미 축소된 상태라 비용이 낮고 검증이 쉽다.

### 12.3 정리·형변환

| ID | op | 인자 | 설명 |
|---|---|---|---|
| `OP-020` | `fill_null` | `column`, `value` \| `strategy` | `strategy`: `zero` \| `forward` \| `backward` |
| `OP-021` | `drop_null` | `columns[]` | |
| `OP-022` | `cast` | `column`, `to` | `to`: `int64` \| `float64` \| `string` \| `datetime` |
| `OP-023` | `round` | `column`, `digits` | |

`cast`는 **명시적 요청일 때만** 수행한다. Planner가 조인 성사를 위해 자동 형변환하지 않는다(N-23).

### 12.4 결합

| ID | op | 인자 | 설명 |
|---|---|---|---|
| `OP-030` | `concat` | `dataset_ids[]` | 컬럼 구조가 동일해야 함. `symmetric_group` 양쪽 병합에 사용 |
| `OP-031` | `compare` | `dataset_id`, `on[]`, `metrics[]` | 두 Dataset의 지표 비교 |

`OP-030`은 7.4 규칙 3(대칭 endpoint 양쪽 조회 후 합치기)의 실행 수단이다.

---

## 13. 오류 코드 (ERR-) — 동결

`retryable: true`는 "Spec을 고쳐 재시도할 여지가 있음"을 뜻한다.

### 13.1 입력·스펙

| 코드 | retryable | `hint` 내용 | 발생 |
|---|---|---|---|
| `ERR-INVALID_QUERY_SPEC` | ✅ | `violations[]` (JSON Pointer + 사유) | V-1, V-8 |
| `ERR-INVALID_TRANSFORM_SPEC` | ✅ | `violations[]` | TOOL-004 |
| `ERR-UNSUPPORTED_OPERATION` | ✅ | `supported[]` | 미지원 op/fn |

### 13.2 범위·카탈로그

| 코드 | retryable | `hint` | 발생 |
|---|---|---|---|
| `ERR-TABLE_NOT_IN_SCOPE` | ❌ | `tables_in_scope[]` | V-2. **팩 설정 문제** |
| `ERR-UNKNOWN_TABLE` | ✅ | `did_you_mean[]` | V-3. **오타** |
| `ERR-UNKNOWN_COLUMN` | ✅ | `did_you_mean[]` | V-3 |
| `ERR-COLUMN_TYPE_MISMATCH` | ✅ | `expected`, `actual` | V-8, TOOL-005 |
| `ERR-CATALOG_STALE` | ❌ | `catalog_fingerprint`, `pack_fingerprint` | 5.3 |

`ERR-TABLE_NOT_IN_SCOPE`와 `ERR-UNKNOWN_TABLE`을 구분하는 이유는 **복구 경로가 다르기 때문**이다.
전자는 Agent가 고칠 수 없고(사용자에게 범위를 설명해야 한다), 후자는 `did_you_mean`으로 1회 교정된다.

컬럼 오타 대응이 특히 중요하다. 대상 스키마에는 `DATA_SUCESS_CNT` 같은 **원본 오타 컬럼이 존재**하며
(SC-8), 모델이 `SUCCESS`로 "교정"하면 `ERR-UNKNOWN_COLUMN`이 발생한다.
`did_you_mean`은 편집 거리 기준 상위 3개를 반환하여 이 경로를 1턴에 복구시킨다.

### 13.3 관계

| 코드 | retryable | `hint` | 발생 |
|---|---|---|---|
| `ERR-UNKNOWN_RELATIONSHIP` | ✅ | `available[]` | V-4 |
| `ERR-RELATIONSHIP_NOT_ACTIVE` | ❌ | `status`, `notes` | V-4. `active: false` |
| `ERR-RELATIONSHIP_NOT_JOINABLE` | ✅ | `kind`, `carry[]` | V-4. `kind: context`를 JOIN 시도 |

`ERR-RELATIONSHIP_NOT_JOINABLE`의 `hint.carry`는 **Agent가 곧바로 후속 QuerySpec을 만들 수 있도록**
필터 문맥을 그대로 담는다. 거부와 동시에 올바른 경로를 제시하는 것이 목적이다.

### 13.4 시간·파티션

| 코드 | retryable | `hint` | 발생 |
|---|---|---|---|
| `ERR-MISSING_TIME_RANGE` | ✅ | `partition_key`, `default_period` | V-7 |
| `ERR-TIME_RANGE_TOO_WIDE` | ✅ | `max_range`, `requested_range` | V-7 |
| `ERR-INVALID_TIME_FILTER` | ✅ | `partition_key`, `offending_columns[]` | V-6 |
| `ERR-PARTITION_POLICY_MISSING` | ❌ | `table` | V-7. `partitions.yaml` 미선언 |
| `ERR-PRUNING_NOT_APPLIED` | ❌ | `explain_output` | V-12 (개발 모드) |

### 13.5 집계 안전

| 코드 | retryable | `hint` | 발생 |
|---|---|---|---|
| `ERR-INVALID_AGGREGATION` | ✅ | `column`, `role`, `allowed_functions[]` | V-5 |

```json
{
  "code": "ERR-INVALID_AGGREGATION",
  "detail": { "column": "BRANCH_ID", "function": "sum", "role": "identifier", "role_source": "declared" },
  "hint": { "allowed_functions": ["count", "count_distinct", "min", "max"] },
  "retryable": true
}
```

### 13.6 실행·자원

| 코드 | retryable | 발생 |
|---|---|---|
| `ERR-SQL_PARSE_FAILED` | ❌ | V-11. 파싱 불가 |
| `ERR-SQL_VALIDATION_FAILED` | ❌ | V-11. 금지 구문 |
| `ERR-QUERY_TIMEOUT` | ❌ | 30초 초과 (ADR-012) |
| `ERR-TRANSFORM_TIMEOUT` | ❌ | 10초 초과 |
| `ERR-RESULT_TOO_LARGE` | ✅ | 행 수 상한 초과. `hint.suggest_limit` |
| `ERR-RESPONSE_TOO_LARGE` | ✅ | 설정된 `mcp.max_response_bytes` 초과 |
| `ERR-DB_UNAVAILABLE` | ❌ | 접속 실패 |

### 13.7 Dataset·세션

| 코드 | retryable | `hint` | 발생 |
|---|---|---|---|
| `ERR-DATASET_NOT_FOUND` | ❌ | — | 미존재 |
| `ERR-DATASET_EXPIRED` | ❌ | `lineage`, `regenerable` | TTL 만료 |
| `ERR-SESSION_MISMATCH` | ❌ | — | 타 세션 접근 (ADR-011) |
| `ERR-DATASET_LIMIT_EXCEEDED` | ✅ | `evicted[]` | 세션당 20개 초과 |

`ERR-DATASET_EXPIRED`의 `regenerable`은 lineage로 재생성 가능한지를 나타낸다.
Agent는 이를 보고 "다시 조회하겠습니다"와 "처음부터 다시 말씀해 주세요"를 구분한다(SAD 21.2).

### 13.8 기동·팩

| 코드 | 시점 |
|---|---|
| `ERR-PACK_INVALID` | 기동. 4.3.4 검증 실패 |
| `ERR-PACK_SCHEMA_MISMATCH` | 기동. allowlist 테이블 미실재 |
| `ERR-CONFIG_INVALID` | 기동. `--workers > 1` 등 (ADR-004) |

---

## 14. Dataset Manager

### 14.1 파라미터 (ADR-006)

| 항목 | 값 |
|---|---|
| data 저장 | Parquet (authoritative persistent storage) |
| metadata·lineage·ID 상태 | SQLite |
| 메모리 | hot cache |
| TTL | 30분 |
| 세션당 최대 | 20개 (초과 시 LRU 축출) |
| 단일 Dataset 최대 행 | 1,000,000 |
| 전체 메모리 상한 | 8GB |

`PersistentLocalDatasetStore`는 atomic publish와 재기동 복구를 제공한다. `DatasetStore` 경계는 향후
shared/object store 구현으로 교체할 수 있게 유지한다.

### 14.2 Lineage

```json
{ "dataset_id": "ds_000004", "parent_dataset_id": "ds_000003",
  "operation": "sort+limit", "spec_digest": "sha256:…", "created_at": "…", "session_id": "sess_…" }
```

`spec_digest`는 Interaction Log(ADR-024)의 회귀 탐지에 사용된다. 동일 발화가 다른 digest를 만들면
QuerySpec이 변한 것이다.

### 14.3 세션 종료

DataLens는 내부 mapping된 **QueryForge Application Session ID**로 release hint를 보낸다. DataLens 외부
Session ID나 MCP Transport Session ID를 전달하지 않는다. release는 즉시 물리 삭제 계약이 아니며 통지가
유실되어도 QueryForge retention·cleanup 정책으로 회수된다(SAD 26.3).

---

## 15. SQL 생성 규칙

| # | 규칙 | 근거 |
|---|---|---|
| S-1 | 모든 리터럴은 **파라미터 바인딩**. 문자열 연결로 SQL을 만들지 않는다 | 주입 방어 |
| S-2 | 식별자는 백틱으로 인용하고 Catalog에 존재하는 이름만 사용 | V-3 통과분만 |
| S-3 | `SELECT *` 금지. 항상 명시적 컬럼 목록 | 컨텍스트·성능 |
| S-4 | 시간 조건은 항상 `partition_key BETWEEN ? AND ?` 형태 | 프루닝 보장 |
| S-5 | 복합 키 JOIN은 모든 컬럼 쌍을 `AND`로 결합 | ADR-027 R-1 |
| S-6 | `guard` 조건은 `ON` 절에 포함한다 (`WHERE`가 아니라) | `LEFT JOIN` 의미 보존 |
| S-7 | 윈도우 함수 미사용 (PoC 1차) | ADR-005 |
| S-8 | 값 변형 함수(`CAST` / `LPAD` / `TRIM` / `UPPER`) 자동 삽입 금지 | N-23, SC-7 |
| S-9 | 생성 SQL은 단일 statement. 주석 없음 | V-11 |
| S-10 | `MAX_EXECUTION_TIME` 힌트를 항상 부착 | ADR-012 |

### 15.1 복합 키 JOIN 예 (S-5)

```sql
SELECT f.`NODE_ID`, SUM(f.`SUCCESS_CNT`) AS `success_cnt`
FROM `FACT_A` /*+ MAX_EXECUTION_TIME(30000) */ AS f
LEFT JOIN `DIM_B` AS d
  ON  d.`EQUIP_TYPE` = f.`NODE_TYPE`
  AND d.`EQUIP_ID`   = f.`NODE_ID`
WHERE f.`EVENT_TIME` BETWEEN ? AND ?
  AND f.`NODE_TYPE` = ?
GROUP BY f.`NODE_ID`
ORDER BY `success_cnt` DESC
LIMIT 100
```

`NODE_ID`가 `varchar(30)`이고 `EQUIP_ID`가 `varchar(4)`지만(SC-7) **양쪽에 어떤 변환도 적용하지 않는다.**
MySQL의 문자열 비교 의미에 맡기며, 불일치가 관측되면 그것은 데이터의 사실이므로
`relationships.yaml`의 `status`를 낮추고 근거를 기록한다.

---

## 16. 인수 기준 ↔ 테스트 매핑

GLOSSARY 4장의 AC 참조 규칙을 따른다: `<기능ID>/AC-<n>` → `test_<기능ID소문자>_ac<n>`

```
TOOL-001/AC-2  → tests/schema/test_scope.py::test_tool001_ac2
TOOL-002/AC-2  → tests/relationship/test_context_kind.py::test_tool002_ac2
TOOL-003/AC-3  → tests/query/test_pruning.py::test_tool003_ac3
TOOL-003/AC-4  → tests/query/test_time_filter.py::test_tool003_ac4
TOOL-003/AC-6  → tests/query/test_aggregation_safety.py::test_tool003_ac6
TOOL-003/AC-7  → tests/query/test_relationship_kind.py::test_tool003_ac7
TOOL-004/AC-1  → tests/transform/test_no_db.py::test_tool004_ac1
```

### 16.1 고위험 AC (우선 구현)

다음은 **조용한 오류**를 막는 AC다. 실패해도 결과가 정상으로 보이므로 테스트가 유일한 방어선이다.

| AC | 막는 것 |
|---|---|
| `TOOL-003/AC-3` | 프루닝 미적용 — 결과는 정확, 성능만 붕괴 (SC-1) |
| `TOOL-003/AC-4` | 보조 시간 컬럼 필터 — 동일 |
| `TOOL-003/AC-6` | `SUM(식별자)` — 의미 없는 숫자를 정상 반환 (SC-2) |
| `TOOL-003/AC-7` | Grain 불일치 JOIN — Metric 중복을 정상 반환 |
| `TOOL-003/AC-11` | `entity_labels` 순서 불일치 — "첫 번째"가 다른 것을 가리킴 |
| `TOOL-002/AC-4` | `symmetric_group` 누락 — 한쪽 endpoint 결과만 조회 |

### 16.2 픽스처 원칙

- 테이블·컬럼명은 **중립 명칭**을 사용한다(1.3). `FACT_A`, `FACT_A_DETAIL`, `DIM_B`, `FACT_LINK`.
- 오타 컬럼 케이스(SC-8)를 재현하기 위해 픽스처에 의도적 오타 컬럼 1개를 포함한다(`DATA_SUCESS_CNT` → `VAL_SUCESS_CNT`).
- 조인 컬럼 길이 불일치(SC-7)를 재현한다(`varchar(30)` ↔ `varchar(4)`).
- 파티션은 시간 단위 3개, `MAXVALUE` 없음으로 구성한다(SC-9).
- **PK·인덱스를 만들지 않는다**(SC-2, SC-3). 실제 스키마와 같아야 한다.

이 픽스처는 DataLens의 mock 서버로 재사용한다(ADR-019).

---

## 17. PoC 구현 범위

### 17.1 구현

| 영역 | 범위 |
|---|---|
| Tool | 5종 전부 |
| Query | projection, filter(AND / OR 1단계), 비교·IN·NULL·범위, join(relationship 참조), group by, aggregation, derived, order by, limit |
| Transform | `OP-001` ~ `OP-031` |
| 통계 | TOOL-005의 stats 전체 |
| 안전 | V-1 ~ V-14 전 단계 |

### 17.2 인터페이스만 확보 (미구현)

| 항목 | 상태 |
|---|---|
| `query_sql` (Text-to-SQL) | tool 정의·flag·Validator 경로만. 본체 `NotImplementedError` (ADR-002) |
| MySQL 외 Adapter | `DatabaseAdapter` 인터페이스만 |
| Dataset spill | `DatasetStore` 인터페이스만 (ADR-006) |
| 윈도우 함수 기반 rank | Polars 경로만 구현 (ADR-005) |
| 다중 워커 / 공유 Dataset | 기동 차단 (ADR-004) |

---

## 18. 열린 항목

| # | 항목 | 관련 | 기한 |
|---|---|---|---|
| 1 | `partition_granularity` 운영 실제값 (hour / day) | ADR-016 개정, 액션 아이템 #13 | S1 착수 전 |
| 2 | `max_range` 기본값 — granularity 확정 후 산정 | 4.2 | S1 착수 전 |
| 3 | Core `int` 컬럼 `declared_role` 완비 | ADR-022, 액션 아이템 #14 | S1 중 |
| 4 | `contains` / `starts_with`의 `utf8mb3` 콜레이션 동작 | SC-5, 액션 아이템 #12 | S3 |
| 5 | `OP-031 compare` 상세 인자 | 12.4 | S2 |
| 6 | `ERR-` 코드의 DataLens 측 로케일 메시지 매핑표 | ADR-015 | S3 |

---

## 개정 이력

| 버전 | 일자 | 내용 |
|---|---|---|
| 0.1 | 2026-08-10 | 초안. Tool Schema 동결(3장·6~10장·13장). 액션 아이템 #8 결론(TOOL-002 5개 필드 추가), `relationships.yaml` 스키마 확정(ADR-027 구현), QuerySpec `time_range` 1급 필드화(SC-1 구조적 차단), OP-/ERR- 카탈로그 등록 |
| 0.2 | 2026-09-08 | 현재 QueryForge 외부 계약과 정합화. request-scoped Preview/10MB circuit breaker, Persistent Local DatasetStore, QueryForge Application Session 및 MCP Transport Session 분리 반영 |
