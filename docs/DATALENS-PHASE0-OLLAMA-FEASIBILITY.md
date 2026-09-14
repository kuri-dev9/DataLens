# DataLens Phase 0 — Ollama / Agent Feasibility Gate 결과

| 항목 | 내용 |
|---|---|
| 문서 ID | DATALENS-PHASE0-OLLAMA-FEASIBILITY |
| 실행일 | 2026-09-08 |
| 상태 | `BLOCKED_BY_ENVIRONMENT` (정적 contract 검증 완료) |
| Spike | `spikes/ollama_agent_feasibility.py` |

## 1. 범위와 환경

이 Gate는 production 구현이 아니라 `LLMProvider + Bounded Agent + QueryForgeClient` 구조의 구현 전
불확실성 검증이다. 조사 환경에는 `ollama` executable이 없고 `127.0.0.1:11434`의 Ollama API와
`127.0.0.1:8080`의 QueryForge가 실행 중이지 않았다. 지시대로 설치 또는 모델 다운로드를 하지 않았다.

따라서 실제 `gemma4:26b` tool calling, 한국어 multi-turn, latency는 실행하지 못했다. QueryForge의 현재
JSON Schema와 구현을 이용한 static validation, reference 분석, recovery/workflow feasibility 분석까지
완료했다.

## 2. Reference 분석

대상은 QueryForge `scripts/manual_ollama_agent.py`다.

### 2.1 재사용할 개념

- Ollama `/api/chat`의 native `tools`와 응답 `tool_calls`
- MCP `tools/list`의 `inputSchema`를 Ollama function의 `parameters`로 전달하는 mapping
- 모델의 tool argument를 JSON object로 정규화한 뒤 validation하는 순서
- QueryForge structured result를 `role: tool` message로 모델에게 되돌리는 loop shape
- 첫 QueryForge 응답에서 Application Session ID를 얻고 이후 Tool argument에 전달하는 개념
- schema-first, 명시적 `partition_scope`, `error.details.candidates` 기반 교정 prompt

### 2.2 production에서 폐기할 구현

- `requests`로 직접 구현한 MCP JSON-RPC initialize/notification/tool call
- global `requests.Session`과 script-local mutable session state
- transport session header를 직접 관리하는 코드
- print 기반 logging과 응답 내용 출력
- 호출별 timeout만 있고 전체 deadline이 없는 loop
- 단일 `MAX_STEPS` 자유 반복 및 한 호출에서 여러 tool call을 별도 budget 없이 실행하는 구조
- JSON Schema client-side 선검증 없이 arguments를 QueryForge에 전달하는 경로
- production component lifecycle, cancellation, redaction이 없는 script entry point

### 2.3 production 보완점

- Ollama wire format은 `LLMProvider` 안에서 provider-neutral `AssistantTurn`으로 변환한다.
- 공식 MCP SDK만 `QueryForgeClient`에서 사용한다.
- Orchestrator가 allowlist, local JSON Schema validation, Tool-call 3회, recovery 1회, 전체 120초 deadline을
  provider와 무관하게 강제한다.
- DataLens Session과 QueryForge Application Session을 1:1 mapping하고 MCP Transport Session과 분리한다.
- malformed output, cancellation, unknown completion outcome, log redaction을 명시적으로 처리한다.

## 3. QuerySpec 정적 검증 결과

QueryForge `queryforge/schemas/query_spec.schema.json`을 Draft 2020-12 validator로 직접 검사했다.

| 케이스 | 기대 | 결과 |
|---|---:|---:|
| 유효한 time-range QuerySpec | accept | PASS |
| `partition_scope` 누락 | reject | PASS |
| unknown `raw_sql` field | reject | PASS |
| invalid partition kind enum | reject | PASS |
| string 타입 `limit` | reject | PASS |
| 유효한 `rel_<12 hex>` relationship reference | accept | PASS |
| 잘못된 relationship ID 형식 | reject | PASS |
| 유효한 `not_partitioned` scope | accept | PASS |
| malformed JSON | parser reject | PASS |

