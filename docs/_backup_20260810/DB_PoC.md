# DataLens PoC Database Scope

## 1. 문서 목적

본 문서는 DataLens PoC에서 사용할 Database Schema의 최종 범위를 정의한다.

PoC의 목표는 전체 운영 Database를 포괄하는 것이 아니라, 작은 EPC 데이터 영역에서 다음 흐름을 끝까지 검증하는 것이다.

```text
자연어 질문
  → Schema/Semantic 탐색
  → Query Specification 생성
  → QueryForge 조회
  → 통계 결과 해석
  → 후속 질문을 통한 상세·원인·관계 분석
```

최종 Core Scope는 `PM_EPC_KPI_1M`을 중심으로 한 11개 테이블이다. Application, CEI 5분 통계, 부가 Master 및 Code/Reference 테이블은 Core에서 제외하고 Extension 또는 Deferred로 관리한다.

이 범위는 현재 Dataset의 데이터 유무와 Schema가 표현할 수 있는 업무 범위를 분리해서 정의한다. 현재 데이터가 없다는 이유만으로 Schema 지원 대상을 제거하지 않는다.

---

## 2. 범위 결정 원칙

Core 테이블은 다음 조건을 기준으로 선정했다.

1. 자연어 통계 질의의 시작점이 되는 EPC KPI Fact인가
2. KPI 결과에서 Cause, Root Cause, Detach로 직접 Drill-down할 수 있는가
3. EPC Node와 연결된 Link 및 Link 상세 원인을 탐색할 수 있는가
4. EPC 식별자를 사람이 이해할 수 있는 장비 정보로 해석하는 데 필요한가
5. 현재 PoC의 연속 질의 시나리오에 직접 사용되는가
6. 실데이터 또는 일관된 Schema 구조로 관계를 설명할 수 있는가

다음 조건에 해당하는 테이블은 Extension 또는 Deferred로 분리한다.

- Core 질의 흐름에 필수적이지 않은 별도 분석 영역
- 실제 키 체계 또는 관계가 아직 확인되지 않은 데이터
- 현재 Dataset에 데이터가 없어 직접 검증할 수 없는 확장 Fact
- Core 컬럼 자체에 사람이 해석 가능한 값이 있어 Join 필요성이 낮은 Code Master
- IP, Software, Group 등 PoC 핵심 시나리오 밖의 부가 속성

---

## 3. 최종 Core Scope: 11개 테이블

### 3.1 Main Fact

| 테이블 | 역할 | Core 선정 이유 |
|---|---|---|
| `PM_EPC_KPI_1M` | 1분 단위 EPC KPI | 모든 기본 통계 질의의 시작점 |

`PM_EPC_KPI_1M`은 `NODE_TYPE`, `NODE_ID`, `EVENT_TIME`, `CALL_TYPE` 등의 차원과 Attempt, Success, Drop, Detach 계열 지표를 제공하는 중심 Fact다. Schema 의미상 MME 전용 테이블이 아니라 PGW, SGW, MME 등 EPC Node를 포괄하는 범용 KPI Fact로 취급한다.

### 3.2 EPC Detail

| 테이블 | 역할 | 대표 Drill-down |
|---|---|---|
| `PM_EPC_CAUSE_1M` | Cause별 EPC 통계 | 성공률 저하의 주요 Cause 분석 |
| `PM_EPC_ROOT_CAUSE_1M` | EPC Root Cause 통계 | Cause보다 상세한 원인 분석 |
| `PM_EPC_DETACH_DETAIL_1M` | EPC Detach 상세 통계 | Detach 증가 원인 분석 |

이 세 테이블은 `PM_EPC_KPI_1M`의 Node, 시간 및 Call Type 문맥을 유지하면서 상세 원인으로 이동하는 데 사용한다.

### 3.3 Link Detail

| 테이블 | 역할 | 대표 Drill-down |
|---|---|---|
| `PM_LINK_EPC_KPI_1M` | EPC 관련 Link KPI | 특정 EPC Node에 연결된 Link 비교 |
| `PM_LINK_EPC_ROOT_CAUSE_1M` | Link Root Cause 통계 | 성능이 나쁜 Link의 원인 분석 |
| `PM_LINK_EPC_DETACH_DETAIL_1M` | Link Detach 상세 통계 | Link 관점의 Detach 상세 분석 |

여기서 Link는 EPC 장비 간 연결만을 의미하지 않는다. EPC Node는 `NODE1` 또는 `NODE2` 중 한 endpoint로 참여하며, 반대편 endpoint는 VLAN 또는 Interface와 같은 Network Resource일 수 있다.

### 3.4 Equipment Master

