# DataLens PoC Database Relationship Analysis

## 1. 문서 목적

본 문서는 DataLens PoC Core Schema의 테이블 관계, 관계 판단 근거, 실데이터 검증 결과 및 구현 시 주의사항을 정의한다.

`DB_PoC.md`가 “어떤 테이블을 PoC에서 사용할 것인가”를 정의한다면, 본 문서는 “그 테이블들을 어떤 조건으로 연결하며 어디까지 확인되었는가”를 설명한다.

분석의 중심 흐름은 다음과 같다.

```text
EPC KPI
  ├─ Equipment Master
  ├─ Cause / Root Cause / Detach Detail
  └─ Link KPI
       └─ Link Root Cause / Detach Detail
```

현재 Dataset은 MME 0016 중심의 작은 과거 표본이다. 따라서 Schema상 지원 관계와 실데이터로 직접 확인한 관계를 구분한다.

---

## 2. 관계 상태 정의

| 상태 | 의미 |
|---|---|
| `CONFIRMED` | 실제 데이터 값의 일치 또는 명확한 구조로 현재 Dataset에서 확인됨 |
| `STRUCTURAL` | 컬럼 구조와 업무 의미상 관계가 명확하지만 모든 장비 유형을 실데이터로 확인하지 못함 |
| `PARTIAL` | 관계의 일부 방향, 일부 장비 유형 또는 일부 조건만 실데이터로 확인됨 |
| `UNRESOLVED` | 후보 관계는 있으나 ID 체계 또는 Join 규칙을 확정할 수 없음 |
| `DEFERRED` | PoC Core에서 사용하지 않으며 추가 데이터나 시나리오가 생길 때 검증함 |
| `NOT REQUIRED` | 현재 Core 질의에는 Join이 필요하지 않음 |

`CONFIRMED`는 현재 확인한 값 범위에서의 판정이다. 운영 전체 데이터의 무결성, 유일성 및 모든 Node Type에 대한 일치를 보증하지 않는다.

---

## 3. 분석 대상

### 3.1 Core 11개 테이블

```text
PM_EPC_KPI_1M
PM_EPC_CAUSE_1M
PM_EPC_ROOT_CAUSE_1M
PM_EPC_DETACH_DETAIL_1M
PM_LINK_EPC_KPI_1M
PM_LINK_EPC_ROOT_CAUSE_1M
PM_LINK_EPC_DETACH_DETAIL_1M
CM_EPC_INFO
CL_MME
CL_SGW
CL_PGW
```

### 3.2 본 문서에서 함께 평가하는 Deferred 후보

```text
PM_CEI_PGW_5M
PM_CEI_SGW_5M
CD_CALL_TYPE
CD_CAUSE
CAUSE_DEF
```

Application 및 부가 Master는 현재 Core 관계망에서 제외한다.

---

## 4. 관계 요약

| 출발 | 도착 | 후보 Join 조건 | 상태 | 핵심 근거 |
|---|---|---|---|---|
| `PM_EPC_KPI_1M` | `CM_EPC_INFO` | `NODE_TYPE=EQUIP_TYPE` AND `NODE_ID=EQUIP_ID` | `CONFIRMED` | `MME/0016` 실데이터 일치 |
| `PM_EPC_KPI_1M` | EPC Detail 3종 | Node + 시간 + 공통 차원 | `STRUCTURAL` | KPI에서 동일 문맥의 상세 분석 |
| `PM_EPC_KPI_1M` | `PM_LINK_EPC_KPI_1M` | EPC Node가 Link의 NODE1 또는 NODE2와 일치 | `CONFIRMED` | Link의 `NODE2=MME/0016` 확인 |
| `PM_LINK_EPC_KPI_1M` | Link Detail 2종 | Link endpoint + 시간 + 공통 차원 | `STRUCTURAL` | 동일 Link 문맥의 상세 분석 |
| `CM_EPC_INFO` | `CL_MME` | Type별 ID 관계 | `PARTIAL` | MME 4자리 ID 체계 확인 |
| `CM_EPC_INFO` | `CL_PGW` | Type별 ID 관계 | `STRUCTURAL` | Schema 지원, 현재 KPI 실데이터 미확인 |
| `CM_EPC_INFO` | `CL_SGW` | Type별 ID 관계 | `DEFERRED` | 현재 `CL_SGW` 데이터 없음 |
| `PM_CEI_PGW_5M` | `CL_PGW` | `PGW_ID` 후보 | `UNRESOLVED/DEFERRED` | 관측 ID 집합이 단순 정규화로 일치하지 않음 |
| `PM_CEI_SGW_5M` | `CL_SGW` | `SGW_ID` 후보 | `DEFERRED` | 양쪽 관련 데이터 없음 |
| Cause Fact | Code/Reference | Call/Cause 후보 키 | `NOT REQUIRED` | Fact 자체에 해석 가능한 값과 설명 존재 |

