# DataLens Automatic Semantic Architecture

## 1. 문서 목적

본 문서는 DataLens의 자연어 질의 해석을 위한 Semantic Architecture의 설계 방향과,
WrenAI 오픈소스 분석을 통해 확인한 재사용 가능한 구조 및 구현 참고 사항을 정리한다.

DataLens의 목표는 사용자가 Semantic Alias를 지속적으로 작성하고 관리하는 방식이 아니다.

핵심 목표는 다음과 같다.

> 범용 사전과 DB Schema를 기반으로 초기 Semantic을 자동 구성하고,
> 실제 사용자 질의와 성공한 Query를 지속적으로 축적하여
> 해당 데이터 도메인에 점진적으로 특화되는 Semantic System을 구축한다.

따라서 Semantic Layer는 정적인 YAML 설정 파일이 아니라
다음 요소가 결합된 동적인 시스템으로 정의한다.

- General Lexicon
- Schema / Metadata Catalog
- Automatic Semantic Bootstrap
- Semantic Resolver
- Semantic Memory
- Query Memory
- Semantic Learning


---

# 2. 설계 원칙

## 2.1 사용자가 Alias를 관리하지 않는다

다음과 같은 방식은 사용하지 않는다.

```yaml
success_rate:
  aliases:
    - 성공률
    - 성공 비율
    - 성공율
    - 성공 퍼센트
    - 소통률
    - 처리 성공률
```

PoC에서는 간단하지만 실제 운영 환경에서는 도메인과 사용자가 늘어날수록
Alias 관리 비용이 급격하게 증가한다.

특히 다음 문제가 발생한다.

- 새로운 표현이 계속 발생
- 조직별 용어가 다름
- 동일 단어가 도메인마다 다른 의미를 가짐
- Alias 중복 및 충돌
- Semantic YAML 지속적인 유지보수 필요
- 사용자가 Semantic 구조를 이해해야 함

따라서 DataLens에서는 사람이 Alias를 지속적으로 등록하는 방식을 기본 구조로 사용하지 않는다.


## 2.2 범용 언어 지식을 Base로 사용한다

DataLens 자체적으로 범용 Semantic Dictionary를 제공한다.

예:

```text
성공 ↔ 성공하다
원인 ↔ 이유 ↔ 사유
비율 ↔ 율 ↔ 백분율
장비 ↔ 기기 ↔ 설비
수 ↔ 건수 ↔ 개수
```

이 Dictionary는 특정 DataSource에 종속되지 않는다.

초기 데이터는 공개 사전, 동의어 데이터, WordNet 계열 등의
검증 가능한 언어 자원을 ETL하여 구축하는 방향을 고려한다.

단, 일반적인 유사어 관계를 업무적으로 동일한 의미라고 단정해서는 안 된다.

따라서 최소한 다음 정보를 함께 관리한다.

```text
term
canonical_term
language
relation_type
source
confidence
```

relation_type 예:

```text
EXACT
SYNONYM
ABBREVIATION
MORPHOLOGICAL
RELATED
OPPOSITE
```

`RELATED`와 `SYNONYM`은 동일한 신뢰도로 처리하지 않는다.


---

# 3. 전체 Architecture

```text
                       User Question
                             │
                             ▼
                    Semantic Resolver
                             │
          ┌──────────────────┼───────────────────┐
          │                  │                   │
          ▼                  ▼                   ▼
   General Lexicon      Schema Memory       Query Memory
          │                  │                   │
          │                  ▼                   │
          │           Semantic Catalog           │
          │                  │                   │
          └──────────────────┼───────────────────┘
                             │
                             ▼
                       LLM Reasoning
                             │
                             ▼
                    Resolved Semantic
                             │
                             ▼
                         QuerySpec
                             │
                             ▼
                        QueryForge
                             │
                             ▼
                          Database
```

Semantic System은 크게 다음 계층으로 구성한다.

### General Lexicon

DataLens 자체가 제공하는 범용 언어 지식.

### Semantic Catalog

현재 DataSource에서 사용할 수 있는 구조화된 Semantic 정보.

### Schema Memory

Table / Column / Metric / Relationship 등의 정보를 Vector화한 검색 계층.

### Query Memory

과거 자연어 질문과 성공한 QuerySpec/SQL의 관계를 저장한다.

### Semantic Memory

실제 사용 과정에서 발견한 도메인 고유 표현을 축적한다.