| 테이블 | 역할 | 비고 |
|---|---|---|
| `CM_EPC_INFO` | 공통 EPC 장비 정보 | 범용 Node 식별 및 장비명 해석의 기준 Master |
| `CL_MME` | MME Master | MME 세부 속성 확장 |
| `CL_SGW` | SGW Master | 현재 Dataset은 비어 있으나 Schema 지원 유지 |
| `CL_PGW` | PGW Master | PGW 세부 속성 확장 |

`CM_EPC_INFO`를 범용 EPC 장비 해석의 우선 기준으로 사용하고, 장비 유형별 세부 속성이 필요할 때 `CL_MME`, `CL_SGW`, `CL_PGW`를 사용한다.

### 3.5 확정 목록

```text
# Main Fact
PM_EPC_KPI_1M

# EPC Detail
PM_EPC_CAUSE_1M
PM_EPC_ROOT_CAUSE_1M
PM_EPC_DETACH_DETAIL_1M

# Link Detail
PM_LINK_EPC_KPI_1M
PM_LINK_EPC_ROOT_CAUSE_1M
PM_LINK_EPC_DETACH_DETAIL_1M

# Equipment Master
CM_EPC_INFO
CL_MME
CL_SGW
CL_PGW
```

총 11개 테이블이다.

---

## 4. 현재 PoC Dataset 제약

현재 검증 Database는 운영 데이터의 규모, 최신성 및 장비 구성을 대표하지 않는다. 확인된 데이터 상태는 다음과 같다.

| 항목 | 확인 결과 |
|---|---|
| 중심 테이블 | `PM_EPC_KPI_1M` |
| `NODE_TYPE` | `MME` |
| `NODE_ID` | `0016` |
| 데이터 건수 | 140건 |
| 시간 범위 | 2024-05-29 11:20:00 ~ 2024-05-30 11:29:00 |
| 고유 Node 수 | 1개 |
| SGW 데이터 | 확인되지 않음; `CL_SGW`와 `PM_CEI_SGW_5M` 모두 데이터 없음 |

따라서 현재 Dataset으로 검증할 수 있는 것은 MME 0016의 시간대별 KPI, Cause/Root Cause/Detach Drill-down 및 연결 Link 탐색이다. 다음 항목은 현재 Dataset만으로 충분히 검증할 수 없다.

- 여러 MME 간 비교 및 순위
- PGW 또는 SGW별 KPI 비교
- PGW → SGW → MME와 같은 다중 EPC 관계 탐색
- SGW Master와 Fact의 실제 값 일치
- 최근 운영 상태를 전제로 한 질의

이 제약은 Schema Scope의 제약이 아니라 PoC Dataset의 적재 상태에 따른 제약이다. 구현을 MME 0016 전용으로 축소해서는 안 된다.

PoC E2E 시연 전에 데이터 추가가 가능하다면 최근 시점의 MME 및 PGW를 각각 2~3대 이상 적재하는 것이 좋다. 이를 통해 장비 간 비교, Top/Bottom, 이상 장비 선택 후 원인 분석과 같은 후속 질의를 검증할 수 있다. SGW 데이터가 확보되면 Schema상 유지한 SGW 관계도 별도로 검증한다.

---

## 5. Core 사용자 시나리오

현재 Dataset에서 현실적으로 검증 가능한 대표 흐름은 다음과 같다.

```text
MME_16의 시간대별 KPI를 보여줘
  → 성공률이 가장 낮았던 시간은?
  → 그 시간의 주요 Cause는?
  → 가장 많이 발생한 Root Cause는?
  → 해당 MME와 연결된 Link의 상태는?
  → 성능이 나쁜 Link의 Root Cause 또는 Detach 상세는?
```

추가 데이터가 확보되면 다음 흐름으로 확장한다.

```text
EPC 장비별 성공률을 비교해줘
  → 가장 낮은 장비는?
  → 그 장비의 주요 Cause는?
  → 연결된 Link 중 문제가 큰 것은?
  → 다른 장비 또는 이전 시간대와 비교해줘
```

---

## 6. Extension / Deferred

Extension은 Core 완료 후 필요에 따라 추가할 수 있는 범위다. Deferred는 관계 또는 데이터가 충분히 확인될 때까지 활성화를 보류하는 범위다.

### 6.1 CEI 5분 통계 — Deferred

| 테이블 | 상태 | 사유 |
|---|---|---|
| `PM_CEI_PGW_5M` | Deferred | `PGW_ID`가 Core Master와 동일한 ID 체계인지 미확정 |
| `PM_CEI_SGW_5M` | Deferred | 현재 데이터가 없어 관계 검증 불가 |