---

## 5. 중심 Fact와 Equipment Master

### 5.1 관계 정의

```text
PM_EPC_KPI_1M.NODE_TYPE = CM_EPC_INFO.EQUIP_TYPE
PM_EPC_KPI_1M.NODE_ID   = CM_EPC_INFO.EQUIP_ID
```

관계 상태: `CONFIRMED`

실데이터에서 다음 값이 정확히 일치했다.

```text
PM_EPC_KPI_1M
  NODE_TYPE = MME
  NODE_ID   = 0016

CM_EPC_INFO
  EQUIP_TYPE = MME
  EQUIP_ID   = 0016
  EQUIP_NAME = MME_16
  NETWORK    = 4G
```

따라서 MME 0016에 대해서는 KPI Fact의 범용 Node 식별자를 `CM_EPC_INFO`의 장비 정보로 해석할 수 있다.

### 5.2 구현 규칙

`NODE_ID = EQUIP_ID`만으로 Join하지 않는다. 서로 다른 장비 유형이 동일한 ID 문자열을 사용할 가능성이 있으므로 `NODE_TYPE + NODE_ID`와 `EQUIP_TYPE + EQUIP_ID`를 복합 조건으로 사용한다.

개념적인 Join은 다음과 같다.

```sql
SELECT ...
FROM PM_EPC_KPI_1M k
JOIN CM_EPC_INFO e
  ON e.EQUIP_TYPE = k.NODE_TYPE
 AND e.EQUIP_ID   = k.NODE_ID
```

실제 Query 생성 시 DB의 대소문자, 공백, 문자형 길이 및 Collation은 DDL에 맞춰야 한다. 근거 없이 숫자 변환, zero-padding 제거 또는 대소문자 변환을 적용하지 않는다.

### 5.3 확인 범위와 한계

직접 확인한 Node Type은 MME이고 Node ID는 0016 하나다. PGW와 SGW에 같은 규칙이 적용되는 것은 Schema상 강하게 지지되지만 현재 KPI Dataset으로 직접 확인하지 못했다. 따라서 관계 정의는 범용으로 유지하되 테스트 증거에는 MME만 명시한다.

---

## 6. Equipment Master 계층

`CM_EPC_INFO`는 범용 EPC 장비 Master이고, `CL_MME`, `CL_SGW`, `CL_PGW`는 유형별 세부 Master로 취급한다.

권장 탐색 순서는 다음과 같다.

```text
Fact의 NODE_TYPE + NODE_ID
  → CM_EPC_INFO의 EQUIP_TYPE + EQUIP_ID
  → 장비 유형에 따라 CL_MME / CL_SGW / CL_PGW 확장
```

### 6.1 MME

현재 데이터에서 `PM_EPC_KPI_1M`과 `CM_EPC_INFO`의 `MME/0016` 일치가 확인되었고 `CL_MME`도 4자리 MME ID 체계를 사용한다. 따라서 MME 세부 Master 확장 가능성은 높다. 다만 본 검증에서 `CL_MME`의 0016 행 전체와 모든 부가 컬럼의 의미까지 확인한 것은 아니므로 공통 Master 관계와 유형별 세부 관계를 구분한다.

### 6.2 PGW

