# DataLens Architecture Review
## WrenAI 분석 기반 개선 및 확장 제안

> ## ⚠ 문서 상태: 제안 (Proposal) — 판정 완료
>
> **본 문서는 확정 설계가 아니다.** 본문의 `ACCEPT` / `EXTEND` / `NEW` 등 표기는 **제안자의 의견**이며
> 최종 판정이 아니다. 구현 근거로 사용해서는 안 된다.
>
> | 항목 | 내용 |
> |---|---|
> | 상태 | Proposal / 판정 완료 |
> | 판정 문서 | `SEM-REVIEW-001.md` |
> | 확정 결정 | `ADR.md` ADR-020 ~ ADR-027, ADR-009 개정, ADR-010 개정 |
> | 검토일 | 2026-08-10 |
>
> ### 판정 요약 (제안 35건)
>
> | 판정 | 건수 | 대표 항목 |
> |---|---|---|
> | **ACCEPT** | 6 | 제안5 Identifier Heuristic, 제안11 Vector≠Truth, 제안13 QuerySpec 중심 Memory, 제안22·23 |
> | **MODIFY** | 11 | 제안1·6 Catalog 역할, 제안7 Lifecycle 2상태, 제안12 기록만, 제안16 Scope 2단계, 제안25 반환타입 확장 |
> | **DEFER** | 8 | 제안9·10·24 Schema Memory, 제안19 Seed Query, 제안30 Embedding 추상화 |
> | **REJECT** | 5 | **제안2** Catalog SoT, **제안8** General Lexicon, **제안17** Semantic Learner, **제안35** 9단계 Pipeline |
> | **변경 없음** | 5 | 제안26~29·31 (기존 결정 재확인) |
>
> ### 특히 주의할 기각 항목
>
> - **제안2 (semantic.yaml → Catalog가 SoT)** — ADR-009의 CI 도메인 용어 검사가 소실되고 FR-043이 붕괴한다.
>   Domain Pack이 SoT로 유지된다 (ADR-009 개정).
> - **제안8 (General Lexicon)** — 폐쇄망 조달·상용 라이선스 리스크·업계 은어에 무효.
>   결정적 표기 정규화로 대체 (ADR-021).
> - **제안17 (Semantic Learner)** — "반복 사용 성공"은 의미 정확성과 무상관이다.
>   의미 오류는 항상 에러 없이 실행되므로 침묵이 긍정 증거로 계수된다 (ADR-020, N-16).
> - **제안35 (9단계 Retrieval Pipeline)** — ADR-017 Semantic 예산 1,000토큰을 크게 초과하고
>   LLM 호출이 3→4회로 늘어 NFR-001을 위협한다 (ADR-010 개정).
>
> ### 본 문서 이후 확인된 사실 (판정 근거)
>
> - **PoC Core Scope = 11개 테이블** (`DB_PoC.md`) → Schema Retrieval 벡터 검색 불필요
> - **복합 관계가 PoC 필수 요구** (`DB_PoC_Relationship.md` §5) → ADR-027로 승격
> - **현재 Dataset = MME/0016 단일 노드 140행** → SAD 3.1 대표 시나리오 개정
> - **WrenAI는 혼합 라이선스** → ADR-025, 파일 단위 확인 의무

| 항목 | 내용 |
|---|---|
| 문서 목적 | 기존 DataLens 설계와 WrenAI 분석 결과를 비교하여 향후 개선/확장 아이디어를 제안 |
| 대상 문서 | `ADR.md`, `DATALENS-SAD.md`, `GLOSSARY.md` |
| 참고 구현 | WrenAI |
| 문서 성격 | Architecture Review / Improvement Proposal |
| 적용 상태 | 제안(Proposal) |
| 작성일 | 2026-08-10 |

---

# 1. 문서 목적

본 문서는 현재 DataLens / QueryForge 설계를 변경하기 위한 확정 설계서가 아니다.

현재 작성되어 있는 다음 문서를 기준 설계로 유지한다.

- `ADR.md`
- `DATALENS-SAD.md`
- `GLOSSARY.md`

그리고 WrenAI 소스코드 분석 및 DataLens PoC DB 분석 과정에서 확인한 아이디어를 바탕으로 다음을 정리한다.

1. 현재 설계에서 그대로 유지할 부분
2. 기존 구조를 확장하면 좋을 부분
3. WrenAI 구현을 레퍼런스로 사용할 수 있는 부분
4. 기존 설계를 재검토할 가치가 있는 부분
5. 새롭게 추가할 수 있는 DataLens 고유 기능
6. 향후 ADR / SAD / GLOSSARY 개정 시 검토할 항목

핵심 원칙은 다음과 같다.

> WrenAI 구조를 DataLens에 이식하는 것이 목적이 아니다.

WrenAI에서 검증된 아이디어와 구현 패턴을 참고하되,
DataLens의 기존 설계 원칙과 목표를 유지하면서 필요한 부분만 선택적으로 적용한다.

특히 다음 기존 원칙은 최대한 보존한다.

- DataLens Agent가 판단한다.
- QueryForge는 업무 의미를 판단하지 않는다.
- QueryForge는 Domain-neutral Data Execution Engine이다.
- Agent와 QueryForge 사이에는 Query Specification을 사용한다.
- Dataset의 실제 데이터는 QueryForge가 소유한다.
- 멀티턴 분석은 Dataset Lineage와 Session Context를 사용한다.
- MCP Tool은 제한된 범용 기능으로 유지한다.
- DB에 직접 SQL을 생성하여 실행하는 방식보다 구조화된 QuerySpec을 우선한다.

---

# 2. 이번 검토의 배경

초기 DataLens 설계에서는 Semantic Layer를 다음과 같은 구조로 생각했다.

```text
Domain Pack
 ├─ semantic.yaml
 ├─ relationships.yaml
 ├─ partitions.yaml
 ├─ locale.yaml
 └─ examples.jsonl
```

특히 `semantic.yaml`에는 다음과 같은 정보가 포함될 수 있었다.