### Semantic Resolver

위 정보를 종합하여 사용자 표현을 실제 DataSource의 개념으로 연결한다.


---

# 4. DB 최초 연결 시 Automatic Bootstrap

새로운 DB를 연결했다고 해서 사용자가 Semantic 파일을 작성하도록 요구하지 않는다.

초기 과정은 DataLens가 자동으로 수행한다.

```text
Database
   │
   ▼
Schema Introspection
   │
   ▼
Metadata Collection
   │
   ▼
Data Profiling
   │
   ▼
Relationship Candidate Detection
   │
   ▼
Semantic Candidate Generation
   │
   ▼
Embedding / Indexing
   │
   ▼
Semantic Catalog
```

수집 대상은 다음과 같다.

### Schema

```text
Table
Column
Data Type
NULL 여부
PK
FK
Index
Comment
```

### Data Profiling

필요한 경우 제한된 Sample Data를 이용한다.

```text
Distinct Count
Sample Values
Min / Max
NULL Ratio
Cardinality
Value Pattern
```

예:

```text
PM_EPC_KPI_1M.NODE_TYPE

sample:
MME
PGW
SGW
```

이를 통해 단순 VARCHAR가 아니라
EPC 장비 종류를 나타내는 categorical dimension 후보임을 추론할 수 있다.


---

# 5. Semantic Candidate 자동 생성

DDL과 Metadata를 이용하여 Column의 역할을 자동 분류한다.

예:

```text
PM_EPC_KPI_1M

EVENT_TIME
→ temporal dimension

NODE_TYPE
→ categorical dimension

NODE_ID
→ identifier / dimension

ATTEMPT_CNT
→ numeric metric candidate

SUCCESS_CNT
→ numeric metric candidate
```

이 과정에는 heuristic과 LLM을 함께 사용한다.


## 5.1 Identifier Detection

Numeric Column이라고 무조건 Metric으로 판단해서는 안 된다.

예:

```text
NODE_ID
BRANCH_ID
MTSO_ID
```

이들은 숫자형이어도 SUM 대상이 아니다.

다음 조건을 이용한다.

```text
Primary Key

Foreign Key

Relationship Key

*_ID

*_KEY

*_CODE
```

예:

```text
NODE_ID INTEGER

→ identifier candidate
→ SUM 대상 제외
```


---

# 6. Metric 자동 탐색

일부 Metric은 Schema로부터 후보를 자동 생성할 수 있다.

예:

```text
ATTEMPT_CNT
SUCCESS_CNT
```

가 존재할 경우 LLM은 다음 후보를 생성할 수 있다.

```text
success_rate

numerator:
SUCCESS_CNT

denominator:
ATTEMPT_CNT

aggregation:
ratio_of_sums
```

단 이것은 초기에는 확정된 Business Truth가 아니다.

따라서:

```text
status = candidate
source = schema_inference
confidence = 0.82
```

와 같은 상태로 관리할 수 있다.

사용 과정에서 반복적으로 올바르게 사용되거나 검증되면 confidence를 상승시킨다.


---

# 7. Relationship 자동 탐색

DB에 FK가 존재하면 이를 가장 강한 Relationship 근거로 사용한다.

```text
Foreign Key
    ↓
Relationship
```

FK가 없을 경우 Naming / Data Pattern 등을 이용하여 Candidate를 생성한다.

예:

```text
customer_id
    ↓
customers.customer_id
```

DataLens에서는 복합 관계도 지원해야 한다.

현재 EPC PoC의 대표적인 예:

```text
PM_EPC_KPI_1M.NODE_TYPE
PM_EPC_KPI_1M.NODE_ID

        ↕

CM_EPC_INFO.EQUIP_TYPE
CM_EPC_INFO.EQUIP_ID
```

따라서 단일 Column 이름 일치만으로 Relationship을 판단해서는 안 된다.

Relationship 역시 confidence를 갖는 Candidate로 관리하는 것이 적절하다.


---

# 8. General Lexicon

Semantic 해석의 첫 번째 Base는 범용 사전이다.

예:

```text
성공
 ├─ 성공하다
 ├─ 성공한
 └─ 성공됨

비율
 ├─ 율
 ├─ 비중
 └─ 백분율

원인
 ├─ 이유
 ├─ 사유
 └─ 요인
```

단순 문자열 Alias Table이 아니라 관계를 저장한다.

예:

```text
term        canonical     relation      confidence
--------------------------------------------------
이유        원인           SYNONYM       0.90
사유        원인           SYNONYM       0.90
요인        원인           RELATED       0.70
```

이를 통해 LLM이 사용자 질문을 해석할 때 기본적인 언어 지식을 제공한다.


---

# 9. Semantic Memory

General Lexicon만으로 모든 업무 표현을 처리할 수 없다.

실제 조직에서는 다음과 같은 고유 표현이 존재할 수 있다.

```text
소통률
개통률
부착률
접속률
호성공률
```

이러한 표현을 사용자가 별도의 설정 화면에서 등록하도록 하지 않는다.

DataLens가 실제 대화를 통해 점진적으로 학습한다.

예:

사용자:

```text
MME별 소통률 보여줘.
```

Semantic Resolver가:

```text
소통률
    ↓
success_rate ?

confidence = 0.72
```

로 추론했다고 가정한다.

이 결과가 반복적으로 성공하거나 사용자가 명시적으로 의미를 확인하면:

```text
term:
소통률

concept:
success_rate

scope:
datasource

confidence:
0.92
```

와 같이 Semantic Memory에 저장한다.


---

# 10. Semantic Memory Scope

학습된 표현은 Global Knowledge로 바로 승격해서는 안 된다.

동일 표현이 조직이나 DB마다 다른 의미일 수 있기 때문이다.

따라서 Scope를 둔다.

예:

```text
GLOBAL
  ↓
DOMAIN
  ↓
ORGANIZATION
  ↓
DATASOURCE
```

조회 시에는 좁은 Scope를 우선한다.

```text
DATASOURCE
    ↓
ORGANIZATION
    ↓
DOMAIN
    ↓
GLOBAL
```

이를 통해 특정 DataSource에서 학습한 표현이 다른 시스템의 Semantic을 오염시키는 것을 방지한다.


---

# 11. Query Memory

WrenAI 분석에서 특히 참고 가치가 높은 구조이다.

WrenAI는 자연어 질문과 SQL을 Pair로 저장한다.

개념적으로:

```text
NL Query
    ↕

SQL Query
```

예:

```text
"MME별 성공률 보여줘"

        ↕

SELECT NODE_ID,
       SUM(SUCCESS_CNT) / SUM(ATTEMPT_CNT)
...
```

새로운 질문:

```text
"MME 장비들의 성공 비율 알려줘"
```

가 들어오면 기존 Query History에서 Semantic Similarity가 높은 질문을 검색한다.

```text
New Question
     │
     ▼
Embedding
     │
     ▼
Vector Search
     │
     ▼
Similar NL / SQL pairs
     │
     ▼
LLM Context
```

DataLens에서는 SQL만 저장하기보다는 QuerySpec을 함께 저장하는 것이 바람직하다.

예:

```text
nl_query
query_spec
sql
datasource
execution_result
success
created_at
```

QuerySpec은 DB Dialect에 종속되지 않기 때문에 Semantic Memory로서 SQL보다 재사용성이 높다.


---

# 12. Seed Query 자동 생성

WrenAI에서 참고할 가치가 높은 또 하나의 기능이다.

Schema를 기반으로 기본 NL ↔ Query 예제를 자동 생성한다.

예:

Model:

```text
PM_EPC_KPI_1M
```

자동 생성:

```text
"List all PM_EPC_KPI_1M"
```

Query:

```sql
SELECT *
FROM PM_EPC_KPI_1M
LIMIT 100
```

Numeric Metric:

```text
SUCCESS_CNT
```

자동 생성:

```text
"Total SUCCESS_CNT in PM_EPC_KPI_1M"
```

Query:

```sql
SELECT SUM(SUCCESS_CNT)
FROM PM_EPC_KPI_1M
```

Group Dimension이 존재하면:

```text
"SUCCESS_CNT by NODE_TYPE"
```

Query:

```sql
SELECT
    NODE_TYPE,
    SUM(SUCCESS_CNT)
FROM PM_EPC_KPI_1M
GROUP BY NODE_TYPE
```

이렇게 생성된 Seed Query도 Query Memory에 넣을 수 있다.

결과적으로 DB를 처음 연결한 직후부터 Semantic Retrieval에 사용할 기본 Example Set을 확보할 수 있다.


---

# 13. Vector DB의 역할

Vector DB를 Semantic Truth 저장소로 사용하지 않는다.

