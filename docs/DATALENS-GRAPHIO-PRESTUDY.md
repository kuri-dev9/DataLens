# graphio-probe-rca 이식 사전 조사 (DataLens 이관용)

| 항목 | 내용 |
|---|---|
| 상태 | **사전 조사(제안).** 확정 결정은 DataLens의 ADR/SAD 절차를 거쳐야 한다 |
| 작성 | 2026-09-29 |
| 출처 | graphio-probe-rca · QueryForge · DataLens 3개 저장소 전수 조사 + 실서버 트레이스 1건 |
| 다음 단계 | 이 문서를 DataLens 저장소로 가져가 상세 설계 → 구현 |
| 원칙 | 개발 중 새 항목이 나오면 이 문서를 수정·추가한다 (완결 문서가 아니다) |

---

## 0. 배경 — 세 프로젝트의 역할

```
사용자 질문 → DataLens (HTTP + bounded Agent + Ollama gemma4:26b + Memory)
                 ↓ MCP
              QueryForge (읽기 전용 MySQL MCP Server · QuerySpec 검증 10단계)
                 ↓
              MySQL (파티션 테이블)
```

- **graphio-probe-rca** — LTE RCA 도메인 참조 구현. 이식할 지식·교훈의 원천.
  KPI 58 산식, 불변 규칙 18, 실전 결함 기록(ERRORS.md) 20여 건.
- **DataLens·QueryForge** — 범용 엔진. "어떻게 안전하게 조회하는가"는 완성,
  "이 도메인에서 무엇이 맞는 답인가"는 설계상 비워 둠(ADR-009: Core에 도메인 0).

세 프로젝트는 서로 모른 채 같은 원칙을 구현했다: LLM은 새로운 사실을 만들지
않는다 · 컨텍스트 예산 관리 · fail-closed · 부분 결과 보존. **공학 원칙은 이식할
필요가 없고, 이식할 것은 도메인 지식과 아래의 구체 메커니즘이다.**

---

## 1. 합의된 방향 (이번 조사에서 확정)

1. **DataLens·QueryForge는 범용 코어로 유지한다.** RCA 지식은 코드가 아니라
   **Domain Pack(=Skill)** 산출물로 분리한다. "정형화되는 것"은 도구가 아니라
   팩을 장착한 배포 인스턴스뿐이다.
2. **RAG(벡터 유사도 회수)는 배제한다.** 회수는 하되 검색이 아니라 **결정적 색인
   조회**로 한다(질문이 확정한 테이블·지표 → `applies_to` 키 조회). 근거:
   - 규칙성 지식은 "가끔만 회수"되는 순간 규칙이 아니다. 회수 실패가 조용하다.
   - 규모가 작다(KPI 58·룰 39·Cause 35 — 열거 가능, YAML 몇 개 분량).
   - 지연 예산: 병목은 검색 품질이 아니라 LLM 추론(실측: QueryForge 60~96ms,
     LLM 사고 8~16초).
   - 키 미해소는 조용히 넘기지 않고 사용자에게 묻는다(Clarify-Confirm-Log,
     ADR-020) — 그 물음이 다음 버전 팩의 백로그가 된다.
   - 임베딩은 기존 Memory처럼 **회수 보조**(Recipe·Glossary 힌트)로만.
3. **팩은 사전처럼 쓴다.** 통짜 산문이 아니라 항목(지표 하나·규칙 하나) 단위,
   항목마다 색인 키. graphio `metrics/kpi.py`(KpiDef 1개 = 항목 1개)가 원형.
4. **세션·대화 이력 관리는 범용 기능이므로 DataLens 코어에 넣는다.**
   (별도 트랙 — 이 문서 범위 밖. 참조 해소·영속화·과거 세션 검색.)

### Skill(Domain Pack) 관리 뼈대 — 상세 설계 시 반영

- 저장·버전: git 형상 관리 + `pack.yaml`의 version 필드
- 로딩·선택: progressive disclosure — 매니페스트(이름+한 줄)만 상시,
  본문은 결정적 색인 조회로 조건 주입
- 검증 CI: 스키마 검증 + **QueryForge 카탈로그 스냅샷 대조**(드리프트 검출)
  + `examples.jsonl` 골든 테스트 + 민감 컬럼 노출 검사
- 갱신: `declared`/`candidate` 2상태, 자동 승격 금지, 사람 승인
  (기존 `/v1/memory/terms`가 candidate 수집함)