```text
alias
entity mapping
metric
KPI
business terminology
```

이 방식은 PoC를 빠르게 구현하기에는 적절하다.

그러나 실제 운영 시스템으로 확장할 경우 다음 문제가 예상된다.

```text
사용자 표현 증가
        ↓
Alias 증가
        ↓
Domain Pack 증가
        ↓
수동 유지보수 증가
        ↓
Semantic 충돌
        ↓
운영 복잡도 증가
```

예를 들어 다음 표현을 모두 사람이 등록해야 하는 구조가 될 수 있다.

```text
성공률
성공 비율
성공율
성공 퍼센트
소통률
호 성공률
통신 성공률
```

DataLens의 목표가 다양한 DB와 다양한 조직에서 사용할 수 있는 범용 시스템이라면,
사용자가 이러한 Alias를 지속적으로 관리하는 구조는 장기적으로 적합하지 않을 가능성이 높다.

따라서 Semantic Layer를 정적인 Dictionary 중심 구조에서
점진적으로 자동화 가능한 구조로 확장할 수 있는지 검토하였다.

이 과정에서 WrenAI 소스코드를 분석하였다.

---

# 3. WrenAI 분석에서 확인한 주요 구조

WrenAI에는 DataLens 구현 시 참고 가치가 높은 다음 구조가 존재한다.

```text
MDL
Schema Memory
Query Memory
Seed Query
Schema Indexing
generate-mdl
enrich-context
```

특히 다음 세 영역은 높은 참고 가치가 있다.

```text
1. DB Schema → Semantic Model Bootstrap

Schema → Vector Index → Relevant Schema Retrieval
NL Query ↔ SQL Query History

이는 DataLens에서 고려하고 있는 다음 구조와 상당 부분 연결된다.

```text
Automatic Semantic Bootstrap

Semantic Catalog

Schema Memory

Query Memory
Semantic Memory

다만 WrenAI 역시 Semantic Model을 완전히 자동으로 구축하고
사용자 대화를 통해 지속적으로 Semantic 자체를 발전시키는 구조를
완성된 형태로 제공하는 것은 아니다.

따라서 WrenAI는 DataLens Semantic Architecture의 대체재라기보다
강력한 Reference Implementation으로 보는 것이 적절하다.

---

# 4. WrenAI 참고 대상 소스

향후 DataLens 구현 시 다음 WrenAI 코드를 우선적으로 참고할 가치가 있다.

## Memory

```text
core/wren/src/wren/memory/store.py

core/wren/src/wren/memory/schema_indexer.py
core/wren/src/wren/memory/embeddings.py
core/wren/src/wren/memory/seed_queries.py
core/wren/src/wren/memory/index_backend.py
core/wren/src/wren/memory/markdown.py
core/wren/src/wren/memory/watch.py

특히 우선순위가 높은 파일은 다음과 같다.

```text
store.py
schema_indexer.py
seed_queries.py
embeddings.py
Automatic Model Generation
core/wren/src/wren/skills_content/generate-mdl/SKILL.md
Context Enrichment
core/wren/src/wren/skills_content/enrich-context/SKILL.md
이 코드를 그대로 복사하는 것이 목적은 아니다.
다음 내용을 확인하기 위한 Reference로 사용한다.
Schema indexing 방법

Embedding 대상 구성

Vector 검색 방법

Query history 저장 방법

Schema 변경 감지
Seed Query 생성
DB introspection
Relationship 후보 생성
Business Context 확장

---

# 5. 제안 1 — Domain Pack 역할 재정의

## 판정

```text
RECONSIDER / EXTEND
Domain Pack 자체를 제거할 필요는 없다.
다만 Domain Pack이 Semantic Source of Truth가 되는 구조는 재검토할 가치가 있다.
기존 개념:
User / Developer
       │
       ▼
Domain Pack
       │
       ▼
Semantic Provider
향후에는 다음과 같은 방향을 고려할 수 있다.
Database
   │
   ▼
Automatic Bootstrap
   │
   ▼
Semantic Catalog
   │
   ├──────────────┐
   ▼              ▼
Domain Pack     Semantic Memory
(optional)
즉 Domain Pack은 다음 역할로 축소 또는 재정의할 수 있다.
Bootstrap Seed

Curated Override

Deployment Artifact

Import / Export Format
Backup
Environment-specific Configuration

예를 들어 자동 분석 결과가 잘못된 경우에만 사람이 명시적으로 override한다.

```yaml
metric_overrides:

  success_rate:
    numerator: SUCCESS_CNT
    denominator: ATTEMPT_CNT
    aggregation: ratio_of_sums
이러한 Override는 가치가 있다.
반대로 다음과 같은 대규모 Alias 관리는 지양한다.
aliases:
  - 성공률
  - 성공비율
  - 성공 퍼센트
  - 소통률
  - ...
6. 제안 2 — semantic.yaml의 역할 변경
판정
RECONSIDER
semantic.yaml을 제거할 필요는 없다.
그러나 장기적으로 다음 구조를 고려한다.
기존:
semantic.yaml
      │
      ▼
Semantic Source of Truth
제안:
Semantic Catalog
      │
      ├── Export → semantic.yaml
      │
      └── Import ← semantic.yaml
즉 semantic.yaml은 다음 용도로 사용할 수 있다.
Export

Import
Bootstrap
Override
배포
테스트 Fixture

Semantic의 실제 상태는 구조화된 Catalog가 관리한다.

이렇게 하면 Semantic을 런타임에 자동으로 확장하거나 보정할 수 있다.

---

# 7. 제안 3 — Catalog Snapshot을 Semantic Bootstrap 기반으로 확장

## 판정