Vector DB의 역할은 **후보 검색**이다.

예:

```text
"소통률"
    ↓
Vector Search
    ↓
success_rate
communication success rate
call success rate
...
```

최종 정의는 Semantic Catalog에서 조회한다.

```text
success_rate
    ↓
Semantic Catalog
    ↓
SUM(SUCCESS_CNT)
/
SUM(ATTEMPT_CNT)
```

따라서 구조는:

```text
Vector DB
   =
Semantic Retrieval

RDB / Catalog
   =
Semantic Truth
```

로 분리한다.


---

# 14. Semantic Catalog

Semantic의 최종 구조화 정보는 Catalog에 저장한다.

초기에는 다음 정도를 고려한다.

```text
semantic_concept

semantic_metric

semantic_dimension

semantic_entity

semantic_term

semantic_relationship

semantic_feedback
```

예:

### semantic_metric

```text
name:
success_rate

type:
derived

numerator:
SUCCESS_CNT

denominator:
ATTEMPT_CNT

aggregation:
ratio_of_sums

unit:
percent

confidence:
0.94

source:
auto_bootstrap
```


---

# 15. YAML의 역할 변경

기존 설계처럼 `semantic.yaml`을 사용자가 지속적으로 관리하는 방식은 사용하지 않는다.

Semantic Source of Truth는 Catalog가 담당한다.

필요한 경우:

```text
Catalog
   ↓
Export
   ↓
semantic.yaml
```

또는:

```text
semantic.yaml
   ↓
Import
   ↓
Catalog
```

기능만 제공한다.

즉 YAML은:

> Semantic 관리 파일

이 아니라:

> Semantic 교환 / 배포 / 백업 포맷

으로 사용한다.

Relationship 역시 장기적으로 동일한 구조를 적용할 수 있다.


---

# 16. WrenAI 분석 결과

WrenAI에는 DataLens Semantic Architecture 구현 시 참고 가치가 높은 코드가 존재한다.

특히 다음 세 영역이 중요하다.

```text
1. MDL / generate-mdl
2. Schema Memory
3. Query Memory
```


---

# 17. WrenAI Memory 구현

중요 경로:

```text
core/wren/src/wren/memory/
```

확인 대상:

```text
store.py
schema_indexer.py
embeddings.py
seed_queries.py
index_backend.py
markdown.py
watch.py
```


## 17.1 store.py

가장 우선적으로 분석할 파일이다.

WrenAI의 Memory Store를 구현한다.

LanceDB를 이용하여 대략 다음 두 종류의 데이터를 관리한다.

```text
schema_items

query_history
```

주요 관심 함수:

```python
MemoryStore.index_schema()
MemoryStore.get_context()
MemoryStore.store_query()
MemoryStore.recall_queries()
```

DataLens Semantic Memory 구현 시 직접적인 참고 대상이다.


---

# 18. WrenAI Schema Memory

Schema 정보를 Text로 변환하고 Embedding하여 저장한다.

대표적인 저장 정보:

```text
text
vector

item_type
model_name
item_name

data_type
expression
is_calculated

mdl_hash
indexed_at
```

처리 흐름:

```text
MDL
 │
 ▼
extract_schema_items()
 │
 ▼
Text representation
 │
 ▼
Embedding
 │
 ▼
LanceDB
```

질의 시 Schema가 작으면 전체 Schema를 Context로 제공하고,
큰 Schema에서는 Vector Search를 통해 관련 Schema만 검색한다.

이 방식은 DataLens에서도 적극적으로 참고한다.


---

# 19. WrenAI Query History

WrenAI는 다음 정보를 Memory로 저장한다.

```text
nl_query
sql_query
vector
datasource
created_at
tags
```

사용자 질문을 Embedding하고 과거 질문과 Similarity Search를 수행한다.

DataLens에서는 이를 확장하여:

```text
nl_query
resolved_semantic
query_spec
sql
datasource
execution_status
feedback
confidence
```

정도를 저장하는 방향을 고려한다.


---

# 20. WrenAI Seed Query

중요 파일:

```text
core/wren/src/wren/memory/seed_queries.py
```

Schema / Model 정보를 기반으로 기본 Query Example을 자동 생성한다.

특히 참고할 부분:

- Numeric Column 탐색
- Groupable Column 탐색
- Identifier 제외
- Relationship 기반 JOIN Example 생성
- NL / SQL Pair 생성