`CL_PGW`에는 `0037`, `0038`, `0201`, `0091`과 같은 4자리 ID가 관측되었다. 이는 일반 EPC Master의 PGW 세부 정보로 사용할 수 있는 Schema 구조를 지지하지만, 현재 `PM_EPC_KPI_1M`에 PGW 행이 없어 Fact → PGW 실제 Join은 검증하지 못했다.

### 6.3 SGW

`CL_SGW`는 Schema에 존재하지만 현재 Dataset에는 데이터가 없다. 이는 SGW 지원을 제거할 근거가 아니다. 관계는 Schema 지원 대상으로 유지하며, 실제 SGW 데이터가 적재될 때 다음을 확인한다.

- `CM_EPC_INFO(EQUIP_TYPE='SGW', EQUIP_ID)`와 `CL_SGW.SGW_ID` 일치
- Fact의 `NODE_TYPE='SGW'`, `NODE_ID`와 공통 Master 일치
- ID 길이, leading zero 및 중복 여부

---

## 7. EPC KPI에서 상세 원인으로의 Drill-down

### 7.1 대상 관계

```text
PM_EPC_KPI_1M
  ├─ PM_EPC_CAUSE_1M
  ├─ PM_EPC_ROOT_CAUSE_1M
  └─ PM_EPC_DETACH_DETAIL_1M
```

이 관계의 목적은 하나의 Fact 행을 물리적인 FK로 연결하는 것이 아니라, 동일한 업무 문맥을 더 상세한 Grain에서 조회하는 것이다.

일반적으로 유지해야 하는 문맥은 다음과 같다.

- `NODE_TYPE`
- `NODE_ID`
- `EVENT_TIME` 또는 동일 집계 시간 구간
- `CALL_TYPE` 등 양쪽에 공통으로 존재하는 업무 차원
- 질문에서 선택된 지역, 사업자 또는 그룹 차원

정확한 Join Key는 각 테이블의 DDL에 존재하는 공통 컬럼만 사용해야 한다. Detail 테이블의 Grain이 Cause, Root Cause 또는 Detach 속성으로 더 세분화되므로, KPI와 Detail을 행 단위 1:1 관계로 가정해서는 안 된다.

### 7.2 안전한 Query 방식

다음 방식을 권장한다.

1. KPI 질의에서 대상 Node와 시간 구간을 결정한다.
2. 선택된 필터 문맥을 Detail 질의에 전달한다.
3. Detail Fact를 Cause/Root Cause/Detach 차원으로 재집계한다.
4. Detail 행을 KPI 행에 직접 붙여 Metric 중복을 일으키지 않는다.

즉, 이는 “Join 후 한 번에 집계”보다 “문맥을 이어받은 후속 Query”에 가까운 관계다.

---

## 8. Cause 데이터와 Code/Reference

### 8.1 실데이터 확인 결과

Cause 계열 데이터에는 다음과 같이 사람이 해석 가능한 값이 이미 포함되어 있다.

```text
CALL_TYPE
  ATTACH, PAGING, SRMO, TAU, SRMT, ...

CAUSE_TYPE
  S1AP, EMM, ESM, DIAMETER, GTPv2-S11, GTPv2-S10, ...

CAUSE
  C_02000121, ...

DESCRIPTION
  RADIO_CONNECTION_WITH_UE_LOST
  SYSTEM FAILURE
  NETWORK_FAILURE
  ...
```

### 8.2 판정

| 테이블 | 판정 | 이유 |
|---|---|---|
| `CD_CALL_TYPE` | `NOT REQUIRED` | Fact의 `CALL_TYPE`이 이미 의미 있는 문자열 |
| `CD_CAUSE` | `NOT REQUIRED` | Cause 유형과 설명을 Fact에서 해석 가능 |
| `CAUSE_DEF` | `NOT REQUIRED` | `DESCRIPTION`이 사용자 설명에 활용 가능 |

따라서 이 세 테이블은 Core에서 제외한다. 이는 테이블이 무의미하다는 뜻이 아니라, 현재 PoC의 자연어 원인 분석에 추가 Join이 필수적이지 않다는 뜻이다.

향후 다음 요구가 생기면 Extension으로 복원할 수 있다.