- 감사: 응답 metadata에 팩 이름·버전 기록

---

## 2. DataLens 이식 — 확정 3건

### A. 답변 근거 검증기 (확정)

**무엇**: 최종 `answer` 문장 속 수치가 이번 턴에 모델이 실제로 받은 사실에
존재하는지 대조. 없으면 그 문장을 **고치지 않고 폐기**하고 `warnings`에 사유 기록.

**메커니즘 — 재실행도 정오 판정도 없다**:

```
턴 진행 중 (기존 흐름 변경 없음):
  도구 결과가 DataLens를 경유할 때 수치를 집합에 복사
  (preview 행, row_count, describe 통계 — 이미 메모리에 있는 객체)
턴 종료 시 (추가되는 유일한 단계):
  answer에서 수치를 정규식 추출 → 집합 대조 → 미존재 수치 문장 폐기
```

- 추가 쿼리 0회, LLM 재호출 0회. 비용은 정규식 + set 조회.
- 책임 분리: 수치의 **정확성**은 QueryForge가 이미 보장(실 DB 실행 결과).
  검증기는 **충실성(grounding)** — "모델이 그 수치를 본 적 있는가"만 판정.
- preview 5행 제약과 정합: 모델이 전체 데이터 수치를 말하려면 describe/transform
  으로 사실을 먼저 확보해야 하고, 확보한 순간 집합에 들어와 통과된다.
  "본 것만 말하거나, 말하려면 도구로 확인하라"로 압박하는 효과.

**설계 결정 필요 (유일)**: 파생 수치 정책 — 모델이 계산한 값(예: row_count 128
− 표시 3행 = "이외 125개")을 얼마나 허용할지. graphio는 허용 오차 ±0.06 +
작은 정수(≤12) 허용으로 완화. 엄격 노선은 "산수 금지, 도구로 확인" 유도.

**한계 (정직하게)**: 해석 오류는 못 잡는다 — 수치는 맞는데 증가/감소 방향을
반대로 쓰는 것. graphio 실측: "수치는 가드레일이 지키지만 해석은 지키지 못한다."

**참조 구현**: `graphio-probe-rca/src/graphio/rca/llm.py` —
`validate()` · `_pack_numbers()` · `_number_ok()` (수치 포맷 다중 등록,
허용 오차, 폐기 사유 기록 `dropped_claims`). 폐기는 기존 `warnings` 채널로 노출
(mock 계약의 `tool_call rejected` 표기가 선례 — 신규 이벤트 불요).

### B. 실패 이유 어휘 확장 (확정)

**무엇**: "왜 실패/모름인지"를 사용자가 구분하도록 이유 코드를 추가한다.
채널은 이미 있다(`failed_step {index, tool, upstream_code, intent}` + `warnings`
+ `recovery`) — 이식할 것은 채널이 아니라 **어휘**다.

| 이유 코드(안) | 뜻 | 사용자가 바꿀 방향 |
|---|---|---|
| 데이터에 없음 | DB/카탈로그 범위 밖 | 다른 소스에서 확인 |
| 표본 부족 | 분모가 얇아 판단 불가 | 기간·범위 확대 |
| 예산에 잘림 | 시스템은 갖고 있으나 컨텍스트에 못 실음 | 재질문/좁힌 질문 |
| 검증 폐기 | A 검증기가 문장을 버림 | 폐기 사유 확인 |

**근거**: graphio ERRORS **G-8** — "모른다"가 *미보유*와 *못 보여줌* 두 뜻을
가지면 그중 하나는 거짓말이다. 같은 문장으로 거절되면 운영자가 구분 불가.

### C. 무응답 워치독 (확정 — 실측 트레이스가 근거)

**무엇**: 어떤 하위 호출(특히 LLM)도 전역 deadline을 넘겨 살아남을 수 없게
워치독으로 강제하고, 끊을 때 **반드시 `error` 이벤트를 흘린다**
(`failed_step` 동봉 — 부분 결과 보존은 기존 구현 활용).

**근거 (2026-09-29 실서버 트레이스)**: "PGW별 데이터 사용량" 질문 →
schema 3회 성공 → query 2회 rejected → **이후 625초+ 동안 ping만 36회.**
설정상 전역 deadline 240초·LLM 타임아웃 120초가 모두 발동하지 않았다.
bounded Agent 4중 경계 중 시간 경계가 실전에서 구멍. (근본 원인 미규명 —
상세 설계 때 조사.)