Identifier 판별에서는 다음과 같은 heuristic을 사용한다.

```text
Primary Key
Relationship Key
ID-like name
```

따라서:

```text
NODE_ID
MTSO_ID
BRANCH_ID
```

같은 컬럼이 Numeric이라는 이유로 Metric으로 처리되는 것을 방지한다.

DataLens Automatic Semantic Bootstrapper 구현 시 참고 가치가 높다.


---

# 21. WrenAI generate-mdl

중요 경로:

```text
core/wren/src/wren/skills_content/generate-mdl/
```

특히:

```text
SKILL.md
```

를 참고한다.

DB Schema를 탐색하여 Wren MDL을 생성하는 절차가 정의되어 있다.

주요 과정:

```text
DB Connection

      ↓

Schema Discovery

      ↓

Type Normalization

      ↓

Model Generation

      ↓

Relationship Generation

      ↓

MDL
```

SQLAlchemy Inspector를 이용하는 방식이 포함되어 있다.

대표적으로:

```python
inspector.get_table_names()

inspector.get_columns()

inspector.get_pk_constraint()

inspector.get_foreign_keys()
```

등이다.

DataLens Schema Bootstrapper 구현 시 참고한다.


---

# 22. WrenAI Relationship 생성

FK가 존재하면 이를 Relationship으로 변환한다.

FK가 없을 경우 Naming Convention을 이용한 추론도 고려한다.

예:

```text
customer_id

     ↓

customers.customer_id
```

DataLens에서는 이를 확장하여:

```text
Column Name
Data Type
Sample Value
Cardinality
Composite Key
Value Overlap
```

등을 함께 사용해야 한다.

특히 EPC PoC처럼:

```text
NODE_TYPE + NODE_ID

↕

EQUIP_TYPE + EQUIP_ID
```

같은 복합 Relationship을 처리해야 한다.


---

# 23. WrenAI enrich-context

중요 경로:

```text
core/wren/src/wren/skills_content/enrich-context/
```

특히:

```text
SKILL.md
```

를 참고한다.

WrenAI 역시 Schema만으로 모든 Business Semantic을 알아낼 수 없다는 전제를 가진다.

추가 Context 대상으로 다음과 같은 항목을 다룬다.

```text
Enum Meaning

Unit

NULL Meaning

Magic Value

Soft Delete

Business Synonym

Timezone

Cross-system Identifier

Business Metric
```

DataLens에서는 이 Context를 사용자가 직접 입력하는 것보다
Data Profiling + LLM + Query Memory + User Conversation을 이용하여
최대한 자동으로 확보하는 방향으로 확장한다.


---

# 24. WrenAI에서 그대로 해결되지 않는 부분

WrenAI를 그대로 적용한다고 DataLens의 목표가 달성되는 것은 아니다.

WrenAI는 기본적으로:

```text
DB
 ↓
MDL
 ↓
Semantic Memory
 ↓
Text-to-SQL
```

구조를 가진다.

DataLens가 목표로 하는 구조는:

```text
DB
 ↓
Automatic Schema Analysis
 ↓
Automatic Semantic Bootstrap
 ↓
Semantic Catalog
 ↓
Semantic Memory
 ↓
Natural Language Query
 ↓
Continuous Learning
```

이다.

따라서 다음 영역은 DataLens에서 별도로 구현해야 한다.

### Automatic Semantic Bootstrapper

Schema + Data Profile + General Lexicon + LLM을 이용한 Semantic Candidate 자동 생성.

### Semantic Learner

사용자 대화 및 Query 실행 결과를 이용하여 Semantic Memory를 지속적으로 확장.

### Semantic Feedback

사용자의 명시적/암묵적 교정을 Semantic Knowledge로 변환.

### Confidence Management

자동 생성 Semantic을 즉시 Truth로 취급하지 않고 신뢰도를 관리.

### Scope Management

Domain / Organization / DataSource별 Semantic을 분리.


---

# 25. DataLens가 WrenAI에서 차용할 부분

우선순위는 다음과 같다.

| 기능 | 활용 방향 |
|---|---|
| MemoryStore | 적극 참고 |
| LanceDB 구조 | 적극 참고 |
| Schema Indexing | 적극 참고 |
| Query History | 적극 참고 |
| Seed Query | 적극 참고 |
| Identifier Heuristic | 적극 참고 |
| MDL | 구조 참고 |
| generate-mdl | Bootstrap 설계 참고 |
| Relationship 생성 | 참고 후 확장 |
| enrich-context | 개념 참고 |
| Alias 관리 | 최소화 |
| 수동 Semantic 관리 | 사용하지 않는 방향 |