```text
EXTEND
현재 Catalog Snapshot 개념은 매우 가치가 높다.
기존 목적은 DB Metadata를 매 요청마다 읽지 않고
사전에 수집하여 사용하는 것이다.
이 구조를 Semantic Bootstrap의 입력으로 확장할 수 있다.
Database
    │
    ▼
Catalog Snapshot
    │
    ▼
Schema Profiler
    │
    ▼
Semantic Bootstrapper
    │
    ▼
Semantic Catalog
Catalog Snapshot에는 기존 Metadata 외에
선택적으로 다음 Profiling 정보를 포함할 수 있다.
Distinct Count

NULL Ratio
Min / Max
Sample Values
Cardinality
Value Pattern
Identifier Candidate
Enum Candidate

예:

```text
NODE_TYPE

type:
VARCHAR

sample:
MME
PGW
SGW
distinct_count:
3

이 정보는 다음 추론에 사용할 수 있다.

```text
NODE_TYPE
→ categorical dimension candidate
8. 제안 4 — Automatic Semantic Bootstrapper 추가
판정
NEW
DataLens에 새로운 논리 컴포넌트로 다음을 고려한다.
Semantic Bootstrapper
역할:
DB Schema와 Metadata를 분석하여 초기 Semantic Candidate를 자동 생성한다.

입력:
Catalog Snapshot

Schema

Column Name

Column Type
PK / FK
Index
Comment
Sample Values
Data Profile

출력:

```text
Entity Candidate

Dimension Candidate

Metric Candidate

Identifier Candidate
Relationship Candidate
Temporal Dimension Candidate

예:

```text
EVENT_TIME
→ temporal dimension

NODE_TYPE
→ categorical dimension

NODE_ID
→ identifier
ATTEMPT_CNT
→ metric candidate
SUCCESS_CNT
→ metric candidate

Semantic Bootstrapper는 LLM과 deterministic heuristic을 함께 사용할 수 있다.

---

# 9. 제안 5 — Identifier Heuristic 도입

## 판정

```text
NEW / WRENAI REFERENCE
WrenAI Seed Query 구현에서는 숫자형 컬럼이라고 무조건 Metric으로 처리하지 않는다.
다음과 같은 조건을 이용하여 Identifier를 제외한다.
Primary Key

Relationship Key

ID-like Column Name

DataLens에서도 유사한 규칙을 적용할 가치가 있다.

예:

```text
NODE_ID

BRANCH_ID

MTSO_ID

이들이 INTEGER 타입이라고 하더라도:

```sql
SUM(NODE_ID)
같은 Query가 생성되어서는 안 된다.
초기 규칙 후보:
PK
FK
*_ID
*_KEY
*_CODE
Relationship Key
Low-cardinality identifier pattern
이 규칙은 Semantic Bootstrapper와 Seed Query Generator에서 공통으로 사용할 수 있다.
10. 제안 6 — Semantic Catalog 도입
판정
NEW
정적인 Semantic Dictionary를 점진적으로 구조화된 Catalog로 발전시키는 방안을 고려한다.
예:
semantic_concept

semantic_entity

semantic_dimension

semantic_metric
semantic_term
semantic_relationship
semantic_feedback

각 Semantic에는 다음 Metadata를 둘 수 있다.

```text
source

confidence
status
scope
evidence
created_at
updated_at

예:

```text
name:
success_rate

type:
derived_metric

numerator:
SUCCESS_CNT
denominator:
ATTEMPT_CNT
aggregation:
ratio_of_sums
source:
schema_inference
confidence:
0.82
status:
candidate

---

# 11. 제안 7 — Semantic Lifecycle 도입

## 판정

```text
NEW
자동으로 생성된 Semantic을 즉시 확정된 Truth로 사용하면 위험하다.
따라서 Semantic 상태를 관리하는 방안을 고려한다.
예:
DISCOVERED
     ↓
CANDIDATE
     ↓
VERIFIED
     ↓
TRUSTED
또는 더 단순하게:
CANDIDATE

ACTIVE

REJECTED

각 Semantic에는 다음을 함께 관리한다.

```text
confidence

source
evidence
usage_count
success_count
failure_count

이를 통해 자동 추론 결과와 검증된 Business Rule을 구분할 수 있다.

---

# 12. 제안 8 — General Lexicon 추가

## 판정

```text
NEW
Semantic Alias를 사용자가 계속 관리하는 대신
DataLens 자체적으로 범용 언어 사전을 제공하는 방안을 고려한다.
예:
원인
 ↔ 이유
 ↔ 사유

비율
 ↔ 율
 ↔ 백분율
장비
 ↔ 기기
 ↔ 설비

다만 일반 사전의 유사어를 Business Truth로 사용해서는 안 된다.

따라서 관계 유형을 구분한다.

```text
EXACT

SYNONYM
ABBREVIATION
MORPHOLOGICAL
RELATED
OPPOSITE

그리고 다음 Metadata를 함께 관리한다.

```text
source

language

confidence

General Lexicon의 역할은:

> Semantic Candidate Discovery

이지:

> Business Meaning Definition

이 아니다.

---

# 13. 제안 9 — Schema Memory 도입

## 판정

```text
NEW / WRENAI REFERENCE
WrenAI의 Schema Memory 구조는 DataLens에서 높은 참고 가치가 있다.
Schema 정보를 Embedding하여 Vector Index에 저장한다.
개념적으로:
Schema
   │
   ▼
Text Representation
   │
   ▼
Embedding
   │
   ▼
Vector Index
사용자 질문:
MME별 성공률 보여줘
입력 시:
Question
   │
   ▼
Embedding
   │
   ▼
Schema Search
   │
   ▼
Relevant Schema
예:
PM_EPC_KPI_1M

NODE_TYPE

NODE_ID

ATTEMPT_CNT
SUCCESS_CNT

만 LLM Context 후보로 전달한다.

---

# 14. 제안 10 — Schema Retrieval과 기존 MCP Schema Tool 결합

## 판정