이는 `native tool calling → local JSON parse/schema validation → bounded recovery → QueryForge validation`의
결정적 validation 부분이 구현 가능함을 증명한다. 모델이 valid argument를 얼마나 자주 생성하는지는 실제
Ollama가 없어 측정하지 못했다.

## 4. Constrained decoding 판정

현재 reference는 native tool calling만 사용하고 `/api/chat`의 schema-constrained `format`과 tools를
결합하지 않는다. 설치된 Ollama version/model behavior가 없어 두 기능의 동시 강제를 production capability로
판정할 수 없다.

Phase 0 안전성은 Ollama constrained decoding에 의존하지 않는다. 최소 필수 경로는 다음이다.

```text
native tool call
→ JSON object/string normalization
→ 현재 QueryForge inputSchema의 local validation
→ valid: QueryForgeClient
→ invalid: recovery budget 1회
→ QueryForge server validation (항상 유지)
```

Constrained decoding이 실제 환경에서 안정적으로 가능하면 invalid generation을 줄이는 최적화로 사용하되,
local validation과 QueryForge validation을 제거하지 않는다.

## 5. Recovery Model

| 입력/오류 | 내부 분류 | Recovery | 처리 |
|---|---|---:|---|
| malformed JSON, wrong type, required 누락, invalid enum | `RECOVERABLE_BY_AGENT` | 1회 | validator path/message와 schema 일부만 모델에 반환 |
| unknown Tool | `INTERNAL_ERROR` | 0회 | allowlist 단계에서 차단; QueryForge에 보내지 않음 |
| `MISSING_PARTITION_SCOPE` | `RECOVERABLE_BY_AGENT` | 1회 | schema 결과/active period로 안전하게 보완 가능할 때만 |
| `UNKNOWN_COLUMN` | `RECOVERABLE_BY_AGENT` | 1회 | QF candidate/hint가 있을 때만 교정 |
| `UNKNOWN_TABLE` | `RECOVERABLE_BY_AGENT` 또는 `USER_CLARIFICATION_REQUIRED` | 최대 1회 | 단일 명확 후보면 교정, 아니면 사용자 확인 |
| ambiguous identifier/복수 후보 | `USER_CLARIFICATION_REQUIRED` | 0회 | 모델 추측 실행 금지 |
| 기타 policy/validation reject | `QUERYFORGE_REJECTED` | 0회 | 안전한 설명으로 종료 |
| `QUERY_TIMEOUT`, 결과 불명확 MCP 오류 | `UPSTREAM_TIMEOUT` | 0회 | 자동 재실행 금지 |
| Ollama timeout/cancel | `UPSTREAM_TIMEOUT` | 0회 | 마지막 완료 state 유지 |
| parser/adapter invariant 위반 | `INTERNAL_ERROR` | 0회 | correlation ID와 내부 로그 |

요청서의 `MISSING_TIME_RANGE`는 현재 QueryForge ErrorCode에는 없으며 production code는
`MISSING_PARTITION_SCOPE`를 사용한다. DataLens internal/public mapping에서 과거 명칭을 현재 코드로
혼동하지 않아야 한다.

## 6. Tool-call budget 분석

| Workflow | 최소 Tool call | Phase 0 기본 3회 |
|---|---:|---|
| `schema → query` | 2 | 충분 |
| `schema → relationship → query` | 3 | 한도 내, recovery 여유 없음 |
| `query → transform → describe` | 3 | 한도 내, recovery 여유 없음 |
| `schema → relationship → query → describe` | 4 | 한도 초과 |

Phase 0 acceptance는 다음 방식이면 기본값 3으로 가능하다.

- Turn 1: `schema(list_tables)`로 table과 `partitioned` 정보를 함께 확보한다.
- Turn 2: Session state의 선택 table을 재사용해 `query`한다. 불필요한 schema 재탐색을 금지한다.
- Turn 3: preview/metadata로 판단하고 필요한 경우 `describe` 한 번만 사용한다.
- 관계 탐색과 describe가 모두 필요한 복합 요청은 Phase 0 acceptance에서 제외하거나 config를 명시적으로
  높인 별도 scenario로 시험한다.