---

# 26. 구현 시 우선 확인할 WrenAI 파일

최우선:

```text
core/wren/src/wren/memory/store.py

core/wren/src/wren/memory/schema_indexer.py

core/wren/src/wren/memory/embeddings.py

core/wren/src/wren/memory/seed_queries.py
```

다음:

```text
core/wren/src/wren/memory/index_backend.py

core/wren/src/wren/memory/markdown.py

core/wren/src/wren/memory/watch.py
```

Semantic Bootstrap 참고:

```text
core/wren/src/wren/skills_content/generate-mdl/SKILL.md
```

Context Enrichment 참고:

```text
core/wren/src/wren/skills_content/enrich-context/SKILL.md
```

구현 시 위 파일들을 우선적으로 다시 분석한다.


---

# 27. DataLens Semantic 성장 모델

DataLens Semantic은 처음부터 완벽할 필요가 없다.

핵심은 사용하면서 해당 DataSource에 특화되는 것이다.

초기:

```text
General Dictionary

+
Schema
```

상태에서 시작한다.

사용 초기:

```text
General Dictionary
+
Schema
+
Auto Semantic
+
Seed Queries
```

실제 사용이 시작되면:

```text
General Dictionary
+
Schema
+
Auto Semantic
+
Real Query History
+
Semantic Terms
```

충분히 사용된 시스템은:

```text
General Knowledge
       │
       ▼
Domain Knowledge
       │
       ▼
Organization Knowledge
       │
       ▼
DataSource Knowledge
```

순으로 점점 좁고 정확한 Semantic Context를 갖게 된다.

즉 시간이 지날수록 전체 Vector Space를 무작정 넓히는 것이 아니라
현재 DataSource에서 실제 사용하는 영역에 **집중**해야 한다.


---

# 28. Query 처리 시 Semantic Retrieval 전략

사용자가 다음과 같이 질문했다고 가정한다.

```text
MME별 소통률 보여줘.
```

### Step 1. General Lexicon

```text
MME
소통
률
```

범용 의미 탐색.


### Step 2. DataSource Semantic Memory

현재 DataSource에서:

```text
소통률
```

과 관련된 기존 Semantic 검색.


### Step 3. Query Memory

과거 질문:

```text
MME 성공률 보여줘
MME별 성공 비율
장비별 호 성공률
```

등을 Vector Search.


### Step 4. Schema Memory

관련 Schema 검색:

```text
PM_EPC_KPI_1M

NODE_TYPE
NODE_ID
ATTEMPT_CNT
SUCCESS_CNT
```


### Step 5. LLM Resolution

모든 Context를 이용하여:

```text
MME
→ NODE_TYPE = MME

MME별
→ NODE_ID GROUP BY

소통률
→ success_rate
```

후보를 생성.


### Step 6. Semantic Catalog

`success_rate`의 실제 정의를 조회한다.


### Step 7. QuerySpec 생성

최종적으로 구조화된 QuerySpec을 생성한다.


### Step 8. QueryForge

QuerySpec 검증 및 SQL 생성/실행.


### Step 9. Learning

성공한 질문과 QuerySpec을 Query Memory에 기록한다.

필요한 경우:

```text
소통률 → success_rate
```

관계도 Semantic Memory Candidate로 기록한다.


---

# 29. 중요한 원칙: Retrieval과 Truth 분리

다음 원칙은 반드시 유지한다.

> Vector Search 결과 자체를 Query 정의로 사용하지 않는다.

Vector Search는:

```text
Candidate Discovery
```

용도다.

실제 Query 의미는:

```text
Semantic Catalog
+
Relationship Catalog
+
QuerySpec Validation
```

을 통해 확정한다.

이를 통해 Vector Similarity 오판이 직접 잘못된 SQL 실행으로 이어지는 것을 방지한다.


---

# 30. QueryForge와의 역할 분리

Semantic System은 DataLens Agent 영역이다.

QueryForge에 Semantic 학습 기능을 넣지 않는다.

```text
DataLens

General Lexicon
Semantic Catalog
Schema Memory
Query Memory
Semantic Memory
Semantic Resolver
Semantic Learner

        │
        ▼

     QuerySpec

        │
        ▼

     QueryForge

        │
        ├─ Validate
        ├─ Plan
        ├─ Generate SQL
        ├─ Execute
        └─ Return Result
```