```text
EXTEND
Vector Retrieval이 기존 schema Tool을 대체해서는 안 된다.
다음 구조가 적절하다.
User Question
      │
      ▼
Schema Memory
      │
      ▼
Candidate Tables / Columns
      │
      ▼
TOOL-001 schema
      │
      ▼
Verified Metadata
즉 Vector Search는:
어디를 찾아볼 것인가?
를 결정하는 보조 수단이다.
MCP Schema Tool은:
실제 Schema가 무엇인가?
를 확인하는 수단이다.
이렇게 하면 기존 Fail-closed 철학을 유지하면서
큰 Schema에서 탐색 비용을 줄일 수 있다.
15. 제안 11 — Vector Retrieval과 Truth 분리
판정
NEW PRINCIPLE
다음 원칙을 명시적으로 도입할 가치가 있다.
Vector Retrieval 결과는 Truth가 아니다.

Vector DB의 역할:
Candidate Retrieval
Semantic Catalog의 역할:
Structured Semantic Truth
Relationship Catalog의 역할:
Validated Structural Relationship
QueryForge의 역할:
Validated Execution
따라서:
Vector Similarity
      ↓
Candidate
      ↓
Catalog Validation
      ↓
QuerySpec
      ↓
QueryForge Validation
과정을 유지한다.
16. 제안 12 — Query Memory 추가
판정
NEW / WRENAI REFERENCE
WrenAI는 다음 Pair를 Vector Memory에 저장한다.
Natural Language
       ↕

SQL

DataLens에서는 이를 한 단계 확장할 수 있다.

```text
Natural Language
       ↕

Resolved Semantic
       ↕
QuerySpec
       ↕
SQL

예:

```text
"MME별 성공률 보여줘"

       ↓

metric:
success_rate
dimension:
node_id
filter:
node_type = MME
   ↓
QuerySpec
   ↓
SQL

새로운 질문:

```text
MME 성공 비율 알려줘
가 들어왔을 때 과거 성공 Query를 검색하여
Semantic Resolution에 참고할 수 있다.
17. 제안 13 — SQL보다 QuerySpec을 Memory의 중심으로 사용
판정
NEW / DATALENS ADVANTAGE
이 부분은 WrenAI보다 DataLens가 구조적으로 유리할 수 있다.
WrenAI Query Memory는 NL ↔ SQL 중심이다.
하지만 SQL은 다음에 종속된다.
DBMS

Dialect
Physical Schema

DataLens에는 이미 Query Specification이라는 논리 계층이 존재한다.

따라서 Query Memory의 중심은 다음과 같이 두는 것이 좋다.

```text
NL
 ↕

QuerySpec
 ↕
SQL

QuerySpec은 다음 의미를 보존한다.

```text
Metric

Dimension

Filter

Group

Sort
Time Range

따라서 Query Memory의 장기 재사용성이 SQL보다 높다.

---

# 18. 제안 14 — Session Context와 Query Memory 분리

## 판정

```text
KEEP + EXTEND
기존 Session / Dataset Lineage 설계는 유지한다.
다만 장기 Memory와 역할을 구분한다.
Session Context
=
현재 대화의 기억

Dataset Lineage
현재 분석 흐름의 데이터 계보
Query Memory
과거 성공한 질의 경험
Semantic Memory
조직/도메인 특유의 언어 지식

이 네 가지는 서로 다른 책임을 가진다.

이를 혼합하면 Context 관리가 복잡해질 수 있으므로
논리적으로 분리하는 것이 좋다.

---

# 19. 제안 15 — Semantic Memory 추가

## 판정

```text
NEW
실제 사용자 대화를 통해 발견되는 표현을 장기적으로 축적하는 구조를 고려한다.
예:
사용자:
MME별 소통률 보여줘.
DataLens가 문맥과 기존 Query Memory를 이용하여:
소통률
→ success_rate

confidence:
0.72

로 해석했다고 가정한다.

이 관계가 반복적으로 사용되거나
사용자가 명시적으로 확인하면:

```text
term:
소통률

concept:
success_rate

scope:
datasource

confidence:
0.95

와 같이 저장할 수 있다.

핵심은 사용자가 별도의 Alias 관리 작업을 하지 않는 것이다.

---

# 20. 제안 16 — Semantic Scope 추가

## 판정

```text
NEW
학습한 Semantic을 모든 DB에서 공통으로 사용하면 위험하다.
예를 들어:
소통률
이라는 표현은 특정 조직에서만 success_rate를 의미할 수 있다.
따라서 다음 Scope 구조를 고려한다.
GLOBAL
  ↓
DOMAIN
  ↓
ORGANIZATION
  ↓
DATASOURCE
검색 우선순위는 반대로 한다.
DATASOURCE
  ↓
ORGANIZATION
  ↓
DOMAIN
  ↓
GLOBAL
가장 구체적인 Context를 우선한다.
21. 제안 17 — Semantic Learner 추가
판정
NEW
Semantic Memory에 무엇을 저장할지 판단하는 논리 컴포넌트를 고려한다.
Semantic Learner
입력:
User Question

Resolved Semantic

QuerySpec

Execution Result

User Follow-up
User Correction
Repeated Usage

출력:

```text
Semantic Candidate

Confidence Update

Term Mapping

Candidate Promotion

Candidate Rejection

단, 모든 대화를 자동으로 Semantic Truth로 승격해서는 안 된다.

---

# 22. 제안 18 — User Feedback를 학습 신호로 사용

## 판정

```text
NEW
다음과 같은 사용자 행동은 강한 Semantic Evidence가 될 수 있다.
명시적 Correction:
"소통률은 성공률 말하는 거야."
강한 Evidence.
반복 사용:
소통률 → success_rate
여러 Query에서 반복 성공.
중간 Evidence.
단순 LLM 추론:
소통률 ≈ success_rate
약한 Evidence.
이를 confidence에 반영할 수 있다.
23. 제안 19 — Seed Query Generator 도입
판정
NEW / WRENAI REFERENCE
WrenAI의 seed_queries.py는 높은 참고 가치가 있다.
Schema를 이용하여 기본 Query Example을 자동 생성한다.
예:
Table
+
Numeric Metric
+
Groupable Dimension
으로:
"SUCCESS_CNT by NODE_TYPE"
와 같은 기본 Query를 생성할 수 있다.
DataLens에서는 NL ↔ SQL보다는 다음 구조를 권장한다.
NL