- 다국어 Cause 이름
- 표준화된 상위/하위 Cause 분류
- 코드 유효기간 또는 버전 관리
- Fact에 없는 운영 설명과 조치 가이드

---

## 9. Link 관계 모델

### 9.1 기존 가정의 수정

`PM_LINK_EPC_KPI_1M`을 EPC 장비 간 링크만 표현하는 테이블로 정의해서는 안 된다. 실데이터에서 다음 구조가 확인되었다.

```text
NODE1_TYPE = VLAN
NODE1_ID   = 3_GigabitEthernet0/3/4

NODE2_TYPE = MME
NODE2_ID   = 0016

LINK_TYPE  = S1MME
```

추가로 VLAN endpoint의 ID에는 `2_4/1/19`, `3/1/14`, `2_3/12` 등 Interface 또는 Resource 이름 형태의 값이 관측되었다.

### 9.2 올바른 개념 모델

```text
Typed Endpoint 1                      Typed Endpoint 2
(EPC 또는 Network Resource)  ↔  (EPC 또는 Network Resource)
```

EPC Node는 `NODE1` 또는 `NODE2` 어느 쪽에도 나타날 수 있다. 반대편 endpoint가 `VLAN`, Interface 또는 다른 Network Resource일 수 있으므로 다음 가정을 금지한다.

- `NODE1`은 항상 상위 EPC 장비다.
- `NODE2`는 항상 하위 EPC 장비다.
- 양쪽 endpoint는 모두 `CM_EPC_INFO`에 존재한다.
- `NODE1_ID`와 `NODE2_ID`는 동일한 ID 형식을 사용한다.
- Link는 반드시 MME ↔ SGW 또는 SGW ↔ PGW다.

### 9.3 EPC Node와 Link의 연결 조건

EPC Node가 어느 endpoint에 있는지 양쪽을 모두 확인해야 한다.

```text
(KPI.NODE_TYPE = LINK.NODE1_TYPE AND KPI.NODE_ID = LINK.NODE1_ID)
OR
(KPI.NODE_TYPE = LINK.NODE2_TYPE AND KPI.NODE_ID = LINK.NODE2_ID)
```

현재 데이터에서는 다음 두 번째 조건이 성립한다.

```text
KPI.NODE_TYPE = MME  = LINK.NODE2_TYPE
KPI.NODE_ID   = 0016 = LINK.NODE2_ID
```

관계 상태는 MME 0016에 대해 `CONFIRMED`다.

### 9.4 권장 모델링

Semantic Layer에서는 endpoint를 다음과 같은 typed reference로 취급한다.

```text
endpoint_1 = { type: NODE1_TYPE, id: NODE1_ID }
endpoint_2 = { type: NODE2_TYPE, id: NODE2_ID }
```

EPC Master Join은 endpoint의 `type`이 EPC Equipment Type으로 해석 가능한 경우에만 수행한다. `VLAN`과 같은 endpoint를 `CM_EPC_INFO`에 강제로 Join하지 않는다.

Query 구현 시 OR Join은 중복 및 성능 문제를 만들 수 있으므로, 필요하다면 다음과 같이 endpoint를 정규화한 논리 View 또는 두 분기 `UNION ALL`을 사용한다.

```text
link_id | endpoint_no | endpoint_type | endpoint_id | peer_type | peer_id
```

이 구조는 “MME 0016에 연결된 모든 Link와 상대 endpoint”를 방향과 무관하게 조회하기 쉽다.

---

## 10. Link KPI에서 Link 상세로의 Drill-down

```text
PM_LINK_EPC_KPI_1M
  ├─ PM_LINK_EPC_ROOT_CAUSE_1M
  └─ PM_LINK_EPC_DETACH_DETAIL_1M
```

Link 상세 관계에서도 `NODE1`과 `NODE2`의 방향을 임의로 바꾸지 않는다. 양쪽 테이블에 존재하는 다음 문맥을 사용해 후속 조회한다.

- `NODE1_TYPE`, `NODE1_ID`
- `NODE2_TYPE`, `NODE2_ID`
- `LINK_TYPE`
- 시간 구간
- 양쪽에 공통으로 존재하는 Call/업무 차원

