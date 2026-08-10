# DataLens — Semantic Architecture 제안 비판적 검토

| 항목 | 내용 |
|---|---|
| 문서 ID | SEM-REVIEW-001 |
| 버전 | 0.1 |
| 작성일 | 2026-08-10 |
| 검토 대상 | `WRENAI_DATALENS_ARCHITECTURE_REVIEW.md` (제안 1~35), `SEMANTIC_ARCHITECTURE.md` (§1~34) |
| 기준 문서 | `ADR.md` (ADR-001~019), `DATALENS-SAD.md` (v0.1), `GLOSSARY.md` (v0.1) |
| 성격 | Critical Review. **기존 문서를 수정하지 않는다.** 변경 가치가 있는 항목만 선별 제안한다 |
| 비고 | §11은 `SEMANTIC_ARCHITECTURE.md` 추가 확인 사항. 판정 변경 1건, 신규 발견 5건 포함 |

---

## 반영 결과 (2026-08-10)

본 검토의 결론은 다음과 같이 확정 문서에 반영되었다. **구현 근거는 아래 ADR/SAD이며 본 문서가 아니다.**

| 검토 항목 | 반영 위치 |
|---|---|
| §3 Clarify-Confirm-Log, §4 통제 원칙 C-1~C-5 | **ADR-020**, SAD 8.4 규칙 5, SAD 14.4, P-11·P-12, N-15·N-16 |
| §3.2 Surface Form Normalizer (General Lexicon 대체) | **ADR-021**, SAD 14.3 |
| §7.2 Identifier 집계 금지 | **ADR-022**, N-17, SAD 23.4 판정 7 |
| §2.3 Schema Profiling 수집 제약 | **ADR-023**, N-18 |
| §7.3 Interaction Log (QuerySpec 중심 기록) | **ADR-024**, SAD 15.5 |
| §5 WrenAI 라이선스 경계 | **ADR-025**, N-19, 액션 아이템 #9 |
| §4.3 C-5 평가 재현성 | **ADR-026**, N-20 |
| §11.4 복합 Relationship 선언 스키마 | **ADR-027**, SAD 3.1 U1·U4, 23.4 판정 9, 액션 아이템 #8 |
| §7.1 Domain Pack SoT 유지 (제안2 기각) | **ADR-009 개정** |
| §6 Vector 3분할 및 게이트 | **ADR-010 개정**, SAD 15.4, P-13 |
| §11.1 PoC Dataset 제약, 대표 시나리오 실행 불가 | **SAD 3.1 개정** + 제약표, 23.4 단서, 액션 아이템 #10 |
| §11.7 §33 미결 12건 정리 | ADR-020·021·024로 대부분 소멸 |

**해소된 액션 아이템**: 테이블 수(Core 11개, `DB_PoC.md`) → ADR-010a 판정 근거로 사용.
**신규 액션 아이템**: #8 TOOL-002 응답 필드(S0 내), #9 라이선스 확인(착수 전), #10 다중 장비 데이터,
#11 운영 규모 행 수, #12 일본어 콜레이션.

제안 문서 2종(`SEMANTIC_ARCHITECTURE.md`, `WRENAI_DATALENS_ARCHITECTURE_REVIEW.md`)에는
상태 배너와 미채택 항목표가 삽입되었다. 원문은 논의 이력으로 보존한다.

---

## 0. 요약 판정

제안 35건 중 **그대로 수용 6건, 수정 후 수용 11건, 연기 8건, 기각 5건, 변경 없음(현행 유지 재확인) 5건**이다.

한 문장 결론:

> 리뷰 문서의 **진단은 옳고, 처방은 과하다.**
> Alias 수동 관리가 장기적으로 붕괴한다는 진단은 타당하다. 그러나 처방으로 제시된 7개 신규 컴포넌트 + Vector Retrieval 스택은
> 이 프로젝트의 실제 제약(컨텍스트 8~16K, Agent 스텝 3회, 1인 개발 15주, 폐쇄망, 검증이 병목)과 정면으로 충돌한다.
> 같은 문제의 **80%는 기존 설계에 이미 존재하는 3가지 장치**(`did_you_mean` 힌트 · `session_aliases` · Knowledge Candidate Store)를
> 연결하고, 표기 정규화 1개를 추가하는 것으로 해결된다.

가장 중요한 지적 세 가지를 먼저 적는다.

1. **리뷰 문서에 비용 축이 없다.** 35개 제안 어디에도 구현 비용, 15주 일정 대비 우선순위, 컨텍스트 예산 영향, 지연 영향이 없다. 1인 개발 + 코딩 에이전트 체제(ADR-018)에서 병목은 코드가 아니라 **검증**이다. "좋은 아이디어"는 판정 기준이 될 수 없다.
2. **제안된 Semantic Retrieval Pipeline(§39)은 ADR-017 예산 안에서 실행 불가능하다.** 아래 §2에 산술을 적었다.
3. **자동 학습된 Semantic의 오류는 fail-closed로 잡히지 않는다.** 잘못된 컬럼은 에러가 되지만, 잘못된 지표 매핑은 **그럴듯한 틀린 숫자**를 만든다. 분석 제품에서 가장 나쁜 실패 모드이며, 리뷰 문서의 confidence 메커니즘은 이것을 막지 못한다(§4).

---

## 1. 제안의 전제 재검토 — 문제는 실재하지만 아직 관측되지 않았다

리뷰 문서 §2의 문제 제기는 이렇다.

```
사용자 표현 증가 → Alias 증가 → Domain Pack 증가 → 수동 유지보수 증가 → Semantic 충돌
```

이 인과는 맞다. 그리고 SAD 1.3이 "PoC 종료 후에도 판매 가능한 제품"을 목표로 하므로, 확장 불가능한 구조는 실제 부채가 된다. 여기까지는 동의한다.

그러나 현재 상태는 다음과 같다.