Recovery budget 1은 Tool-call 전체 3회 안에 포함한다. 따라서 3-call 정상 workflow에서 validation 실패가
나면 회복할 자리가 없다는 trade-off를 metric으로 기록해야 한다. 기본값 3은 유지할 수 있지만 architecture
invariant로 승격할 수 없다.

## 7. Multi-turn과 latency

실제 한국어 3턴은 Ollama와 QueryForge가 모두 실행되지 않아 `BLOCKED_BY_ENVIRONMENT`다. 다음 상태가
continuity의 최소 입력이다.

- 선택한 table 및 partition metadata
- active period
- active Dataset ID와 preview/summary
- QueryForge Application Session mapping
- compact TurnSummary

provider resume/chat session은 continuity의 진실 원천이 아니다. 후속 Turn에서 위 명시적 state를 넣고 schema를
불필요하게 다시 호출하지 않는지 측정해야 한다.

Latency도 측정하지 못했으므로 120초 적합성을 주장할 수 없다. 실제 재시험에서는 각 Turn별 LLM latency,
각 QF call latency, total latency, Tool/recovery count를 기록하고 최소 여러 회의 median과 observed maximum을
보고한다. 표본이 충분하기 전에는 P95라는 명칭을 사용하지 않는다.

## 8. Contract 차이

현재 QueryForge production schema의 `preview_rows.description`에는 `mcp.max_response_bytes(32KB)`가 남아
있다. 그러나 최신 QueryForge ADR, `config/queryforge.yaml`, DataLens consistency gate가 가리키는 현재
기본값은 10,485,760 bytes(10MB)다. 실행 validation에는 영향이 없는 description stale defect이며 이번
작업에서는 QueryForge production contract를 수정하지 않았다. QueryForge repository의 별도 문서/contract
정합성 수정이 필요하다.

## 9. Gate Decision

| Gate | 판정 | 근거 |
|---|---|---|
| GATE-1 Ollama native tool calling 기반 사용 | `BLOCKED_BY_ENVIRONMENT` | Ollama executable/API/model 없음 |
| GATE-2 QuerySpec argument 안정성 | `BLOCKED_BY_ENVIRONMENT` | schema validation 경로는 PASS, 모델 생성 품질은 미측정 |
| GATE-3 constrained decoding 필요성/실현성 | `BLOCKED_BY_ENVIRONMENT` | 유용한 최적화이나 설치 version/model에서 동시 사용 미검증 |
| GATE-4 native + local validation + bounded recovery | `PASS_WITH_CONDITION` | local validation 9/9 PASS; 실제 recovery 성공률 측정 필요 |
| GATE-5 3 calls / 1 recovery / 120초 | `PASS_WITH_CONDITION` | 제한된 acceptance workflow는 1~3 calls; latency와 recovery headroom 미측정 |
| GATE-6 I1→I2→I3 진행 | `PASS_WITH_CONDITION` | provider와 독립된 skeleton/session/MCP client는 진행 가능; I4/I5 전 Ollama 재시험 필수 |

Architecture 변경은 필요하지 않으므로 `ADR_CHANGE_REQUIRED`는 아니다.

## 10. 권장 LLMProvider 최소 계약

```text
complete(
  messages: list[ProviderNeutralMessage],
  tools: list[ToolDefinition],
  deadline: Deadline,
  output_policy: OutputPolicy
) -> AssistantTurn

AssistantTurn
├── content: string | null
├── tool_calls: list[ToolCall{name, arguments}]
├── stop_reason: normalized enum
├── usage: TokenUsage | null
└── provider_request_id: opaque | null
```

Provider 책임:

- `/api/chat` wire format, model name, timeout/cancel 처리
- string/object arguments normalization과 malformed transport response 판별
- provider stop reason/usage 정규화
- raw response 및 credential 비노출

Provider 책임이 아닌 것:

- Tool allowlist와 QuerySpec schema validation
- Tool/recovery budget과 120초 전체 deadline 소유
- QueryForge 호출/session mapping
- DataLens Session continuity와 public error mapping

이 책임은 각각 Orchestrator, QueryForgeClient, Application Service에 남긴다.