↕

QuerySpec

↕

Expected SQL Shape

---

# 24. 제안 20 — Seed Query를 Bootstrap Evaluation에 활용

## 판정

```text
NEW
현재 Golden Question Set은 사람이 검증한 품질 기준으로 유지해야 한다.
Seed Query가 Golden Set을 대체해서는 안 된다.
대신 다음 구조를 고려한다.
Schema
   │
   ▼
Seed Query Generator
   │
   ▼
Bootstrap Test Set
새 DB 연결 직후 자동으로 기본 검증 Query를 생성한다.
예:
count

sum
group by
time filter
dimension filter
relationship join

이를 통해 Semantic Bootstrap 결과가 최소한 구조적으로 동작하는지
자동 검증할 수 있다.

Golden Set:

```text
Human Validated
Seed Test Set:
Automatically Generated
으로 역할을 구분한다.
25. 제안 21 — Relationship Candidate 자동 탐색
판정
EXTEND / WRENAI REFERENCE
현재 Relationship은 다음 근거를 사용할 수 있다.
DB FK

Domain Pack Declaration

여기에 자동 Candidate Detection을 추가할 수 있다.

우선순위:

```text
1. Foreign Key

2. Composite Key Pattern

3. Column Naming

4. Data Type

5. Sample Value Overlap

Cardinality

LLM Inference


예:

```text
customer_id
→ customers.customer_id
보다 복잡한 EPC PoC의 경우:
NODE_TYPE + NODE_ID

↕

EQUIP_TYPE + EQUIP_ID

같은 복합 관계도 탐색해야 한다.

자동 탐색 결과는:

```text
Candidate Relationship
으로 관리하고 검증 전에는 확정 관계로 취급하지 않는다.
26. 제안 22 — Relationship Catalog와 Semantic Catalog 분리
판정
NEW
Relationship은 언어 지식과 성격이 다르다.
예:
"성공률"
은 Semantic 문제지만:
NODE_TYPE + NODE_ID
=
EQUIP_TYPE + EQUIP_ID
는 구조적 관계다.
따라서 논리적으로:
Semantic Catalog

Relationship Catalog

를 구분하는 것이 좋다.

`relationships.yaml`은 계속 다음 역할을 할 수 있다.

```text
Curated Relationship

Override

Import / Export
Bootstrap Seed

---

# 27. 제안 23 — Schema 변경 감지를 Semantic 영향 분석으로 확장

## 판정

```text
EXTEND / WRENAI REFERENCE
기존 Schema Fingerprint 개념을 적극 활용한다.
현재:
Schema 변경
   ↓
Fingerprint 변경
향후:
Schema 변경
   ↓
Fingerprint 변경
   ↓
Affected Semantic 탐색
   ↓
Affected Relationship 탐색
   ↓
Affected Query Memory 탐색
   ↓
Selective Re-index
예:
Column 추가
Semantic Bootstrap 재수행

Schema Embedding 생성

### Column 삭제

```text
관련 Metric invalid

관련 Query Memory stale

### Relationship 변경

```text
관련 QuerySpec 재검증
전체 Vector Index를 항상 재생성하는 것보다
영향 범위만 재처리하는 구조를 고려한다.
WrenAI의 다음 구현을 참고한다.
watch.py

mdl_hash

schema indexing

---

# 28. 제안 24 — Context Budget과 Schema Memory 결합

## 판정

```text
EXTEND
DataLens는 LLM에 전체 Schema를 전달하지 않는 방향을 유지한다.
Schema Memory는 이 원칙과 잘 맞는다.
기존:
schema tool
   ↓
Relevant Schema
   ↓
LLM
확장:
User Question
     │
     ▼
Schema Memory Retrieval
     │
     ▼
Candidate Schema
     │
     ▼
Schema Tool Verification
     │
     ▼
Context Manager
     │
     ▼
LLM
이를 통해 큰 DB에서도 Context Budget을 안정적으로 유지할 수 있다.
29. 제안 25 — SemanticProvider를 Facade로 발전
판정
EXTEND
기존 SemanticProvider 추상화는 유지할 가치가 있다.
다만 장기적으로 단순 Dictionary Provider가 아니라
여러 Semantic Source를 통합하는 Facade로 확장할 수 있다.
예:
SemanticProvider
      │
      ├─ General Lexicon
      │
      ├─ Semantic Catalog
      │
      ├─ Schema Memory
      │
      ├─ Query Memory
      │
      └─ Semantic Memory
Agent는 내부 구현을 알 필요 없이:
resolve()

search()
get_metric()
get_entity()
get_relationship_candidate()

같은 논리 인터페이스만 사용한다.

이를 통해 초기 PoC에서는 단순 구현체를 사용하고
향후 자동 Semantic 구조로 교체하기 쉬워진다.

---

# 30. 제안 26 — QueryForge에는 Semantic Memory를 넣지 않는다

## 판정

```text
KEEP
이 부분은 기존 설계를 변경하지 않는 것이 좋다.
다음 책임 경계를 유지한다.
DataLens
=
Meaning / Reasoning / Memory

QueryForge
Validation / Planning / Execution

따라서 다음 기능은 DataLens 측에 둔다.

```text
General Lexicon

Semantic Bootstrap

Semantic Catalog

Schema Memory

Query Memory
Semantic Memory
Semantic Resolver
Semantic Learner

QueryForge는 계속:

```text
QuerySpec Validate

Query Plan

SQL Generate
Execute
Dataset
Transform
Describe

를 담당한다.

이는 QueryForge의 Domain-neutral 원칙을 유지하는 데 중요하다.

---

# 31. 제안 27 — MCP Tool 5개 구조 유지

## 판정

```text
KEEP
Semantic 기능이 추가된다고 MCP Tool을 늘릴 필요는 없다.
현재:
schema

relationship

query
transform
describe

구조를 유지한다.

Semantic Resolution은 MCP Tool이 아니라
DataLens 내부 Intelligence Layer에서 수행한다.