| 항목 | 현재 |
|---|---|
| Domain Pack | 1개 (`poc_telecom`) |
| 대상 DB | 1개 |
| 조직 | 1개 |
| 관측된 alias 충돌 | 0건 |
| 관측된 사용자 표현 분포 | **없음** (S0 미착수) |
| 대상 테이블 수 | **미확인** (ADR 액션아이템 #4) |

즉 리뷰 문서는 **관측되지 않은 스케일 문제**에 대해 **7개 컴포넌트를 선제 설계**하고 있다. 이것은 두 가지 이유로 위험하다.

첫째, 표현 다양성의 실제 분포를 모른다. "성공률/성공 비율/성공율/소통률"이 정말 문제인지, 아니면 실사용자 5명이 사실상 3개 표현만 쓰는지 알 수 없다. 후자라면 Domain Pack의 alias 20줄로 끝난다. **이 데이터는 S1 이후 자동으로 수집된다**(§3의 Interaction Log). 데이터 없이 설계하지 말고, 데이터를 만드는 장치부터 넣는 것이 옳다.

둘째, 이 프로젝트는 확장성 요구를 이미 다른 방식으로 처리하고 있다. SAD 22.1의 "교체 축" 표와 ADR-018의 "인터페이스는 완비하고 구현은 순차 확장"이 그것이다. 즉 **원래 설계 철학은 "지금 만든다"가 아니라 "지금 막지 않는다"이다.** 리뷰 문서는 이 철학에서 이탈해 구현을 앞당기려 한다.

**판정**: 문제 제기는 채택. 해결 시점은 "지금 구축"이 아니라 "지금 관측 장치 설치, 데이터 확보 후 결정"으로 조정한다.

---

## 2. 기존 구조와의 충돌 — 리뷰 문서가 다루지 않은 4건

리뷰 문서는 §30~33에서 "QuerySpec / QueryForge / Dataset Lineage / MCP는 유지"라고 선언하며 충돌이 없다고 주장한다. 선언 수준에서는 맞다. 그러나 **정량 제약과의 충돌 4건**을 다루지 않았다.

### 2.1 컨텍스트 예산 위반 (ADR-017) — 심각

ADR-017의 16K 기준 배분에서 Semantic 컨텍스트는 **1,000 토큰**이다. 여기에 리뷰 문서 §39가 제안하는 것을 넣어보면:

| 주입 항목 | 추정 토큰 | 비고 |
|---|---|---|
| General Lexicon 후보 (Step 1) | 150~300 | 형태소 단위 후보 다수 |
| Semantic Memory 히트 (Step 2) | 100~200 | term + concept + confidence + scope |
| Query Memory 히트 top-3 (Step 3) | **450~900** | 항목당 NL + QuerySpec ≈ 150~300 |
| Semantic Catalog 정의 (Step 6) | 150~250 | numerator/denominator/aggregation |
| **합계** | **850~1,650** | |

Query Memory top-3만으로 Semantic 예산 전체를 소진하며, 합계는 예산을 최대 65% 초과한다. Schema Memory 결과(Step 4)는 스키마 예산 2,500 안에서 처리 가능하나, 이 역시 `describe_table` 결과와 경합한다.

더 결정적인 것은 **ADR-001의 대안(B) 시나리오**다. 26B A4B를 택하면 컨텍스트 상한이 **8K**로 반토막 나고(NFR-005), 예산표도 대략 절반이 된다. 그 경우 Semantic 예산은 약 500 토큰이며, **제안된 Retrieval Pipeline은 구조적으로 실행 불가능**하다.

리뷰 문서는 §28에서 "Schema Memory를 이용해 Context Budget을 더 효과적으로 적용"이라고 주장하는데, Schema Memory 단독으로는 참이다(스키마 후보를 좁히므로). 그러나 **Query Memory와 Semantic Memory는 예산을 순증가시킨다.** 이 둘을 같은 항목으로 묶은 것이 오류다.

### 2.2 지연 예산 위반 (NFR-001 / ADR-012)

현재 1턴당 LLM 호출은 3회다: 의도 분류 → Spec 생성 → 응답 생성 (SAD §16 시퀀스 기준).

§39 파이프라인은 Step 5에 **독립적인 LLM Resolution 호출**을 추가한다. A2 15.3GB / 12B Q4 환경에서 호출당 지연을 보수적으로 5~15초로 잡으면 턴당 총 지연이 **약 33% 증가**한다. NFR-001은 P50 30초, NFR-002는 하드 120초다. 여기에 임베딩 추론(질문 1건, 비교적 저렴)과 Vector 검색이 더해진다.

즉 이 파이프라인은 **NFR-001을 위협하고, 그 대가로 얻는 것은 "아마도 맞을 확률이 조금 높은 추측"**이다. §3에서 제시하는 대안(명확화 1회 되묻기)은 LLM 호출을 늘리지 않으면서 **확정된 정답**을 얻는다.

### 2.3 N-8 / NFR-010 위반 — Schema Profiling의 사각지대

리뷰 문서 §7·§8은 Catalog Snapshot에 `distinct_count`, `sample_values`, `min/max`, `cardinality`를 추가하자고 제안한다. 이 제안 자체는 유용하다(§3에서 ACCEPT). 그러나 **수집 방법을 전혀 언급하지 않는다.**

- 대상 테이블은 일단위 대용량 파티션 테이블이며 **외부 자산**이다 (ADR-016, SAD 2.3).
- N-8: "시간 범위 조건 없는 대용량 테이블 조회" 금지.
- NFR-010: "파티션 프루닝 미적용 쿼리 **0건**".

`SELECT COUNT(DISTINCT NODE_TYPE) FROM PM_EPC_KPI_1M`을 무심코 실행하면 **운영 DB 풀스캔**이며, PoC 성공 판정 기준 4번을 그 자리에서 위반한다. 프로파일링은 반드시 다음 제약 하에 정의되어야 한다.

1. 단일 파티션(예: 최근 1일) 한정 + `LIMIT` 강제
2. 런타임 금지. `queryforge catalog refresh --profile` CLI에서만 (ADR-016의 명시적 갱신 원칙과 N-14에 정합)
3. `MAX_EXECUTION_TIME` 동일 적용
4. **`information_schema.STATISTICS`의 `CARDINALITY`를 우선 사용** — 이미 수집 중이며(ADR-016) 추가 쿼리가 0이다. 근사값이지만 "categorical 후보인가"를 판단하는 데는 충분하다

### 2.4 ADR-018 검증 체제와의 충돌 — 가장 실무적인 문제

ADR-018은 **모든 기능이 "인수 기준 + 검증 테스트"와 한 쌍**으로만 정의된다고 못박는다. 감독자는 코드를 읽지 않고 AC 통과 여부로만 진척을 판정한다.

이제 다음 기능의 AC를 작성해 보라.

```
Semantic Learner / 자동 승격
  AC-1  confidence가 0.95를 넘으면 candidate를 active로 승격한다
```

AC-1은 **테스트 가능하지만 무의미하다.** 검증되는 것은 "0.95 임계값 비교 로직이 동작한다"이지, "승격된 의미가 옳다"가 아니다. 후자를 검증할 오라클이 없다. 마찬가지로 confidence decay, semantic conflict resolution, 4단계 lifecycle 전이는 모두 **동작은 테스트되지만 정확성은 테스트되지 않는** 기능이다.

1인 감독 체제에서 이런 기능은 최악이다. 코딩 에이전트는 green을 보고하는데 시스템은 조용히 틀려간다. **ADR-018의 규율이 성립하지 않는 기능은 PoC에 넣지 않는 것이 이 프로젝트의 일관된 원칙이다.**

---

## 3. 더 단순하고 더 나은 대안 — 기존 자산 3개의 결합

리뷰 문서가 놓친 가장 중요한 사실: **필요한 부품이 이미 SAD에 전부 있다.**

| 기존 자산 | 위치 | 현재 용도 |
|---|---|---|
| `did_you_mean` 힌트 | SAD 17.2 | `UNKNOWN_COLUMN` 오류 복구 |
| `session_aliases` | SAD 12.2, FR-005, UC-008 | 사용자 정의 alias 세션 보관 |
| Knowledge **Candidate Store** | SAD 15.2 | "대화에서 추출된 학습 후보. 사람 승인 후 팩에 반영" |
| 명확화 되묻기 | UC-010, FR-016, P-4 | 의도 불명확 시 되묻기 |

리뷰 문서 §19의 "Semantic Memory"는 SAD 15.2의 Candidate Store를 **다른 이름으로 재발명한 것**이다. 차이는 단 하나 — SAD는 사람 승인을 요구하고, 리뷰 문서는 confidence 기반 자동 승격을 제안한다. 그 차이가 이 검토의 핵심 쟁점이다.

### 3.1 제안하는 대안: Clarify-Confirm-Log 루프

```
사용자: "MME별 소통률 보여줘"
   │
   ▼
Surface Form Normalizer          NFKC + 접미사/조사 정규화 (신규, ~50줄)
   │  "소통률" → "소통" + "률(=비율)"
   ▼
SemanticProvider.find_metric()   Domain Pack 조회 → 미해소
   │
   ▼
후보 제시 (신규 아님 — did_you_mean 확장)
   │  "'소통률'을 찾지 못했습니다. 다음 중 하나인가요?
   │   1) 성공률 (SUCCESS_CNT / ATTEMPT_CNT)
   │   2) 접속 성공률 (...)"
   ▼
사용자: "1번"
   │
   ├──▶ session_aliases["소통률"] = success_rate    (기존 FR-005, 즉시 반영)
   └──▶ Candidate Store 기록                        (기존 SAD 15.2)
              │
              ▼
        운영자가 주기적으로 검토 → Domain Pack diff 생성 → 승인 → 배포
```

**이 루프가 우월한 이유:**

| 관점 | 자동 학습 (리뷰 문서) | Clarify-Confirm-Log (대안) |
|---|---|---|
| 얻는 데이터 | 추론된 매핑 (confidence 0.72) | **사용자가 확정한 매핑** |
| 오류 누적 | 조용히 누적, 탐지 불가 | 구조적으로 불가능 |
| 신규 컴포넌트 | 7개 | **0개** (정규화기 1개만 신규) |
| 추가 LLM 호출 | +1 | 0 |
| 추가 컨텍스트 | 850~1,650 토큰 | ~50 토큰 (후보 목록) |
| Vector DB | 필요 | 불필요 |
| 설계 원칙 | P-4 위반 (확정 가능한 것을 LLM에 위임) | P-4·P-6·UC-010에 정합 |
| ADR-018 AC | 작성 곤란 | 자명함 |
| 비용 | 수 주 | **수 일** |

유일한 단점은 **첫 만남에서 1턴을 소비한다**는 것이다. 그러나 그 1턴은 UC-010에 이미 정의된 정상 동작이며, 대가로 얻는 것은 영구적으로 정확한 매핑이다. 그리고 두 번째 만남부터는 `session_aliases`로 0턴이다.

### 3.2 General Lexicon보다 나은 것: Surface Form Normalizer

리뷰 문서 §12의 General Lexicon은 다음 문제가 있다.

1. **폐쇄망 조달 문제.** 한국어/일본어 범용 유의어 사전을 어디서 구하나. KorLex, Open Korean WordNet, 일본어 WordNet 계열은 라이선스가 상업 납품에 부적합하거나 검토 부담이 크다. **ADR-001이 Gemma 4를 택한 이유가 정확히 "납품 법무 리스크 회피"였다.** 사전 도입은 그 결정과 모순된다.
2. **효과 없는 지점에 작동한다.** 실제 실패 사례는 "소통률" 같은 **업계·조직 은어**다. 어떤 범용 사전에도 없다.
3. **효과 있는 지점은 사전이 필요 없다.** "성공률 / 성공율 / 성공 비율 / 성공 퍼센트"는 유의어 문제가 아니라 **표기 변형 문제**다.

따라서 사전이 아니라 **결정적 정규화기**로 대체한다. ADR-015가 이미 NFKC + 대소문자 통일을 요구하므로, 그 확장으로 자연스럽다.

```
정규화 규칙 (결정적, 사전 불필요)
├─ NFKC 정규화, 대소문자 통일           (ADR-015 기존)
├─ 공백/구두점 제거                      "성공 률" → "성공률"
├─ 비율 접미사 정규화                    률|율|비율|퍼센트|% → :RATE
├─ 개수 접미사 정규화                    수|개수|건수|카운트 → :COUNT
├─ 조사/한정 접미사 제거                 별|의|를|은|는|당
└─ (JP) 전각/반각·카타카나 표기 통일     ADR-015 기존 요구
```

`성공률 / 성공율 / 성공 비율 / 성공퍼센트` → 전부 `성공:RATE`로 수렴한다. **Domain Pack에는 정식 명칭 1개만 등록하면 된다.** 리뷰 문서가 제기한 alias 폭발 문제의 상당 부분이 여기서 소멸한다.

규모: 규칙 테이블 + 함수 약 50~80줄. 로케일별 파일 분리(`locales/KR.yaml`, `JP.yaml`)로 기존 구조에 그대로 들어간다. AC 작성이 자명하다(입력 → 기대 정규형 테이블 테스트).

**이것이 이 검토에서 제안하는 유일한 실질적 신규 기능이다.**

---

## 4. 잘못된 Semantic 누적 위험의 통제 — 핵심 쟁점

사용자가 명시적으로 물은 항목이므로 별도로 다룬다.

### 4.1 왜 fail-closed가 이 문제를 막지 못하는가

기존 설계의 안전망(P-6, ADR-002 AST Validator, ADR-016 파티션 강제)은 **구조적 오류**를 막는다.

| 오류 유형 | 탐지 | 결과 |
|---|---|---|
| 존재하지 않는 컬럼 | ✅ Validator | 에러 → 복구 |
| 시간 범위 누락 | ✅ Planner | 에러 → 복구 |
| 잘못된 JOIN | ✅ relationship id 참조 강제 | 에러 → 복구 |
| **"소통률" → `attach_success_rate` (실제로는 `call_success_rate`)** | ❌ **없음** | **정상 실행. 그럴듯한 틀린 숫자** |

마지막 행이 문제의 전부다. 의미 오류는 **유효한 SQL을 만들고, 유효한 Dataset을 만들고, 유효한 문장을 만든다.** 시스템의 어떤 계층도 이것을 거부할 근거가 없다. 그리고 사용자는 숫자만 보고는 판별할 수 없다 — 판별할 수 있었다면 애초에 이 시스템이 필요 없다.

### 4.2 confidence 메커니즘의 근본적 결함

리뷰 문서 §22는 세 가지 evidence를 제시한다.

```
명시적 Correction  → 강한 Evidence
반복 사용          → 중간 Evidence
단순 LLM 추론      → 약한 Evidence
```

**두 번째가 치명적이다.** "반복 사용에서 성공"이란 무엇인가? 쿼리가 에러 없이 실행되었다는 뜻이다. 그런데 §4.1에서 봤듯 의미 오류는 **항상** 에러 없이 실행된다. 즉 이 신호는 의미 정확성과 **무상관**이다.

결과적으로 다음 피드백 루프가 성립한다.

```
LLM이 "소통률 ≈ success_rate" 추론 (confidence 0.72)
        ↓
사용자에게 알리지 않고 실행 → 숫자 출력
        ↓
사용자는 틀린 줄 모름 → 이의 제기 없음
        ↓
시스템은 "이의 없음"을 성공으로 계수 → usage_count++, confidence↑
        ↓
0.95 도달 → ACTIVE 승격
        ↓
이후 모든 사용자에게 틀린 매핑이 "검증된 Semantic"으로 적용
```

**침묵을 긍정 증거로 취급하는 것**이 이 설계의 구조적 결함이다. 또한 12B급 로컬 모델의 self-reported confidence는 캘리브레이션이 나쁘기로 잘 알려져 있어, 0.72와 0.82의 차이에 임계값을 걸 근거가 없다.

### 4.3 통제 원칙 (제안)

자동 학습을 도입한다면 최소한 다음 5개가 동시에 성립해야 한다. 하나라도 빠지면 도입하지 않는 편이 낫다.

| # | 원칙 | 근거 |
|---|---|---|
| **C-1** | **해석 공개 의무.** 팩에 선언되지 않은 용어를 해석했으면 답변에 반드시 명시한다. "'소통률'을 성공률(SUCCESS_CNT/ATTEMPT_CNT)로 해석했습니다." | 사용자에게 **부정 신호를 낼 기회**를 준다. 이것 없이는 학습 신호 자체가 성립하지 않는다 |
| **C-2** | **침묵은 증거가 아니다.** usage_count / 무이의는 confidence에 기여하지 않는다. **명시적 사용자 확인만** 증거로 인정한다 | §4.2 |
| **C-3** | **자동 승격 금지.** 학습물은 영구히 `candidate`. 승격은 Domain Pack diff에 대한 사람 승인으로만 | SAD 14.4 기존 결정. 리뷰 문서 §19·§21은 이를 뒤집는다 |
| **C-4** | **격리 범위 기본값은 세션.** 확인된 매핑도 기본은 `session_aliases`이며, 다른 사용자·세션에 자동 전파되지 않는다 | 한 사용자의 오해가 조직 전체 진실이 되는 것을 차단 |
| **C-5** | **평가 재현성 고정.** golden set 실행 시 semantic 버전을 핀으로 고정한다. 학습으로 인해 어제의 테스트 결과가 오늘 달라지면 ADR-018 체제가 붕괴한다 | 리뷰 문서 전혀 미언급 |

C-1은 **비용이 거의 없고 단독으로도 가치가 크다.** 응답 생성 프롬프트(`respond.md`)에 규칙 한 줄과 Spec에 `resolution_notes` 필드 하나를 추가하면 된다. SAD 8.4의 "불확실한 경우 단정하지 않고 근거를 함께 제시한다"의 구체화이기도 하다. **PoC에 즉시 반영할 것으로 분류한다.**

---

## 5. WrenAI 참고의 적합성 — 라이선스 경계를 먼저 정하라

리뷰 문서 §4는 참고할 WrenAI 파일 목록을 구체적으로 제시한다(`store.py`, `schema_indexer.py`, `seed_queries.py`, `embeddings.py` 등). 아이디어 참조로는 타당하다. 그러나 **라이선스 경계가 문서에 전혀 없다.**

확인 결과 WrenAI(Canner)는 **혼합 라이선스** 구조다. 저장소 최상위는 역사적으로 AGPL-3.0이었고, 이후 core/SDK/skills는 Apache-2.0으로 정리되었으며 일부 모듈에 AGPL-3.0이 유보되어 있다는 설명이 있다. AGPL-3.0 변경 요청 이슈도 공개되어 있다.

DataLens는 **고객사 납품 상용 제품**이 목표다(SAD 1.3). AGPL-3.0 코드가 유입되면 네트워크 서비스 제공 시 소스 공개 의무가 발생할 수 있으며, 이는 ADR-001이 Gemma 4를 택하며 피하고자 했던 바로 그 리스크다.

**필수 조치:**

1. 구현 착수 전 **참조 대상 파일 각각의 라이선스 헤더를 개별 확인**한다. 저장소 루트 LICENSE만 보고 판단하지 않는다.
2. 참조 방식을 문서에 명시한다: **"동작 원리 이해 및 설계 참고"는 허용, "코드 복사·이식·파생"은 라이선스 확인 전 금지.**
3. 코딩 에이전트 지시문에 명시한다. 에이전트는 참고 코드를 그대로 옮겨 쓰는 경향이 있다 — **1인 체제에 리뷰어가 없다는 ADR-019의 전제가 여기에도 적용된다.**
4. 별도 ADR로 확정한다(§8, ADR-025 후보).

### 5.1 WrenAI 아이디어별 적합성

| WrenAI 요소 | DataLens 적합성 | 판단 |
|---|---|---|
| Schema Indexing (벡터) | **낮음~중간.** WrenAI는 BigQuery/Snowflake 등 임의 대규모 웨어하우스가 대상이라 필수. DataLens는 단일 고객 통계 스키마이며 테이블 수 미확인. 또한 `SUCCESS_CNT` 같은 **식별자는 임베딩이 약한 입력**이다(산문이 아님) | 테이블 수 확인 후 재평가 |
| Seed Query 생성 | **중간.** 아이디어는 유용. 다만 "Semantic 검증"이 아니라 **실행 경로 스모크 테스트**다(§7 제안19) | 축소 채택, 후순위 |
| Identifier Heuristic | **높음.** 즉시 적용 가치 | 채택 |
| Query Memory (NL↔SQL) | **개념은 적합, 구현은 부적합.** DataLens는 QuerySpec 계층이 있으므로 SQL이 아니라 QuerySpec을 저장해야 한다. 이건 DataLens가 우위 | 개념 채택, 저장만 |
| `generate-mdl` / MDL | **낮음.** Domain Pack + Catalog Snapshot + QuerySpec으로 이미 커버. 중복 모델링 계층 | 기각(리뷰 문서와 동의) |
| `enrich-context` | **높음(개념).** enum 의미, 단위, NULL 의미, magic value, soft delete, timezone — **이건 Domain Pack에 있어야 할 필드 목록이다** | Domain Pack 스키마 보강으로 흡수 |
| Schema Change Detection (`watch.py`) | **중복.** SAD 18.2의 fingerprint + 영향 분석 리포트가 이미 동일 역할. 자동 감시는 N-14 위반 | 기존 유지 |
| LanceDB | **결정 유보 타당** | 리뷰 문서와 동의 |

가장 가치 있는 참고는 실은 코드가 아니라 **`enrich-context`가 열거한 "스키마만으로는 알 수 없는 정보 목록"**이다. 이건 Domain Pack `semantic.yaml`에 어떤 필드가 있어야 하는지에 대한 체크리스트로 직접 쓸 수 있고, 비용이 0이다.

---

## 6. Vector DB 필요/불필요 구분

사용자가 명시적으로 물은 항목이다. 용도별로 분리해서 판단해야 하며, **ADR-010이 이 셋을 한 덩어리로 묶어 DEFERRED 처리한 것이 오히려 문제**다(리뷰 문서 §41의 지적은 이 점에서 옳다).

| 용도 | Vector 필요성 | 판단 근거 |
|---|---|---|
| **Schema Retrieval** (질문 → 관련 테이블/컬럼) | **불필요 (현 규모)** | 테이블 수가 수십 규모라면 컬럼명·코멘트에 대한 **역색인 + 정규화 문자열 매칭**이 임베딩보다 정확하고, 디버깅 가능하고, 모델이 필요 없고, 폐쇄망 반입물이 늘지 않는다. 식별자(`ATTEMPT_CNT`)는 임베딩이 잘 다루는 입력이 아니다. **수백 개 이상이면 재평가** |
| **용어 매칭** (소통률 ↔ 성공률) | **불필요** | 한국어에서 이 문제의 큰 부분은 **표기·형태소 변형**이며 §3.2의 결정적 정규화가 처리한다. 남는 부분(진짜 은어)은 범용 임베딩 모델도 못 맞춘다 — 도메인 어휘이기 때문이다. 명확화 되묻기가 정답 |
| **Query Memory 유사 질의 검색** | **유일하게 진짜 필요할 수 있음** | 한국어/일본어 **문장 단위 패러프레이즈 매칭**은 임베딩이 실제로 잘하는 작업이다. 단, **PoC 시점에 이력이 0건이므로 가치도 0.** 가치는 사용량과 함께 증가한다 |
| **운영 문서 RAG** (매뉴얼, KPI 설명) | **필요하나 범위 밖** | SAD 15.2에 이미 정의됨. 비정형 산문이므로 임베딩이 적절. PoC 이후 |

**결론:**

> PoC에서 Vector DB는 **어느 용도로도 필요하지 않다.** ADR-010의 DEFERRED 판단은 옳았다.
> 다만 ADR-010을 **3개로 분할**하여 각각의 재평가 게이트를 명시해야 한다. 현재는 "RAG 전체"가 한 덩어리라 부분 도입 논의가 불가능하다.

부수 비용도 짚어둔다. 임베딩 도입 시 **다국어 임베딩 모델 가중치가 폐쇄망 반입 산출물에 추가**되고(ADR-014의 5개 항목 → 6개), 설치 검증 절차(SAD 19.1)와 버전 관리 대상이 늘어난다. NFR-012(설치 2시간)에도 영향이 있다. "벡터 검색 하나 추가"가 아니라 **배포 산출물 증가**다.

---

## 7. 제안별 판정 (35건)

`ACCEPT` 그대로 반영 · `MODIFY` 수정 후 반영 · `DEFER` PoC 이후 · `REJECT` 미반영 · `NO-CHANGE` 현행 유지 재확인(신규 결정 아님)

| # | 제안 | 판정 | 근거 (요약) |
|---|---|---|---|
| 1 | Domain Pack 역할 재정의 | **MODIFY** | Override/Seed 역할 명확화는 수용. **Semantic SoT 지위 이전은 기각** (제안2 참조) |
| 2 | semantic.yaml → Catalog가 SoT | **REJECT** | 방향이 반대다. §7.1 상세 |
| 3 | Catalog Snapshot → Semantic Bootstrap 기반 확장 | **ACCEPT** | 단 §2.3의 수집 제약 4개 필수. `information_schema.STATISTICS` 우선 |
| 4 | Automatic Semantic Bootstrapper | **MODIFY** | 런타임 컴포넌트가 아니라 **오프라인 CLI 생성기**로. 출력은 `semantic.yaml` **초안**이며 사람이 검토 후 커밋 |
| 5 | Identifier Heuristic | **ACCEPT** | 최고 가성비. 단 **QueryForge Validation에 배치** (§7.2) |
| 6 | Semantic Catalog 도입 | **MODIFY** | SoT 아님. Domain Pack에서 **파생되는 재생성 가능한 인메모리 인덱스**로 한정 |
| 7 | Semantic Lifecycle 4단계 | **MODIFY** | **2상태로 축소**: `declared`(팩 유래, 신뢰) / `candidate`(그 외, 공개 없이 사용 금지). ADR-018 AC 작성 가능성 |
| 8 | General Lexicon | **REJECT** | 라이선스·조달·효과 3중 문제. **대안: Surface Form Normalizer** (§3.2) |
| 9 | Schema Memory (벡터) | **DEFER** | 액션아이템 #4(테이블 수) 확인에 **게이트**. 수십 규모면 불필요 |
| 10 | Schema Retrieval + MCP schema tool 결합 | **DEFER** | 제안9 종속. 원칙(검색은 힌트, tool이 진실)만 선반영 |
| 11 | Vector Retrieval ≠ Truth | **ACCEPT** | 원칙만. 신규 ADR로 명문화. P-6의 자연스러운 확장 |
| 12 | Query Memory | **MODIFY** | **기록은 ACCEPT, 검색은 DEFER.** §7.3 — 가장 중요한 조정 |
| 13 | SQL 대신 QuerySpec을 Memory 중심으로 | **ACCEPT** | **리뷰 문서 최고의 통찰.** DataLens의 구조적 우위를 정확히 짚음 |
| 14 | Session Context / Query Memory 역할 분리 | **ACCEPT** | 개념 구분만. 구현 변경 없음. GLOSSARY 반영 가치 있음 |
| 15 | Semantic Memory 신설 | **MODIFY** | 신규 저장소 불필요. **SAD 15.2 Candidate Store로 흡수.** 자동 사용 금지(C-3) |
| 16 | Semantic Scope 4단계 | **MODIFY** | GLOBAL/DOMAIN/ORG/DATASOURCE → **`pack` / `session` 2단계.** 조직 1개·DB 1개에서 4단계는 검증 불가능한 복잡성 |
| 17 | Semantic Learner | **REJECT** | **대안: Clarify-Confirm-Log** (§3.1). 학습 컴포넌트가 아니라 워크플로로 해결 |
| 18 | User Feedback를 학습 신호로 | **MODIFY** | 명시적 correction **만** 증거. 반복사용/무이의는 증거 아님 (C-2) |
| 19 | Seed Query Generator | **DEFER** | 유용하나 후순위. **명칭 변경 필요**: Semantic 검증 아님 → "Schema Smoke Test" |
| 20 | Seed Query를 Bootstrap Evaluation에 | **MODIFY** | 기대 정답이 없으므로 의미 검증 불가. **SAD 19.1 설치 체크리스트의 "스모크 5문항" 자동 생성**으로 축소하면 가치 있음 |
| 21 | Relationship Candidate 자동 탐색 | **MODIFY** | **오프라인 CLI 제안기**로 한정. `relationships.yaml` 초안 생성 → 사람 검토. 런타임 후보 사용은 ADR-002(relationship id 참조 강제)와 충돌 |
| 22 | Relationship / Semantic Catalog 분리 | **ACCEPT** | 이미 `relationships.yaml`/`semantic.yaml`로 분리되어 있음. **문서화만 하면 됨 (신규 작업 없음)** |
| 23 | Schema 변경 → Semantic 영향 분석 확장 | **ACCEPT** | SAD 18.2에 이미 골격 존재. 영향 대상에 "지표 정의 / 저장된 QuerySpec"을 추가하는 **소폭 확장** |
| 24 | Context Budget + Schema Memory 결합 | **DEFER** | 제안9 종속 |
| 25 | SemanticProvider를 Facade로 | **MODIFY** | **시그니처 4개는 동결 유지**(S0 exit criterion). 확장은 인터페이스가 아니라 **반환 타입에 `source`/`confidence` 추가**로. `get_relationship_candidate()` 신설은 미검증 관계를 fail-closed 경로에 노출하므로 기각 |
| 26 | QueryForge에 Semantic 미포함 | **NO-CHANGE** | 기존 결정 유지. 재확인 가치는 있음 |
| 27 | MCP Tool 5개 유지 | **NO-CHANGE** | ADR-017 그대로 |
| 28 | QuerySpec 중심 유지 | **NO-CHANGE** | ADR-002 그대로 |
| 29 | Dataset / Lineage 유지 | **NO-CHANGE** | 기존 그대로 |
| 30 | 폐쇄망 Embedding Provider 추상화 | **DEFER** | 사용처가 확정되기 전 인터페이스를 만드는 것은 ADR-018의 "인터페이스 완비" 원칙 남용. **인터페이스도 아직 필요 없다** |
| 31 | Vector DB 기술 미확정 | **NO-CHANGE** | ADR-010 DEFERRED와 동일. 신규 결정 아님 |
| 32 | MDL 미도입 | **ACCEPT** | 동의. 중복 모델링 계층 회피 |
| 33 | `enrich-context` 개념 차용 | **MODIFY** | **자동 채움이 아니라 Domain Pack 스키마 필드로 흡수.** enum 의미/단위/NULL 의미/magic value/timezone은 사람이 아는 정보다 |
| 34 | Semantic 성장 모델 | **DEFER** | 서사이지 결정이 아님. 판정 대상 아님 |
| 35 | Semantic Retrieval Pipeline (9단계) | **REJECT** | §2.1·§2.2 예산·지연 위반. 현행 3~4단계 유지 |

### 7.1 제안2 상세 — 왜 Semantic Catalog를 SoT로 만들면 안 되는가

Domain Pack이 SoT인 것은 우연이 아니라 **ADR-009의 핵심 설계**다. Pack의 가치는 내용이 아니라 **형식**에 있다.

| Domain Pack 속성 | Semantic Catalog(런타임 DB)로 옮기면 |
|---|---|
| Git 버전 관리 | 소실 |
| diff 리뷰 가능 | 소실 |
| CI 검사 대상 (ADR-009 도메인 용어 검출) | **소실 — 유일한 방어선이 사라진다** |
| 폐쇄망 반입 산출물 (ADR-014 #3) | 백업·마이그레이션 대상으로 전환 |
| "다음 고객사엔 팩만 새로 작성" (FR-043) | 붕괴 |
| 롤백 | 소실 |

특히 **CI 검사 상실**이 치명적이다. ADR-009와 ADR-019는 "1인 개발 체제에는 리뷰어가 없으므로 물리적 경계와 자동 검사가 유일한 방어선"이라고 두 번 반복해 명시한다. Semantic을 런타임 가변 상태로 옮기면 이 방어선이 사라진다.

추가로 논리적 문제가 있다. 리뷰 문서는 Catalog를 SoT로 삼으면서 **Semantic Conflict Resolution은 §48에서 "후순위"로 연기**한다. 무언가를 진실의 원천으로 삼으면서 충돌 해소 규칙을 나중에 정하겠다는 것은 순서가 뒤집힌 것이다.

**올바른 방향:**

```
Domain Pack (Git, SoT)
      │ 로드
      ▼
Semantic Index (파생, 재생성 가능, 인메모리)
      ▲
      │ 승인된 diff
Candidate Store (학습 후보, 사용 안 함)
      ▲
      │ 확인된 매핑만
Clarify-Confirm 루프
```

### 7.2 제안5 상세 — Identifier Heuristic의 올바른 배치

리뷰 문서는 이 규칙을 Semantic Bootstrapper와 Seed Query Generator에서 쓰자고 한다. 그러면 **LLM에 주는 힌트**가 되고, 힌트는 무시될 수 있다.

더 나은 배치는 **QueryForge Validation의 결정적 규칙**이다.

- 이 규칙은 **도메인 지식이 아니라 구조 지식**이다(`*_ID`, PK, FK). 따라서 N-1·ADR-009의 도메인 용어 금지에 저촉되지 않는다. QueryForge의 domain-neutral 원칙과 양립한다.
- QueryForge는 Catalog Snapshot에서 PK/FK를 **이미 수집하고 있다**(ADR-016). 추가 데이터 수집이 0이다.
- 파티션 프루닝 강제(ADR-016)와 정확히 같은 성격의 안전 규칙이며, 같은 위치에 놓이는 것이 일관적이다.
- **결정적이므로 P-4에 정합**하고, fail-closed이므로 P-6에 정합하며, AC 작성이 자명하다.

```
QueryForge Validation 규칙 (신규)
  식별자 성격 컬럼에 SUM/AVG/STDDEV 등 수치 집계를 적용하면
  INVALID_AGGREGATION 반환 + 허용 집계(COUNT, COUNT DISTINCT) 힌트 동봉

  식별자 판정: PK ∪ FK ∪ relationship key ∪ 명명 패턴(*_ID, *_KEY, *_CODE, *_NO)
  Domain Pack에서 컬럼 단위 예외 선언 가능 (오탐 대비)
```

`did_you_mean` 방식의 힌트를 함께 반환하면 SAD 17.2의 오류 복구 루프를 그대로 탄다. 구현 규모 ~40줄.

### 7.3 제안12 상세 — Query Memory는 "메모리"가 아니라 "평가 자산"이다

리뷰 문서는 Query Memory를 런타임 검색 대상으로 본다. 그 관점에서는 PoC 가치가 0이다 — 이력이 없기 때문이다.

관점을 바꾸면 **PoC에서 가장 가치 있는 항목 중 하나**가 된다.

ADR-018은 이 프로젝트의 병목이 **검증**이라고 선언한다. NFR-007/008은 golden set 정답률로 판정된다. 그런데 golden set은 누가 만드나? 1인이 손으로 만든다. 이것이 진짜 병목이다.

성공한 상호작용을 JSONL로 남기면:

```jsonl
{"ts":"...","locale":"ko","utterance":"MME별 성공률 보여줘",
 "resolution":[{"term":"성공률","concept":"success_rate","source":"pack"}],
 "intent":"new_query","spec":{...QuerySpec...},"tool":"query",
 "outcome":"ok","rows":42,"steps":2,"latency_ms":8300}
```

이 로그가 제공하는 것:

1. **Golden set 확장의 원재료** — 실사용 발화를 손으로 지어내지 않아도 된다. GQ- 항목을 로그에서 승격한다
2. **실제 표현 분포 데이터** — §1에서 지적한 "관측되지 않은 문제"를 관측 가능하게 만든다. alias 폭발이 진짜인지 6개월 뒤 데이터로 판정할 수 있다
3. **회귀 탐지** — 동일 발화의 QuerySpec이 바뀌면 즉시 감지
4. **미래 Query Memory 검색의 학습 데이터** — 나중에 벡터화할 때 이미 데이터가 있다
5. **NFR 실측** — steps, latency가 그대로 지표

그리고 **비용이 사실상 0이다.** SAD 20의 감사 요구("모든 실행 SQL과 Tool Call을 구조화 로그로 기록")와 21.3의 관측 항목이 이미 대부분을 요구하고 있다. `resolution` + `spec` 필드를 추가하는 정도다.

단, 로그에 사용자 발화가 들어가므로 **NFR-011(민감정보 로그 노출 0건)과의 관계를 명시**해야 한다. 원칙: 발화 원문과 Spec은 기록, **행 데이터 값은 기록하지 않는다**(필터 리터럴은 마스킹 정책 필요 — 신규 ADR 대상).

---

## 8. 최종 정리

### 8.1 반드시 반영할 것 (PoC 내, 총 비용 ~1주 미만)

| # | 항목 | 근거 | 규모 |
|---|---|---|---|
| **A-1** | **해석 공개 의무 (C-1)** — 팩 미선언 용어를 해석했으면 답변에 명시 | §4.3. 학습 신호의 전제 조건. 이것 없이는 어떤 학습도 성립 불가 | `respond.md` 규칙 + Spec 필드 1개 |
| **A-2** | **Identifier 집계 금지 규칙** — QueryForge Validation | §7.2. `SUM(NODE_ID)` 원천 차단. 결정적·fail-closed | ~40줄 + AC 3개 |
| **A-3** | **Surface Form Normalizer** — NFKC + 접미사/조사 정규화 | §3.2. alias 폭발 문제의 대부분을 사전 없이 해소. ADR-015 자연 확장 | ~50~80줄 + 테이블 테스트 |
| **A-4** | **Interaction Log (NL + Resolution + QuerySpec + 결과)** | §7.3. 평가 자산. 표현 분포 관측 장치. SAD 20/21.3과 거의 중복 | 필드 2개 추가 |
| **A-5** | **명확화 루프에 지표 후보 제시** — `did_you_mean`을 미해소 용어로 확장 | §3.1. UC-010/FR-016의 구체화. 추측 대신 확정 | 기존 힌트 경로 재사용 |
| **A-6** | **Catalog Snapshot에 프로파일 필드 추가** (제안3) | §2.3 제약 하에. `information_schema.STATISTICS` 우선 사용 시 추가 쿼리 0 | 스냅샷 스키마 확장 |

A-1 ~ A-5는 **신규 컴포넌트가 0개**다. 전부 기존 경로의 확장이다.

### 8.2 수정해서 반영할 것

| # | 원 제안 | 수정 내용 |
|---|---|---|
| **B-1** | 제안1·2·6 (Catalog를 SoT로) | **Domain Pack이 SoT로 유지.** Semantic Catalog는 Pack에서 파생되는 재생성 가능 인덱스. §7.1 |
| **B-2** | 제안15·17 (Semantic Memory / Learner) | **신규 저장소·컴포넌트 없음.** SAD 15.2 Candidate Store에 흡수. 학습은 Clarify-Confirm-Log 워크플로. §3.1 |
| **B-3** | 제안7 (Lifecycle 4단계) | **2상태**: `declared` / `candidate`. 자동 승격 없음 |
| **B-4** | 제안16 (Scope 4단계) | **2단계**: `pack` / `session`. `session_aliases`가 이미 후자 |
| **B-5** | 제안8 (General Lexicon) | **사전 → 결정적 정규화기**로 대체 (A-3) |
| **B-6** | 제안18 (Feedback 학습 신호) | 명시적 correction만 evidence. 반복사용·무이의는 제외 (C-2) |
| **B-7** | 제안4·21 (Bootstrapper / Relationship 자동 탐색) | 런타임 컴포넌트 아님. **오프라인 CLI 초안 생성기.** 출력은 사람이 검토·커밋하는 YAML 초안 |
| **B-8** | 제안25 (Provider Facade) | 시그니처 4개 동결 유지. **반환 타입에 `source`/`confidence` 추가**로 확장 |
| **B-9** | 제안12 (Query Memory) | **기록만.** 검색은 DEFER. 위치는 "메모리"가 아니라 "평가 자산" (A-4) |
| **B-10** | 제안20 (Seed Query 평가) | Semantic 검증 아님. **"Schema Smoke Test"**로 명명, SAD 19.1 설치 체크리스트에 연결 |
| **B-11** | 제안33 (`enrich-context`) | 자동 채움 아님. **Domain Pack `semantic.yaml` 필드 목록으로 흡수** (enum 의미, 단위, NULL 의미, magic value, soft delete, timezone) |

### 8.3 PoC 이후로 미룰 것

| # | 항목 | 재평가 게이트 |
|---|---|---|
| **C-1** | Schema Memory / 벡터 스키마 검색 (제안9·10·24) | **액션아이템 #4 (테이블 수) 확인 후.** 수십 규모면 역색인으로 충분, 수백 이상이면 재평가 |
| **C-2** | Query Memory **검색** (제안12 후반) | 로그 3~6개월 축적 후. 실제 패러프레이즈 빈도 측정 |
| **C-3** | Embedding Provider 추상화 (제안30) | C-1 또는 C-2가 GO 판정된 시점 |
| **C-4** | Seed Query Generator (제안19) | S4 설치 리허설 준비 시. 저비용이므로 여유 시 앞당김 가능 |
| **C-5** | Semantic Bootstrapper CLI (제안4) | 두 번째 Domain Pack 작성 착수 시. **1개짜리 팩에 자동 생성기는 손해** |
| **C-6** | 운영 문서 RAG (SAD 15.2) | 기존 계획대로 PoC 이후 |
| **C-7** | 조직 단위 Scope 확장 | 두 번째 조직/DataSource 발생 시 |
| **C-8** | Semantic 성장 모델 (제안34) | 결정 대상 아님. 방향 서술로 보관 |

### 8.4 반영하지 않을 것

| # | 항목 | 기각 사유 |
|---|---|---|
| **D-1** | **General Lexicon** (제안8) | 폐쇄망 조달 곤란 + 상업 납품 라이선스 리스크(ADR-001 결정과 모순) + 실제 실패 지점인 은어에 무효 + 유효 구간은 사전 없이 해결 가능 |
| **D-2** | **Semantic Catalog를 SoT로 이전** (제안2) | ADR-009/019의 CI 방어선 소실. FR-043 붕괴. 충돌 해소 규칙 미정 상태에서 SoT 지정은 순서 오류 |
| **D-3** | **Semantic Learner / 자동 승격** (제안17) | SAD 14.4 명시적 결정 위반. 침묵을 긍정 증거로 취급하는 구조적 결함(§4.2). ADR-018 AC 작성 불가 |
| **D-4** | **9단계 Semantic Retrieval Pipeline** (제안35) | ADR-017 컨텍스트 예산 최대 65% 초과, 8K 옵션에서는 실행 불가. NFR-001 지연 33% 증가 |
| **D-5** | **MDL 도입** (제안32) | 리뷰 문서와 동일 결론. 중복 모델링 계층 |

### 8.5 추가로 필요한 Architecture Decision

| ADR 후보 | 제목 | 핵심 결정 | 우선순위 |
|---|---|---|---|
| **ADR-020** | Semantic Truth 경계 | Domain Pack = SoT. 학습물은 영구 `candidate`. 승격은 Pack diff + 사람 승인. 상태는 2개. §4.3의 C-1~C-5를 규범으로 등록 | **S0 내** |
| **ADR-021** | 표기 정규화 정책 | Surface Form Normalizer 규칙 집합. ADR-015 확장 또는 개정. 로케일별 규칙 파일 위치 | **S0 내** |
| **ADR-022** | 집계 안전 규칙 | Identifier 집계 금지를 QueryForge 결정적 검증 규칙으로. 판정 기준과 Pack 예외 선언 문법 | S1 |
| **ADR-023** | Schema Profiling 수집 정책 | 파티션 한정·CLI 전용·`information_schema` 우선. N-8/NFR-010 준수 경계 명문화 | S1 |
| **ADR-024** | Interaction Log 정책 | 기록 필드, 보존 기간, 마스킹 범위(발화·필터 리터럴), NFR-011과의 관계, golden set 승격 절차 | S1 |
| **ADR-025** | 외부 오픈소스 참조 및 라이선스 경계 | WrenAI 등 참조 시 "설계 참고 허용 / 코드 파생 금지". 파일 단위 라이선스 확인 의무. 코딩 에이전트 지시문 반영 | **S0 내 (착수 전 필수)** |
| **ADR-026** | 평가 재현성 | golden set 실행 시 semantic 버전 핀 고정. 학습물이 평가 결과에 영향을 주지 않음을 보장 | S1 |
| **ADR-010 개정** | Vector / RAG **분할** | 현재 단일 DEFERRED를 ① Schema Retrieval ② Query Memory Retrieval ③ 문서 RAG 로 3분할하고 **각각의 재평가 게이트를 명시**. 리뷰 문서 §41의 지적 수용 | S2 |

### 8.6 액션아이템 추가

| # | 항목 | 관련 | 기한 | 영향 |
|---|---|---|---|---|
| 8 | **테이블 수 / 컬럼 수 실측** (기존 #4 승격) | ADR-010 개정, C-1 | **S1 착수 전** | Schema Memory 도입 여부를 **단독으로 결정**. 현재 최대 미지수 |
| 9 | WrenAI 참조 대상 파일별 라이선스 확인 | ADR-025 | **구현 착수 전** | 상용 납품 가능 여부 |
| 10 | 실사용자 수 / 예상 발화 다양성 | ADR-020, §1 | S1 중 | alias 폭발 문제의 실재성 판정 |

---

## 9. 원래 질문에 대한 직접 답변

**DataLens의 본래 목표와 부합하는가?**
부분적으로. "자연어만으로 조회·분석"(SAD 1)과 "Domain Pack만 교체하면 다른 도메인 적용"(FR-043)이라는 목표에는 부합한다. 그러나 Semantic Catalog를 SoT로 옮기는 제안은 FR-043을 **직접 훼손**한다.

**기존 QuerySpec / QueryForge / Dataset Lineage / MCP와 충돌하는가?**
선언적으로는 충돌하지 않으나(리뷰 문서가 명시적으로 보존을 선언), **정량 제약 4건과 충돌한다**: 컨텍스트 예산(ADR-017), 지연(NFR-001), DB 부하 정책(N-8/NFR-010), 검증 체제(ADR-018). §2 참조.

**과도한 설계인가?**
그렇다. 신규 논리 컴포넌트 7개 + Vector 스택을 15주 1인 일정에 제안하면서 비용 추정이 전혀 없다. 반면 §8.1의 6개 항목으로 **같은 문제의 대부분을 신규 컴포넌트 0개로** 처리할 수 있다.

**PoC와 제품화 양쪽에서 가치가 있는가?**
분리해야 한다. **양쪽 모두 가치**: Identifier heuristic, Schema profiling, QuerySpec 로깅, 표기 정규화, 해석 공개. **제품화에서만 가치**: Schema Memory, Query Memory 검색, Semantic Catalog, Scope 계층. **양쪽 모두 가치 없음**: General Lexicon, confidence decay, 4단계 scope.

**WrenAI 참고가 적합한가?**
아이디어는 적합, 적용 범위는 축소 필요. **가장 가치 있는 참고는 코드가 아니라 `enrich-context`의 "스키마만으로 알 수 없는 정보 목록"** — Domain Pack 필드 체크리스트로 즉시 활용 가능하고 비용 0. 반대로 Schema Indexing은 WrenAI의 사용 맥락(임의 대규모 웨어하우스)이 DataLens와 다르다. **라이선스 확인이 선행 조건이다.**

**General Lexicon + Semantic Catalog + 3-Memory 구조가 합리적인가?**
개념적 계층 구분(범용어 / 구조화 진실 / 스키마 / 경험 / 조직 언어)은 **개념 모델로는 정확하고 유용하다.** GLOSSARY에 개념으로 등재할 가치가 있다. 그러나 이것을 **5개의 물리 저장소로 구현하는 것은 별개 문제**이며, 현 단계에서는 정당화되지 않는다. 개념 분리와 물리 분리를 구분해야 한다.

**Vector DB 구분이 가능한가?**
가능하다. **PoC에서는 어느 용도로도 불필요.** 장기적으로는 Query Memory 검색에서만 진짜 필요할 가능성이 높고, Schema Retrieval은 규모 확인 후 판단, 용어 매칭은 정규화+되묻기로 대체, 문서 RAG는 원래 범위 밖. §6.

**잘못된 Semantic 누적을 어떻게 통제하나?**
자동 학습을 하지 않는 것이 가장 확실하다. 하더라도 §4.3의 C-1~C-5가 **동시에** 성립해야 한다. 특히 **C-1(해석 공개)이 없으면 부정 신호를 수집할 경로 자체가 없어** 어떤 학습 메커니즘도 원리적으로 작동하지 않는다.

**더 단순하거나 더 좋은 대안이 있는가?**
있다. §3의 Clarify-Confirm-Log 루프 + §3.2의 Surface Form Normalizer. 신규 컴포넌트 0개, 추가 LLM 호출 0회, 추가 컨텍스트 ~50토큰, 그리고 **추론이 아니라 확정된 데이터**를 얻는다.

---

## 10. 이 검토 자체의 한계

1. 대상 DB의 테이블/컬럼 수를 모른다. **이 값 하나가 Schema Memory 판정을 뒤집을 수 있다.** 액션아이템 #8로 등록했다.
2. 실사용자 수와 발화 다양성을 모른다. alias 폭발 문제의 실재성 판정은 S1 이후 데이터에 의존한다.
3. ADR-001 실측(12B vs 26B)이 미완이다. 26B/8K로 확정되면 컨텍스트 관련 판정이 **더 보수적으로** 바뀐다.
4. WrenAI 라이선스는 공개 정보 기준으로 확인했으나, **파일 단위 확인은 구현 착수 전 별도로 수행해야 한다.**

---

# 11. 부록 — `SEMANTIC_ARCHITECTURE.md` 추가 검토

리뷰 문서와 내용이 대체로 일치하므로 §1~§9의 판정은 대부분 유지된다. 그러나 **리뷰 문서에 없던 6건**이 확인되었고, 그중 1건은 판정을 강화해야 한다.

## 11.1 [판정 변경] §32 "PoC에서 Phase 1~3" — 가장 심각한 불일치

두 문서의 PoC 범위 주장이 서로 다르다.

| 문서 | PoC 범위 주장 |
|---|---|
| `WRENAI_..._REVIEW.md` §49 | Phase 1 = 현재 PoC (Catalog Snapshot, Profiling, QuerySpec, Basic Semantic Provider). Vector는 Phase 3 |
| `SEMANTIC_ARCHITECTURE.md` §32 | **"PoC에서는 최소한 Phase 1~3의 구조를 고려"** |

그런데 §32의 Phase 2~3 내용은 다음과 같다.

```
Phase 2 — Semantic Retrieval : Schema Embedding, Vector Search, Semantic Resolver, LLM Context
Phase 3 — Query Memory       : NL/QuerySpec/SQL, Execution Result, Vector Index, Similar Query Retrieval
```

즉 **PoC에 임베딩 모델 + Vector DB + Vector 검색 2종을 넣자는 주장**이다. 이는 다음과 정면 충돌한다.

- **ADR-010 (`DEFERRED`)** — "PoC 범위에서 제외한다. 구현체는 `DictionarySemanticProvider` 하나만 작성한다"
- **SAD 23.2** — RAG / Vector Store는 "Provider 인터페이스만"
- **SAD 24** — RAG Semantic Provider는 PoC 이후 확장 우선순위 **3번**
- **§2.1 / §2.2 본 검토** — 컨텍스트 예산 최대 65% 초과, 지연 33% 증가
- **ADR-014** — 반입 산출물에 임베딩 모델 가중치 추가, NFR-012(설치 2시간) 영향

또한 §32의 Phase 3(Query Memory)은 **PoC 종료 시점에 저장된 이력이 사실상 0건**이므로, 벡터 인덱스를 만들어도 검색할 대상이 없다. 구현 비용만 발생하고 효용은 0이다.

**판정: §8.3의 C-1 · C-2를 `DEFER`에서 유지하되, "PoC 도입 검토 대상 아님"으로 강도를 올린다.** ADR-010의 상태를 변경하려면 리뷰 문서가 아니라 **ADR 개정 절차**를 거쳐야 한다(§11.5).

권장 대안은 §8.1의 A-4(Interaction Log)다. **Phase 3의 저장 부분만 PoC에서 수행하고, 인덱싱·검색은 데이터가 쌓인 뒤에 붙인다.** 데이터는 소급 생성이 불가능하지만 인덱스는 언제든 소급 생성이 가능하다 — 이 비대칭이 순서를 결정한다.

## 11.2 [신규] §14 Semantic Catalog는 배포 구성요소를 늘린다 — 아무도 언급하지 않음

§14는 Catalog에 테이블 7개를 제안한다.

```
semantic_concept / semantic_metric / semantic_dimension / semantic_entity
semantic_term / semantic_relationship / semantic_feedback
```

`semantic_feedback`처럼 런타임에 쓰기가 발생하는 테이블이 포함되어 있으므로, 이것은 **읽기 전용 아티팩트가 아니라 상태를 가진 저장소**다. 그런데 두 제안 문서 어디에도 **이 DB가 어디에 사는지**가 없다.

현재 배포 구성(ADR-014)은 `api` / `queryforge` / `llm` 3개 컨테이너이며, 별도 DB 컨테이너가 없다. 즉 다음 중 하나를 결정해야 한다.

| 선택지 | 영향 |
|---|---|
| SQLite 파일 | 그나마 현실적. ADR-008이 세션 저장에 SQLite를 택한 것과 일관. 단 볼륨 마운트·백업 절차 신설 |
| 신규 DB 컨테이너 | **ADR-008이 명시적으로 회피한 것**: "외부 저장소는 폐쇄망 배포 구성요소를 늘린다" |
| 대상 DB에 저장 | **절대 불가.** ADR-016 — 대상 DB는 외부 자산, DDL 금지, 읽기 전용 계정 |
| 인메모리 재생성 | Domain Pack에서 파생된다면 가능. **§8.2 B-1이 제안하는 방향** |

여기서 §8.2 B-1(Domain Pack = SoT, Catalog = 파생 인덱스)의 실무적 이점이 분명해진다. **파생 인덱스는 인메모리로 충분하며 배포 구성요소가 0개 늘어난다.** 반면 Catalog를 SoT로 만들면 폐쇄망에서 백업·복구·마이그레이션·버전 관리 대상이 하나 늘어나고, 운영 인력이 없는 고객사 환경에서 이는 실질적 리스크다.

**B-1의 근거로 추가한다.**

## 11.3 [신규] §12 Seed Query 예시가 ADR-016을 위반한다 — 실증

§12가 제시하는 자동 생성 SQL 예시 3개 중 **2개가 현재 설계에서 실행 거부된다.**

```sql
SELECT * FROM PM_EPC_KPI_1M LIMIT 100                          -- 파티션 키 범위 조건 없음
SELECT SUM(SUCCESS_CNT) FROM PM_EPC_KPI_1M                     -- 파티션 키 범위 조건 없음
SELECT NODE_TYPE, SUM(SUCCESS_CNT) FROM ... GROUP BY NODE_TYPE -- 동일
```

전부 `MISSING_TIME_RANGE`로 거부된다(ADR-016, SAD 16.1). 그리고 만약 QueryForge를 우회해 직접 실행한다면 **N-8 위반 + NFR-010(프루닝 미적용 쿼리 0건) 즉시 실패**다.

이는 사소한 오타가 아니라 **Seed Query Generator가 partition-aware하지 않으면 이 프로젝트에서 동작하지 않는다**는 증거다. 생성기는 반드시 `partitions.yaml`을 읽어 기본 기간을 주입해야 한다.

**§8.3 C-4(Seed Query Generator)에 필수 요건으로 추가한다:** 생성 결과는 반드시 QuerySpec 형태여야 하고(raw SQL 금지), 파티션 키 필터가 자동 주입되어야 하며, 생성 즉시 QueryForge Validation을 통과해야 한다. 통과 못 하는 Seed는 폐기한다.

## 11.4 [신규] §7 복합 Relationship — 이 문서에서 가장 즉시 실행 가능한 발견

§7과 §22가 반복해서 지적하는 내용이다.

```
PM_EPC_KPI_1M.NODE_TYPE + NODE_ID   ↕   CM_EPC_INFO.EQUIP_TYPE + EQUIP_ID
```

**이것은 Semantic 문제가 아니다. PoC의 구조적 요구사항이다.** 그리고 semantic 논의와 무관하게 지금 확인해야 한다.

- ADR-002는 JOIN을 **relationship id 참조로만** 허용한다. 따라서 복합 관계는 `relationships.yaml`에 **하나의 relationship id로 선언 가능해야** 한다.
- ADR-009는 FK 부재 시 `relationships.yaml`이 "선택 사항이 아니라 PoC 필수 산출물"이라고 명시한다.
- 그런데 **현재 `relationships.yaml`의 스키마가 복합 키(2개 이상 컬럼 쌍)를 표현할 수 있는지 어느 문서에도 정의되어 있지 않다.** ADR-009와 SAD 14.2는 파일 존재만 언급하고 스키마를 정의하지 않았다.
- 단일 컬럼 조인만 지원하도록 구현되면 **UC-003(관계 확장 조회)과 대표 시나리오 U3가 EPC 데이터에서 동작하지 않는다.** 이는 PoC 성공 판정 기준 1번(5턴 완주) 실패를 의미한다.

**액션아이템 #11로 등록한다. 기한은 S1 착수 전, 영향은 PoC 성공 판정 직결.** 자동 탐색(제안21) 여부와 무관하게 **선언 스키마 자체가 복합 키를 지원해야 한다.**

이 검토를 통틀어 **가장 비용이 낮고 가장 시급한 발견**이다.

## 11.5 [신규] §15가 `ACCEPTED` 상태의 ADR을 문서로 뒤집는다 — 절차 문제

§15의 문장:

> 기존 설계처럼 `semantic.yaml`을 사용자가 지속적으로 관리하는 방식은 사용하지 않는다.
> Semantic Source of Truth는 Catalog가 담당한다.

ADR-009는 상태가 **`ACCEPTED`**이며, ADR.md 서두는 상태 정의에서 `ACCEPTED` = "확정. **변경 시 ADR 개정 필요**"라고 명시한다. 또한 ADR.md의 문서 목적은 이렇게 되어 있다.

> 미확정 항목을 문서 본문에 "TBD"로만 남기면 결정이 누락되거나 **구현 단계에서 임의로 확정된다.** 이를 방지하기 위해…

설계 문서가 `ACCEPTED` ADR을 선언문으로 무효화하면 이 장치가 무력화된다. 내용의 옳고 그름과 별개로(본 검토는 §8.4 D-2에서 내용도 기각한다) **절차상 반드시 ADR-009 개정 제안으로 제출되어야 한다.**

이는 형식주의가 아니다. ADR-009/ADR-019가 두 번 반복하는 논리 — "1인 개발 체제에는 리뷰어가 없으므로 명시적 절차와 자동 검사가 유일한 방어선" — 이 그대로 적용된다. 제안 문서가 ADR을 조용히 덮어쓸 수 있으면, 코딩 에이전트는 어느 쪽을 진실로 삼을지 알 수 없다.

## 11.6 [보강] §2.2가 General Lexicon 조달 방식을 명시했다 — D-1 기각 근거 강화

§2.2:

> 초기 데이터는 공개 사전, 동의어 데이터, **WordNet 계열** 등의 검증 가능한 언어 자원을 ETL하여 구축하는 방향을 고려한다.

조달처가 구체화되면서 **§8.4 D-1의 기각 근거가 오히려 강해진다.**

- 한국어 WordNet 계열(KorLex 등)은 연구 목적 조건이 붙는 경우가 있어 **상용 납품 전 라이선스 검토가 필수**다. 일본어 WordNet은 상대적으로 관대하나 Princeton WordNet 파생이므로 고지 의무가 따른다.
- ADR-001이 Gemma 4를 택한 명시적 이유가 **"해외 고객사 납품에서 커스텀 라이선스가 유발하던 법무 검토 리스크가 없다"**였다. 모델에서 회피한 리스크를 사전에서 다시 들이는 셈이다.
- 그리고 §9가 스스로 인정하듯, 실제 문제 표현은 `소통률 / 개통률 / 부착률 / 호성공률` 같은 **업계 은어**다. **어떤 WordNet에도 없다.**

한편 §8에 예시로 든 관계를 보면:

```
성공 ├─ 성공하다 ├─ 성공한 └─ 성공됨      ← 활용형. 사전이 아니라 형태 정규화
비율 ├─ 율 ├─ 비중 └─ 백분율             ← 표기 변형 + 접미사. 정규화로 처리
원인 ├─ 이유 ├─ 사유 └─ 요인             ← 진짜 유의어. 그런데 지표명에 거의 안 쓰임
```

**세 그룹 중 둘은 사전이 필요 없고, 사전이 필요한 하나는 지표 해석에 거의 기여하지 않는다.** §3.2의 Surface Form Normalizer 제안이 정확히 이 지점을 겨냥한다. D-1 유지.

## 11.7 §33 "구현 시 재검토 사항" 12건에 대한 본 검토의 응답

문서 스스로 12개 미결 항목을 나열했다. **착수 2주(S0) 안에 12개를 결정하는 것은 비현실적**이므로, 본 검토가 답한 것과 남는 것을 구분한다.

| # | `SEMANTIC_ARCHITECTURE.md` §33 항목 | 본 검토의 응답 |
|---|---|---|
| 1 | General Lexicon 공개 데이터 | **소멸.** D-1 기각 → 결정 불필요 |
| 2 | Lexicon 저장 방식 | **소멸.** 동일 |
| 3 | Semantic Catalog DB Schema | **소멸/축소.** B-1로 파생 인덱스화 → DB 스키마 불필요 (§11.2) |
| 4 | Vector DB 선정 | **연기.** C-1/C-2 게이트 통과 후 (§6) |
| 5 | Embedding Model 선정 | **연기.** 동일 |
| 6 | Semantic Confidence 정책 | **대체.** 2상태(`declared`/`candidate`)로 축소 → 정책 불필요 (B-3) |
| 7 | Semantic Scope 정책 | **축소.** `pack` / `session` 2단계 (B-4) |
| 8 | Query Memory 보존 정책 | **필요. → ADR-024** |
| 9 | 개인정보 포함 질문의 저장 정책 | **필요. → ADR-024.** NFR-011과 직결. 문서가 이걸 짚은 것은 정확하다 |
| 10 | Semantic Candidate 자동 승격 기준 | **소멸.** 자동 승격 자체를 기각 (D-3) |
| 11 | Schema 변경 시 Memory 재구성 | **축소.** SAD 18.2 영향 분석 확장으로 흡수 (제안23 ACCEPT) |
| 12 | QuerySpec ↔ Semantic Memory 연결 | **대체.** Interaction Log 스키마로 (A-4, ADR-024) |

**12건 중 6건이 소멸하고, 2건이 축소되며, 2건이 연기되고, 실제로 지금 결정해야 하는 것은 2건(모두 ADR-024)이다.** 이것이 §3의 대안 노선을 택했을 때의 실질적 이득이다 — 기능이 아니라 **결정 부담**이 줄어든다.

## 11.8 부록 반영 사항 요약

| 구분 | 변경 |
|---|---|
| 판정 변경 | C-1·C-2: `DEFER` 유지하되 **"PoC 도입 검토 대상 아님"으로 강화** (§11.1) |
| 근거 보강 | B-1 ← §11.2 (배포 구성요소), D-1 ← §11.6 (WordNet 조달), C-4 ← §11.3 (파티션 위반 실증) |
| 신규 액션아이템 | **#11 `relationships.yaml` 복합 키 지원 여부 — S1 착수 전, PoC 성공 판정 직결** (§11.4) |
| 신규 ADR 요건 | ADR-024에 **개인정보/발화 저장 정책** 명시 포함 (§11.7 #9) |
| 절차 이슈 | ADR-009는 `ACCEPTED`. 변경은 설계 문서가 아니라 **ADR 개정 제안**으로 (§11.5) |

**부록 반영 후에도 §8의 전체 구조는 바뀌지 않는다.** 오히려 `SEMANTIC_ARCHITECTURE.md`가 제공한 구체성(배포 위치 미정, 파티션 미고려 SQL, WordNet 조달, 12개 미결 항목)이 §8.1의 저비용 노선을 택할 근거를 강화한다.

---

## 개정 이력

| 버전 | 일자 | 내용 |
|---|---|---|
| 0.1 | 2026-08-10 | 초안. 제안 35건 판정, 신규 ADR 후보 8건, 액션아이템 3건 등록 |
| 0.2 | 2026-08-10 | `SEMANTIC_ARCHITECTURE.md` 반영. §11 부록 추가. 판정 강화 1건, 신규 발견 5건, 액션아이템 #11 등록 |