Link KPI와 Detail의 Grain이 다를 수 있으므로 EPC Detail과 마찬가지로 문맥 기반 후속 Query를 우선한다. endpoint 순서가 서로 다른 행을 동일 Link로 취급해야 하는지는 실제 DDL의 Link 식별자와 추가 데이터 검증 전까지 자동으로 가정하지 않는다.

---

## 11. CEI 관계 분석

### 11.1 PGW CEI

후보 관계는 다음과 같다.

```text
PM_CEI_PGW_5M.PGW_ID ↔ CL_PGW.PGW_ID
```

그러나 관측값은 다음과 같다.

```text
PM_CEI_PGW_5M.PGW_ID : 3, 9, 10, 13, 14, ...
CL_PGW.PGW_ID         : 0037, 0038, 0201, 0091, ...
```

예를 들어 `3`을 `0003`으로 정규화해도 관측된 `0037`과 같지 않다. 두 집합을 단순 leading-zero 차이로 설명할 수 없으므로 다음 가능성을 열어둔다.

- 서로 다른 ID namespace
- 내부 순번과 장비 ID의 차이
- 별도 매핑 테이블 필요
- 일부 데이터만 적재되어 교집합이 보이지 않음

판정은 `UNRESOLVED/DEFERRED`다. 다음 동작을 금지한다.

- 숫자 변환 후 자동 Join
- 임의 zero-padding 후 자동 Join
- `PGW_ID`라는 컬럼명만 근거로 관계 확정

### 11.2 SGW CEI

`PM_CEI_SGW_5M`과 `CL_SGW`는 현재 관련 실데이터가 없어 값 기반 검증을 수행할 수 없다. 판정은 `DEFERRED`다. Schema 정의는 유지하되 Core 관계 및 자동 Join에서는 제외한다.

### 11.3 재검증 조건

CEI를 활성화하려면 다음 중 하나 이상의 근거가 필요하다.

- 공식 ID 매핑 규칙
- CEI와 Master 양쪽에 공통으로 나타나는 충분한 표본
- 별도 Mapping 테이블 또는 변환 로직
- 운영 담당자의 ID 의미 확인

---

## 12. 현재 Dataset 제약

확인된 `PM_EPC_KPI_1M` 데이터는 다음과 같다.

```text
NODE_TYPE : MME
NODE_ID   : 0016
ROW_COUNT : 140
NODE_COUNT: 1
MIN_TIME  : 2024-05-29 11:20:00
MAX_TIME  : 2024-05-30 11:29:00
```

SGW 관련 데이터는 현재 확인되지 않았다. 특히 `CL_SGW`와 `PM_CEI_SGW_5M`이 비어 있어 SGW 관계를 실데이터로 검증할 수 없다.

이 Dataset으로 확인한 내용과 확인하지 못한 내용을 구분하면 다음과 같다.

| 구분 | 결과 |
|---|---|
| MME 0016 KPI → 공통 EPC Master | 확인 완료 |
| MME 0016 KPI → Link endpoint | 확인 완료 |
| Link의 상대 endpoint가 VLAN/Interface일 수 있음 | 확인 완료 |
| Cause 컬럼의 사람이 해석 가능한 값 | 확인 완료 |
| PGW/SGW KPI → 공통 EPC Master | 현재 데이터로 미확인 |
| SGW 유형별 Master 관계 | 현재 데이터로 미확인 |
| CEI PGW → PGW Master | 불일치 양상 확인, 관계 미확정 |
| CEI SGW → SGW Master | 데이터 없어 미확인 |

현재 데이터의 적은 양과 오래된 시점은 관계의 예시 확인에는 사용할 수 있지만, 운영 대표성이나 Metric 품질을 판단하는 근거로 사용해서는 안 된다.

---

## 13. 실데이터 검증 결과

기존의 “추가 검증 예정” 항목은 아래와 같이 실행 결과 및 판정으로 대체한다.

### 13.1 검증 A — EPC KPI와 Equipment Master

목적: `NODE_TYPE + NODE_ID`가 공통 Equipment Master와 실제로 일치하는지 확인

결과:

```text
PM_EPC_KPI_1M : MME / 0016
CM_EPC_INFO    : MME / 0016 / MME_16 / 4G
```

판정: `CONFIRMED`

제한: MME 0016만 직접 확인했으며 PGW/SGW 일반화는 추가 데이터가 필요함

### 13.2 검증 B — Cause 데이터의 해석 가능성

목적: 별도 Code/Reference Join이 Core에 필수인지 확인

결과:

- `CALL_TYPE`은 ATTACH, PAGING, SRMO, TAU 등 의미 있는 값
- `CAUSE_TYPE`은 S1AP, EMM, ESM, DIAMETER, GTPv2 계열 등 의미 있는 값
- `CAUSE`는 코드형 값
- `DESCRIPTION`은 사람이 이해 가능한 원인 설명

판정: `CD_CALL_TYPE`, `CD_CAUSE`, `CAUSE_DEF`는 Core에서 `NOT REQUIRED`

### 13.3 검증 C — Dataset 분포

목적: 현재 데이터의 장비 유형, 규모 및 기간 확인

결과:

```text
MME 0016 한 대
140건
2024-05-29 11:20:00 ~ 2024-05-30 11:29:00
```

판정: MME 중심 시나리오는 가능하나 다중 장비 및 PGW/SGW 비교는 현재 Dataset으로 제한됨

### 13.4 검증 D — CEI와 Master ID

목적: CEI 5분 통계의 장비 ID가 유형별 Master와 Join 가능한지 확인

결과:

```text
CEI PGW ID : 3, 9, 10, 13, 14, ...
CL_PGW ID  : 0037, 0038, 0201, 0091, ...
```

판정: 단순 leading-zero normalization으로 설명할 수 없으므로 `UNRESOLVED/DEFERRED`

SGW는 관련 데이터가 없어 `DEFERRED`

### 13.5 검증 E — EPC Link endpoint

목적: Link가 EPC 장비 간 관계만 표현하는지 확인

결과:

```text
NODE1 = VLAN / 3_GigabitEthernet0/3/4
NODE2 = MME  / 0016
LINK_TYPE = S1MME
```

판정: Link는 typed endpoint 관계이며 상대 endpoint가 VLAN/Interface 등 Network Resource일 수 있음. EPC Node는 NODE1 또는 NODE2 중 어느 쪽에도 참여할 수 있도록 모델링해야 함

### 13.6 검증 F — SGW 데이터 유무

목적: SGW 관계의 실데이터 검증 가능 여부 확인

결과: `CL_SGW` 및 `PM_CEI_SGW_5M`에서 데이터가 확인되지 않음

판정: 현재 Dataset 제약. Schema 지원은 유지하고 실제 관계 검증만 보류

---

## 14. Semantic/Relationship 구현 지침

### 14.1 관계 선언 원칙

1. 공통 EPC Equipment 관계는 복합 키를 사용한다.
2. Detail Fact 관계는 1:1 FK가 아니라 필터 문맥 전파로 모델링한다.
3. Link는 `NODE1`과 `NODE2` 양쪽을 대칭적으로 탐색한다.
4. endpoint type이 EPC 유형일 때만 Equipment Master Join을 허용한다.
5. CEI 관계는 검증 전까지 Relationship registry에 활성 관계로 등록하지 않는다.
6. Cause Reference Join은 현재 Core Query Plan에 자동 삽입하지 않는다.
7. 현재 데이터에 없는 SGW를 Schema에서 제거하거나 MME 전용으로 고정하지 않는다.

### 14.2 권장 관계 표현

개념적으로 다음 관계를 등록할 수 있다.

```yaml
relationships:
  - name: epc_kpi_to_equipment
    status: confirmed
    from: PM_EPC_KPI_1M
    to: CM_EPC_INFO
    on:
      - NODE_TYPE = EQUIP_TYPE
      - NODE_ID = EQUIP_ID

  - name: epc_kpi_to_link_endpoint_1
    status: confirmed_by_typed_endpoint_model
    from: PM_EPC_KPI_1M
    to: PM_LINK_EPC_KPI_1M
    on:
      - NODE_TYPE = NODE1_TYPE
      - NODE_ID = NODE1_ID

  - name: epc_kpi_to_link_endpoint_2
    status: confirmed
    from: PM_EPC_KPI_1M
    to: PM_LINK_EPC_KPI_1M
    on:
      - NODE_TYPE = NODE2_TYPE
      - NODE_ID = NODE2_ID
```