필요한 Schema / Relationship의 실제 확인만
기존 MCP Tool을 통해 수행한다.

---

# 32. 제안 28 — QuerySpec 중심 구조 유지

## 판정

```text
KEEP / STRENGTHEN
WrenAI 분석 이후 오히려 QuerySpec의 가치가 더 명확해졌다.
다음 구조를 유지한다.
Natural Language
       ↓
Semantic Resolution
       ↓
QuerySpec
       ↓
QueryForge
       ↓
SQL
QuerySpec은 다음 기능의 공통 언어로 활용할 수 있다.
Agent ↔ QueryForge

Query Memory

Golden Test

Seed Test

Semantic Learning
Execution Audit

따라서 QuerySpec은 DataLens의 핵심 Architecture Asset으로 유지하는 것이 좋다.

---

# 33. 제안 29 — Dataset / Lineage 구조 유지

## 판정

```text
KEEP
WrenAI의 Query History와 DataLens Dataset Lineage는 목적이 다르다.
Dataset Lineage는 현재 분석 흐름을 나타낸다.
ds_000001
   ↓
filter
   ↓
ds_000002
   ↓
group_by
   ↓
ds_000003
Query Memory는 과거 성공 경험을 나타낸다.
따라서 Query Memory 도입을 이유로
Dataset / Lineage 구조를 변경하지 않는다.
두 구조를 함께 사용한다.
34. 제안 30 — 폐쇄망 환경을 고려한 Embedding 구조
판정
NEW CONSIDERATION
DataLens는 폐쇄망 배포 가능성을 유지해야 한다.
따라서 Vector / Embedding 기능을 도입할 경우
외부 Embedding API에 의존해서는 안 되는 환경을 고려해야 한다.
Embedding Provider 추상화를 고려한다.
EmbeddingProvider

 ├─ LocalEmbeddingProvider

 └─ RemoteEmbeddingProvider
PoC에서는 단순 구현을 선택할 수 있지만
Architecture는 교체 가능하도록 설계한다.
WrenAI의 embeddings.py는 구현 Reference로 활용할 수 있다.
35. 제안 31 — Vector DB는 구현 기술로 고정하지 않는다
판정
DEFER TECHNOLOGY DECISION
WrenAI는 LanceDB를 사용한다.
LanceDB는 참고 가치가 높지만
DataLens에서 즉시 LanceDB를 Architecture Decision으로 확정할 필요는 없다.
후보:
LanceDB

FAISS

Qdrant
pgvector
SQLite Vector Extension
기타 Embedded Vector Store

PoC 요구사항을 기준으로 비교한다.

특히:

```text
폐쇄망

설치 복잡도
Python 호환성
Persistence
Metadata Filter
Backup
운영성

을 고려해야 한다.

따라서 지금 결정할 것은:

> Vector Retrieval Layer가 필요할 수 있다.

까지로 제한하고,

> Vector DB는 LanceDB다.

라고 확정하지 않는다.

---

# 36. 제안 32 — MDL 자체는 도입하지 않는다

## 판정

```text
REFERENCE ONLY
WrenAI MDL은 좋은 Reference다.
특히 다음 구조를 참고할 가치가 있다.
Model

Column

Relationship

Metric

Calculated Field

하지만 DataLens에는 이미:

```text
QuerySpec

Domain Pack

Catalog Snapshot

Relationship
Semantic Provider

구조가 존재한다.

따라서 WrenAI MDL을 그대로 추가하면
중복된 Modeling Layer가 생길 가능성이 있다.

권장 방향:

```text
MDL
→ DataLens Catalog 설계 Reference
이지:
MDL
→ DataLens Core Dependency
가 아니다.
37. 제안 33 — enrich-context 개념 차용
판정
REFERENCE / EXTEND
WrenAI의 enrich-context는 Schema만으로 알 수 없는
Business Context를 추가하는 구조다.
대표적으로:
Enum Meaning

Unit

NULL Meaning

Magic Value

Soft Delete
Business Synonym
Timezone
Business Metric
Cross-system Identifier

이 개념은 DataLens에서도 중요하다.

다만 DataLens에서는 사용자가 모두 직접 작성하도록 하기보다:

```text
Schema Profiling

LLM

General Lexicon

Query History

User Conversation
Curated Override

등을 이용하여 가능한 부분을 자동으로 채우는 방향을 고려한다.

---

# 38. 제안 34 — DataLens Semantic 성장 모델

## 판정

```text
NEW
Semantic System을 정적인 완성품으로 보지 않고
시간에 따라 성장하는 시스템으로 정의할 수 있다.
초기:
General Lexicon

+

Schema

Bootstrap 후:

```text
General Lexicon

+

Schema

+

Auto Semantic

Seed Queries

사용 시작 후:

```text
General Lexicon

+

Schema

+

Auto Semantic

+

Real Query Memory

+

Semantic Memory

장기 운영:

```text
General Knowledge
       ↓
Domain Knowledge
       ↓
Organization Knowledge
       ↓
DataSource Knowledge
즉 Semantic Knowledge가 무작정 넓어지는 것이 아니라
실제로 사용되는 영역에 점점 집중되는 구조를 목표로 한다.
39. 제안 35 — Semantic Retrieval Pipeline
향후 Semantic Resolver는 개념적으로 다음 흐름을 고려할 수 있다.
사용자:
MME별 소통률 보여줘.
Step 1 — General Lexicon
MME
별
소통
률
범용 의미 후보 탐색.
Step 2 — Semantic Memory
현재 DataSource에서:
소통률
관련 표현 검색.
Step 3 — Query Memory
과거 성공 Query 검색.
예:
MME별 성공률 보여줘.

MME 성공 비율 알려줘.

### Step 4 — Schema Memory

관련 Schema 탐색.

```text
PM_EPC_KPI_1M

NODE_TYPE
NODE_ID
ATTEMPT_CNT
SUCCESS_CNT

### Step 5 — LLM Resolution

후보를 종합하여:

```text
MME
→ NODE_TYPE = 'MME'

MME별
→ GROUP BY NODE_ID

소통률
→ success_rate candidate

### Step 6 — Semantic Catalog

`success_rate`의 구조화된 정의 확인.

### Step 7 — QuerySpec

논리 질의 생성.

### Step 8 — QueryForge

검증 / SQL 생성 / 실행.

### Step 9 — Learning

성공한 Query를 Query Memory에 기록.

필요한 경우:

```text
소통률 → success_rate
를 Semantic Candidate로 기록.
40. 기존 Architecture에서 반드시 보존할 결정
WrenAI 분석을 이유로 다음 기존 설계를 변경하지 않는 것을 권장한다.
QueryForge Domain-neutral
KEEP
QueryForge 코드에 EPC, PGW, MME 등의 업무 개념을 넣지 않는다.
QuerySpec
KEEP / STRENGTHEN
Agent와 QueryForge 사이의 논리적 계약으로 유지한다.
Dataset Ownership
KEEP
실제 Dataset은 QueryForge가 소유한다.
Dataset Lineage
KEEP
멀티턴 분석 흐름 추적에 사용한다.
MCP Tool 5개
KEEP
Semantic 기능 때문에 Tool을 증설하지 않는다.
Catalog Snapshot
KEEP / EXTEND
Semantic Bootstrap의 기반으로 확장한다.
Context Budget
KEEP
Schema Memory를 이용하여 더 효과적으로 적용한다.
Fail-closed
KEEP
Vector / LLM 추론은 Candidate 생성에 사용하고
실행 전에는 구조화된 검증을 수행한다.
폐쇄망 지원
KEEP
Embedding / Vector 구현 역시 폐쇄망 사용을 고려한다.
41. 기존 Architecture에서 재검토할 항목
다음은 향후 문서 개정 시 재검토할 가치가 있다.
Domain Pack = Semantic Source of Truth
재검토 권장.
대안:
Semantic Catalog
=
Source of Truth

Domain Pack
Seed / Override / Export / Import

## semantic.yaml의 대규모 Alias

재검토 권장.

대안:

```text
General Lexicon

+

LLM Resolution

+

Semantic Memory

## DictionarySemanticProvider 중심 구조

확장 권장.

대안:

```text
SemanticProvider
=
Semantic Resolution Facade
Vector / RAG 전체 후순위 처리
부분 재검토 권장.
대규모 RAG Platform까지 PoC에 넣을 필요는 없지만
Schema Retrieval과 Query Memory는 DataLens의 자동 Semantic 방향과
직접적으로 연결되므로 별도로 평가할 가치가 있다.
42. ADR.md 향후 검토 포인트
원본 ADR은 본 문서에서 수정하지 않는다.
향후 다음 항목을 검토한다.
ADR-009 계열
Domain Pack의 책임.
검토 내용:
semantic.yaml의 역할

Domain Pack의 Source of Truth 여부
Runtime Semantic Learning과의 관계

### Vector / RAG 관련 ADR

기존 Deferred 결정 중:

```text
Schema Memory

Query Memory

만 별도로 분리하여 재평가할 가치가 있다.

전체 RAG Platform 도입과
Semantic Retrieval을 같은 결정으로 취급하지 않는 것이 좋다.

### Catalog Snapshot 관련 ADR

다음 확장 검토:

```text
Schema Profiling

Semantic Bootstrap

Schema Re-index

### 신규 ADR 후보

```text
Automatic Semantic Bootstrap

Semantic Catalog Source of Truth
Semantic Memory Scope
Vector Retrieval Is Not Truth
Query Memory Policy
Semantic Confidence Lifecycle

---

# 43. DATALENS-SAD.md 향후 검토 포인트

원본 SAD는 본 문서에서 수정하지 않는다.

향후 다음 Component 추가 여부를 검토한다.

```text
Semantic Bootstrapper

Semantic Catalog
General Lexicon
Schema Memory
Query Memory
Semantic Memory
Semantic Resolver
Semantic Learner

기존 SemanticProvider는 삭제하기보다
위 Component를 통합하는 Interface / Facade로 발전시키는 방안을 고려한다.

또한 Context Manager에:

```text
Schema Memory Retrieval

Query Memory Retrieval

결과를 Context Budget 내에서 조합하는 책임을 추가할 수 있다.

---

# 44. GLOSSARY.md 향후 검토 포인트

현재 용어 정의는 유지한다.

향후 Architecture가 확정될 경우 다음 용어 추가를 고려한다.

```text
Semantic Catalog

Semantic Bootstrapper

Semantic Candidate

Semantic Memory

Schema Memory

Query Memory

General Lexicon
Semantic Resolver
Semantic Learner
Semantic Scope
Semantic Confidence

특히 다음 정의는 명확히 유지해야 한다.

```text
Semantic Layer
=
업무 개념과 실제 DB 구조를 연결하는 지식 계층

Semantic Resolver
사용자 표현을 Semantic Concept 후보로 해석하는 처리

둘을 혼동해서는 안 된다.

---

# 45. 새로운 논리 Component 후보

기존 CMP 체계에 추가한다면 다음 정도를 검토할 수 있다.

예:

```text
CMP-DL-SEMANTIC-BOOTSTRAP

CMP-DL-SEMANTIC-CATALOG
CMP-DL-SEMANTIC-RESOLVER
CMP-DL-SEMANTIC-MEMORY
CMP-DL-QUERY-MEMORY
CMP-DL-SCHEMA-MEMORY
CMP-DL-SEMANTIC-LEARNER

실제 ID와 Component 분리는 SAD 개정 시 확정한다.

PoC에서는 이들을 모두 별도 서비스로 만들 필요는 없다.

논리적 책임 분리부터 시작한다.

---

# 46. PoC에 즉시 반영할 가치가 있는 항목

다음 항목은 비교적 구현 비용이 낮으면서
향후 Architecture에도 그대로 활용할 가능성이 높다.

## 1. Schema Profiling

```text
Column Type

PK / FK

Cardinality

Sample Values

Identifier Candidate

## 2. Identifier Heuristic

```text
*_ID