**참조**: graphio ERRORS **G-4**(Ollama는 실패를 예외가 아니라 무응답·빈 응답
으로 표현) · **E-1**(조용한 죽음) · QueryForge `adapters/mysql.py`의
`_QueryWatchdog`(타임아웃 시 강제 종료 패턴 — 같은 스택 안의 선례).

---

## 3. DataLens 이식 — 조건부 후보 2건

### D. 에스컬레이션 재시도 (후보)

거부가 반복되면 같은 방식으로 다시 묻지 않는다(graphio G-3 원칙). 2회째부터는
오류 문구 대신 **올바른 QuerySpec 완성 예시**를 주입. 예시의 정식 공급처는
Domain Pack `examples.jsonl` + Memory Recipe — 성공 사례 하나면 4회 시도가
1회가 된다.

**근거 (같은 트레이스)**: 시도 1이 "select 항목은 객체여야 한다"를 정확히
알려줬는데 시도 2가 반영 못 함. 교정 힌트가 `'count' was expected`로 오도한
정황도 있음(`SUM` 대문자가 소문자 enum 불일치였을 가능성 — 추정, 미검증).

### E. 질문 관련성 기반 컨텍스트 선별 (조건부)

예산 때문에 자를 때 "무엇을 물었는지"를 남길 기준에 포함(graphio G-8의 해법,
`_question_hits` — 한국어 조사 제거 후 대조). **컨텍스트 초과가 실제로 발생할
때만 필요** → 선행 실측:

- Ollama 응답의 `prompt_eval_count`를 로깅해 `num_ctx=8192`와 비교
- 재시도 누적 턴(위 트레이스 같은)이 좋은 시험 대상
- Ollama는 초과 시 **경고 없이 앞부분을 자르므로**(graphio G-3에서 17,703자로
  실증) 로그 없이는 겪고 있어도 모른다
- 실측 진행 중 (사용자 목업 테스트)

---

## 4. 검토 후 제외한 것 (재논의 방지용 기록)

| 항목 | 제외 사유 |
|---|---|
| 프롬프트 끝 규칙 반복 (graphio G-5) | 모델 최소 gemma4:26b 전제 + native tool calling — 원래 용도(작은 모델 규율)가 없음. 토큰 낭비 |
| 템플릿 폴백 ("LLM 없이도 완결") | DataLens는 계획 자체를 LLM이 세워 대체 엔진이 없음 — 구조상 불성립. 부분 결과 보존(datasets·warnings·failed_step을 에러 응답에 동봉)은 이미 구현됨 |
| RAG / Vector DB / 9단계 Retrieval | §1.2 — DataLens SEM-REVIEW-001 기각과 동일 결론 |
| Semantic Learner (자동 승격) | ADR-020 기각 유지 — 의미 오류는 에러 없이 실행되므로 침묵이 긍정 증거로 계수됨 |

---

## 5. QueryForge — 코드 변경 없음 (확정)

graphio의 강점(도메인 판단·LLM 다루기)은 QueryForge 헌법(ADR-009 도메인 0,
ADR-001 LLM 범위 밖)상 들어갈 수 없고, 완성도도 가장 높다(테스트 1.9배,
TODO 0건). **기능 추가 없이 그대로 사용.** 남는 것은 운영 설정 3건:

1. `query_cost` 임계 4개 채우기 — 현재 전부 null이라 Cost Guard 무력.
   더미 데이터 대량 적재 후 EXPLAIN 실측 기준으로 설정
2. 정책 구성 — `allowed_tables`. 민감 컬럼은 코드가 아니라 **MySQL 컬럼 단위
   GRANT**로 차단(읽기 전용 계정에 권한을 안 주면 fail-closed로 동작)
3. 카탈로그 refresh 운영 절차 — 적재 때마다 `catalog refresh` 필수
   (스키마가 같아도 data_range가 바뀌면 재게시하는 구조)

아이디어 백로그(나중 문서화 합의): 저표본 경고(`LOW_SAMPLE_SIZE`) ·
절단 시 "나쁜 순" 정렬 · 빈 `SemanticProvider` 자리에 컬럼 role 기반
집계 안전 확장.

---

## 6. 신규 프로젝트 후보