따라서 QueryForge의 Domain-neutral 원칙을 유지한다.


---

# 31. 최종 방향

DataLens의 Semantic Layer는 정적인 설정 파일이 아니다.

다음과 같은 **Automatic Semantic System**으로 구현한다.

```text
             General Lexicon
                    │
                    │
Database ──→ Semantic Bootstrapper
                    │
                    ▼
             Semantic Catalog
                    │
             ┌──────┴──────┐
             ▼             ▼
        Schema Memory   Relationship
             │            Catalog
             │
User ────────┼───────────────┐
             │               │
             ▼               ▼
       Query Memory    Semantic Memory
             │               │
             └───────┬───────┘
                     ▼
              Semantic Resolver
                     │
                     ▼
                 QuerySpec
                     │
                     ▼
                 QueryForge
```

초기에는 범용 사전과 DB Schema에 의존한다.

실제 사용이 시작되면:

```text
User Conversation
Successful Query
User Correction
Repeated Expression
Query Context
```

등을 Semantic Memory로 점진적으로 축적한다.

따라서 DataLens는 시간이 지날수록 단순히 더 많은 단어를 아는 시스템이 아니라,

> **해당 조직과 해당 DataSource에서 실제 사용하는 언어와 데이터 관계에 점점 더 집중하는 시스템**

으로 발전한다.


---

# 32. 구현 단계 제안

현재 PoC에서는 전체 Semantic Learning System을 한 번에 구현하지 않는다.

### Phase 1 — Semantic Bootstrap

```text
General Lexicon
Schema Introspection
Data Profiling
Semantic Candidate
Relationship Candidate
Semantic Catalog
```

### Phase 2 — Semantic Retrieval

```text
Schema Embedding
Vector Search
Semantic Resolver
LLM Context
```

### Phase 3 — Query Memory

```text
NL Query
QuerySpec
SQL
Execution Result
Vector Index
Similar Query Retrieval
```

### Phase 4 — Semantic Learning

```text
Semantic Term Extraction
User Correction Detection
Confidence
Scope
Candidate Promotion
```

### Phase 5 — Continuous Semantic Optimization

```text
Duplicate Merge
Conflict Detection
Confidence Decay
Unused Semantic Cleanup
Domain Specialization
Memory Re-indexing
```

PoC에서는 최소한 Phase 1~3의 구조를 고려하되,
실제 구현 범위는 프로젝트 일정에 따라 조정한다.


---

# 33. 구현 시 재검토 사항

실제 개발 착수 전 다음 항목을 결정한다.

1. General Lexicon으로 사용할 공개 데이터
2. Lexicon 저장 방식
3. Semantic Catalog DB Schema
4. Vector DB 선정
5. Embedding Model 선정
6. Semantic Confidence 정책
7. Semantic Scope 정책
8. Query Memory 보존 정책
9. 개인정보/민감정보가 포함된 사용자 질문의 Memory 저장 정책
10. Semantic Candidate 자동 승격 기준
11. Schema 변경 시 Memory 재구성 전략
12. QuerySpec과 Semantic Memory 연결 방식


---

# 34. 핵심 결론

WrenAI 분석 결과 DataLens가 필요로 하는 모든 Semantic 자동화 기능이 완성된 형태로 존재하지는 않는다.

그러나 다음 핵심 구현은 높은 참고 가치가 있다.

```text
WrenAI generate-mdl
    → Automatic Bootstrap 참고

WrenAI Schema Memory
    → Schema Vector Retrieval 참고

WrenAI Query History
    → NL ↔ Query Memory 참고

WrenAI Seed Queries
    → 초기 Semantic Example 자동 생성 참고

WrenAI enrich-context
    → Business Context 확장 모델 참고
```

DataLens는 이를 기반으로 한 단계 더 확장한다.

최종적으로 목표하는 차별점은 다음과 같다.

> **사용자가 Semantic Layer를 만드는 것이 아니라
> DataLens가 DB와 범용 언어 지식으로 Semantic을 자동 구축하고,
> 실제 사용자 대화를 통해 해당 도메인의 언어와 Query Pattern을 지속적으로 학습한다.**

이 원칙을 DataLens Semantic Architecture의 기본 방향으로 사용한다.