`PM_CEI_PGW_5M.PGW_ID`의 관측값은 `3`, `9`, `10`, `13`, `14` 등이고, `CL_PGW.PGW_ID`의 관측값은 `0037`, `0038`, `0201`, `0091` 등이다. 이는 단순히 leading zero를 제거하거나 추가하면 일치한다고 볼 수 없다. 다른 ID namespace 또는 별도 매핑 규칙의 가능성이 있으므로 직접 Join하지 않는다.

### 6.2 EPC Application 통계 — Extension

```text
PM_EPC_APP_ATT_WN_1M
PM_EPC_APP_HTTP_WN_1M
PM_EPC_APP_TCP_WN_1M
PM_EPC_APP_THR_WN_1M
PM_EPC_APP_TTT_WN_1M
APP_CODE
APP_PROTOCOL
APP_SERVICE_CD
```

Application/Protocol 품질은 유효한 확장 영역이지만, 현재 PoC의 KPI → Cause → Link 연속 질의 검증에는 필수적이지 않다. Application 시나리오가 확정될 때 Fact와 Reference를 함께 추가한다.

### 6.3 부가 EPC Master — Extension

```text
CL_MME_GRP
CL_EPC_IP
CL_EPC_IP3
CL_EPC_SW
```

Group, IP, 다중 IP 및 Software 정보는 장비 속성 확장에 유용하지만 현재 Core 통계·원인 분석에는 필수적이지 않다. 구체적인 질문 또는 Join 규칙이 정의될 때 추가한다.

### 6.4 Code / Cause Reference — Deferred

```text
CD_CALL_TYPE
CD_CAUSE
CAUSE_DEF
```

실제 Cause 데이터의 `CALL_TYPE`, `CAUSE_TYPE`, `CAUSE`, `DESCRIPTION`에는 다음과 같이 사람이 직접 해석할 수 있는 값이 포함되어 있다.

```text
CALL_TYPE   : ATTACH, PAGING, SRMO, TAU, SRMT, ...
CAUSE_TYPE  : S1AP, EMM, ESM, DIAMETER, GTPv2-S11, GTPv2-S10, ...
CAUSE       : C_02000121, ...
DESCRIPTION : RADIO_CONNECTION_WITH_UE_LOST, SYSTEM FAILURE,
              NETWORK_FAILURE, ...
```

따라서 초기 자연어 원인 분석은 별도 Code Master Join 없이 수행할 수 있다. 향후 다국어 표시, 설명 표준화, 상위 Cause 분류 또는 추가 메타데이터가 필요할 때 Reference 테이블을 다시 검토한다.

---

## 7. PoC 비활성 범위

다음 영역은 현재 PoC DDL에서 기본적으로 제외한다.

- ENB, CELL, Radio 및 5G Access 통계·Master
- 가입자 단위 Raw/XDR 데이터
- Alarm/FM 데이터
- 사용자, 권한 및 UI 설정 데이터
- EPC Core와 직접 관계가 확인되지 않은 기타 Network Master
- 개발, 백업, 테스트 및 임시 테이블

이는 영구 삭제 결정이 아니다. EPC Core 검증 완료 후 동일한 Domain Pack 및 Relationship 설계 방식으로 점진적으로 추가할 수 있다.

---

## 8. 구현 및 관리 원칙

1. 원본 DDL은 수정하지 않고 별도로 보존한다.
2. PoC용 DDL에는 Core 11개 테이블을 기본 포함한다.
3. Extension/Deferred 테이블은 명시적인 시나리오와 관계 검증 없이 Core에 합치지 않는다.
4. `NODE_ID`만으로 장비를 Join하지 않고 가능한 경우 `NODE_TYPE + NODE_ID` 복합 식별자를 사용한다.
5. Link endpoint는 EPC Equipment로 고정하지 않고 typed endpoint로 모델링한다.
6. 현재 Dataset의 결측이나 편향을 Schema 의미로 일반화하지 않는다.
7. 관계의 근거와 검증 상태는 `DB_PoC_Relationship.md`에서 관리한다.

---

## 9. 완료 기준

Database Scope 단계는 다음 조건을 만족하면 완료로 본다.

- Core 11개 테이블 DDL이 PoC Schema에 포함됨
- Core 테이블의 시간, Node 및 상세 차원 관계가 Semantic/Relationship 정의에 반영됨
- MME 0016을 대상으로 KPI → Cause/Root Cause/Detach → Link 후속 질의가 동작함
- CEI 관계를 미확정 상태로 유지하고 자동 Join 대상에서 제외함
- 현재 Dataset 제약이 테스트 결과 및 시연 문서에 명시됨

이후 작업은 이 Scope를 기반으로 Domain Pack, `relationships.yaml`, Metric 정의 및 QueryForge E2E 검증으로 진행한다.