### 6.1 더미 데이터 생성기 (1순위 — 사용자 과업)

쓰기 작업이라 읽기 전용인 두 저장소에 못 들어감. 대상: QueryForge가 바라보는
MySQL 파티션 테이블. graphio `sim/`에서 가져올 원칙:

- **분석이 성립하는 데이터** > 그럴듯한 데이터 (E-3: 균등 샘플링은 그 축의
  분석 검증을 불가능하게 함 — 실제 편중을 Zipf 등으로 재현)
- **필드 간 논리 종속 유지** (E-2: 독립 샘플링은 "성공인데 답 없음" 같은
  불가능 레코드를 만든다 — 조건부 결합분포로)
- **차원당 표본 수를 먼저 계산** (E-4: 총량이 아니라 셀당·그룹당 표본이
  검증 가능성을 결정)
- 지각 도착·결측 의도 주입 (없으면 그 처리 로직이 영원히 미검증)
- 결측은 0이 아니라 NULL
- **ground truth 동봉** (자동 채점의 전제)
- 적재 후 QueryForge `catalog refresh` 연동 (§5.3)

시작점: QueryForge `tests/fixtures/profiles/`의 파티션 프로파일 8종 SQL.

### 6.2 Domain Pack 저장소 (telco-rca)

코드가 아니라 지식 산출물 — 별도 git 저장소 + 자체 CI(§1 뼈대). 초기 콘텐츠는
graphio에서 번역: `metrics/kpi.py`(산식 58) · `schema/codes.py`(Cause 테이블·
판정 함수) · CLAUDE.md 불변 규칙(Call Type 고정 · 정상 해제 14 Cause ·
First Error 원인/증상 · down 축 · 롤업 카운트 합산 · 최소 표본 등).
이 규칙들의 공통점: **어기면 쿼리가 실패하는 게 아니라 그럴듯한 틀린 숫자가
조용히 나온다** — QueryForge 10단계 검증이 못 잡는 층.

### 6.3 평가 하네스 (선택)

ground truth 기반 자동 채점(graphio `score` 사상). "정답 있는 데이터가 없었다면
Top-1 23%와 85%를 구분할 방법이 없었다"(ERRORS 결언). 단 **D-5 교훈**: 채점기
버그는 구현 품질을 오판시킨다 — 정확도가 낮으면 모델보다 채점기를 먼저 의심.
규모가 작으면 6.1에 포함 가능.

---

## 7. 구현 시 참조 지점 (graphio-probe-rca)

| 파일 | 볼 것 |
|---|---|
| `src/graphio/rca/llm.py` | A 검증기 원형: `validate` · `_pack_numbers` · `_number_ok` · `dropped_claims` / E 원형: `ask` · `_question_hits` / `_repair_json` · 빈 응답 RuntimeError |
| `src/graphio/metrics/kpi.py` | 팩 항목 구조의 원형 (KpiDef 레지스트리) |
| `src/graphio/schema/codes.py` | Cause 코드 테이블 + 판정 함수 (팩 콘텐츠) |
| `src/graphio/sim/` | 6.1 원칙의 구현 (profile 학습·scenarios·generator) |
| `ERRORS.md` | G-3(프롬프트 예산·재시도 축소) G-4(빈 응답) G-5(스키마 강제) G-8(질문 관련성) G-9(모름≠0) E-1~E-4(시뮬레이터) |
| `CLAUDE.md` 불변 규칙 1~18 | 팩으로 번역할 도메인 규칙 목록 |

## 8. 실측 트레이스 기록 (2026-09-29)

질문 "PGW별로 데이터 사용량 확인하고 싶어" (실서버, SSE):

- 0→16s LLM 사고 → `schema list_tables` 50건 **truncated:true** (QF 96ms)
- 24s `list_tables pattern=PGW` 15건 — **절단 고지→좁힘 설계 실증**
- 34s `describe_table PM_CEI_PGW_5M` 90컬럼
- 66s/85s `query` 2회 rejected (select 문자열·`SUM` 대문자 추정) — 시도 1의
  교정을 시도 2가 미반영, 힌트가 `'count' was expected`로 오도 정황
- 이후 **625s+ ping만 36회** — deadline 240s·LLM timeout 120s 미발동 (→ C)
- 지연 구성: QueryForge 60~96ms, 나머지 전부 LLM (→ §1.2 지연 논거 실증)