이는 개념 예시이며 실제 `relationships.yaml`의 스키마에 맞춰 변환해야 한다. endpoint 1 관계는 모델 구조상 필요하지만 현재 실데이터에서 직접 확인된 것은 endpoint 2의 MME 0016이다.

### 14.3 중복 집계 방지

KPI Fact와 Cause/Link Detail을 한 Query에서 무조건 Join하면 1:N 확장으로 KPI Metric이 중복될 수 있다. 다음 중 하나를 사용한다.

- Fact별 사전 집계 후 Join
- Node/시간 문맥을 전달한 별도 Query
- 존재 여부만 필요한 경우 `EXISTS`
- endpoint 정규화 View에서 명확한 Grain 유지

---

## 15. 권장 검증 시나리오

현재 Dataset 기준 E2E 검증 순서는 다음과 같다.

```text
1. MME/0016의 시간대별 KPI 조회
2. CM_EPC_INFO에서 MME_16 장비명 해석
3. 저성능 시간 구간 선택
4. 동일 Node/시간 문맥으로 Cause 또는 Root Cause 조회
5. MME/0016이 NODE1 또는 NODE2인 Link 조회
6. 상대 VLAN/Interface endpoint 표시
7. 선택한 Link의 Root Cause 또는 Detach Detail 조회
```

기대되는 사용자 대화는 다음과 같다.

```text
“MME_16의 KPI를 보여줘.”
“가장 낮았던 시간은 언제야?”
“그때 주요 실패 원인은?”
“이 MME에 연결된 링크를 보여줘.”
“그중 문제가 큰 링크의 원인은?”
```

---

## 16. 추가 데이터 확보 시 재검증 항목

PoC 직전에 데이터가 추가되면 다음 순서로 관계를 재검증한다.

1. 여러 MME의 Fact ↔ `CM_EPC_INFO` 일치율
2. PGW Fact ↔ `CM_EPC_INFO` ↔ `CL_PGW` 연결
3. SGW Fact ↔ `CM_EPC_INFO` ↔ `CL_SGW` 연결
4. `NODE1`에 EPC Node가 위치한 Link 표본
5. 여러 Link Type의 endpoint type 분포
6. Link KPI와 Link Detail 간 공통 Grain 및 중복 여부
7. CEI ID의 공식 의미와 Master 매핑 규칙
8. Null, 공백, 대소문자, leading zero 및 중복 ID 품질

재검증 결과가 현재 판정과 다르면 본 문서의 상태와 근거를 함께 갱신한다.

---

## 17. 최종 결론

현재 PoC의 중심 관계는 `PM_EPC_KPI_1M.NODE_TYPE/NODE_ID`와 `CM_EPC_INFO.EQUIP_TYPE/EQUIP_ID`의 복합 관계이며, MME 0016으로 실데이터 일치가 확인되었다.

Cause 계열 Fact는 사람이 해석 가능한 Call/Cause 유형과 설명을 포함하므로 별도 Code/Reference 없이 Core 원인 분석을 수행할 수 있다.

Link는 EPC↔EPC로 한정되지 않는다. EPC Node와 VLAN/Interface 같은 Network Resource 사이의 typed endpoint 관계로 모델링해야 하며, EPC Node가 `NODE1` 또는 `NODE2` 어느 쪽에 있는지 모두 탐색해야 한다.

CEI의 PGW ID는 유형별 Master ID와 단순 정규화로 연결할 수 없으므로 관계를 확정하지 않는다. SGW는 현재 데이터가 없지만 이는 Dataset 제약이며 Schema 지원 대상에서는 유지한다.

이 결론을 기준으로 Core 11개 테이블의 Semantic/Relationship 정의를 진행하고, CEI, Application, 부가 Master 및 Code/Reference는 필요한 근거가 확보될 때 단계적으로 활성화한다.