*_KEY
PK
FK
Relationship Key

## 3. Semantic Candidate 구조

최소한:

```text
type

source

confidence

status

를 고려한다.

## 4. QuerySpec 저장

성공한 NL Query와 QuerySpec의 Pair를 기록할 수 있도록 구조를 준비한다.

## 5. Schema Fingerprint

Semantic Bootstrap 결과와 연결할 수 있도록 한다.

---

# 47. 구현 직전 추가 분석할 WrenAI 영역

현재 WrenAI 분석만으로 Architecture 방향은 충분히 잡을 수 있다.

다만 실제 구현 직전에는 다음 영역을 추가 분석하는 것이 좋다.

## MDL 내부 구현

확인 목적:

```text
Model

Column

Relationship
Metric
Calculated Field

를 실제 코드에서 어떻게 표현하는지 확인.

DataLens Semantic Catalog Schema 설계 Reference로 사용한다.

## Memory → LLM Context 경로

확인 목적:

```text
MemoryStore.get_context()

↓

Prompt / Agent
↓
SQL Generation

전체 소비 경로 추적.

DataLens의:

```text
Semantic Resolver

↓

Context Manager

↓

Agent

설계에 참고한다.

## Retrieval Policy

확인 대상:

```text
top-k

similarity threshold
metadata filter
ranking
fallback

## Schema Change Handling

확인 대상:

```text
watch.py

mdl_hash

re-index

stale memory handling

---

# 48. 후순위로 두어도 되는 항목

현재 PoC에서는 다음 기능을 바로 구현할 필요는 없다.

```text
Semantic 자동 승격

Confidence Decay

Semantic Conflict Resolution

Organization Scope

대규모 Vector Cluster
GraphRAG
Cross-DB Semantic Sharing
Automatic Memory Cleanup
Advanced Reranking

Architecture상 확장 가능성만 확보한다.

---

# 49. 권장 발전 순서

## Phase 1 — 현재 PoC

```text
Catalog Snapshot

Schema Profiling

Relationship

QuerySpec

QueryForge

Basic Semantic Provider

## Phase 2 — Automatic Bootstrap

```text
General Lexicon

Semantic Bootstrapper
Semantic Candidate
Semantic Catalog

## Phase 3 — Retrieval

```text
Schema Memory

Embedding

Relevant Schema Retrieval

## Phase 4 — Experience Memory

```text
Query Memory

NL ↔ QuerySpec

Similar Query Retrieval

## Phase 5 — Continuous Semantic Learning

```text
Semantic Memory

Semantic Learner

User Correction
Confidence
Scope

이 순서는 Architecture 방향을 나타낸다.

실제 PoC 일정에 따라 Phase 일부를 앞당겨 검증할 수 있다.

---

# 50. 최종 권장 Architecture 방향

현재 DataLens Architecture의 기본 골격은 유지할 가치가 높다.

특히:

```text
Agent

QuerySpec

QueryForge
Dataset
Lineage
MCP
Catalog Snapshot

구조는 WrenAI 분석 이후에도 변경할 이유가 크지 않다.

오히려 Semantic 영역을 다음과 같이 확장하는 것이 핵심이다.

```text
                     General Lexicon
                            │
                            │
Database ──→ Catalog Snapshot
                            │
                            ▼
                  Semantic Bootstrapper
                            │
                            ▼
                    Semantic Catalog
                            │
              ┌─────────────┼─────────────┐
              │             │             │
              ▼             ▼             ▼
        Schema Memory   Query Memory   Semantic Memory
              │             │             │
              └─────────────┼─────────────┘
                            ▼
                    Semantic Resolver
                            │
                            ▼
                          Agent
                            │
                            ▼
                        QuerySpec
                            │
                            ▼
                       QueryForge
                            │
                            ▼
                         Dataset
그리고 멀티턴 분석은 기존 구조를 유지한다.
Session Context

+

Dataset Lineage

장기 경험은 별도로 관리한다.

```text
Query Memory

+

Semantic Memory

이 구분이 중요하다.

---

# 51. 핵심 결론

WrenAI 분석 결과 DataLens의 기존 Architecture를 대규모로 변경할 필요는 없다.

오히려 기존 구조 중 다음은 강점으로 판단된다.

```text
QuerySpec

Domain-neutral QueryForge
Dataset Ownership
Dataset Lineage
고정 MCP Tool
Catalog Snapshot
Context Budget
Fail-closed

WrenAI에서 가장 적극적으로 참고할 부분은 다음이다.

```text
Schema Memory

Query Memory

Seed Query

Schema Indexing
generate-mdl
Schema Change Detection
enrich-context

DataLens가 WrenAI보다 한 단계 더 발전시킬 수 있는 부분은 다음이다.

```text
Automatic Semantic Bootstrap

General Lexicon Base

NL ↔ QuerySpec Memory

User Conversation 기반 Semantic Learning
Semantic Confidence
Semantic Scope
Semantic Lifecycle

최종적으로 DataLens Semantic Architecture의 방향은 다음과 같이 정의할 수 있다.

> DataLens는 사용자가 Semantic Layer를 지속적으로 작성하고 관리하는 시스템을 목표로 하지 않는다.

> 범용 언어 지식과 DB Schema를 기반으로 초기 Semantic을 자동 구축하고,
> 실제 사용자 질의와 성공한 QuerySpec을 축적하여 해당 조직과 DataSource에 점진적으로 특화되는 구조를 목표로 한다.

WrenAI는 이 구조 전체의 대체재가 아니라,
Schema Memory / Query Memory / Bootstrap / Context Enrichment 구현을 위한 중요한 Reference Implementation으로 활용한다.

본 문서의 제안은 즉시 기존 ADR / SAD / GLOSSARY에 반영하지 않는다.

향후 DataLens 설계 개정 시 본 문서를 참고하여 각 제안을:

```text
ACCEPT

MODIFY
DEFER
REJECT

중 하나로 판단하고,
Architecture Decision이 필요한 항목은 별도의 ADR로 확정한다.