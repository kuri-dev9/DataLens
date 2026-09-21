# DataLens API 구현 스펙 (코딩 에이전트용)

이 문서는 DataLens 프론트엔드를 구현하기 위한 명세다. 추측하지 말고 이 문서에 적힌
사실만 사용한다. 여기에 없는 엔드포인트나 파라미터를 호출하지 않는다.

| 항목 | 값 |
|---|---|
| Base URL | `<BASE_URL>` (별도 전달) |
| 인증 | 모든 요청에 `x-api-key` 헤더 (`/v1/health` 제외) |
| Content-Type | `application/json` |
| 문자셋 | UTF-8 |
| 검증 기준 | 2026-09-14 실서버 실측 |

---

## 0. 절대 규칙

1. **`EventSource`를 사용하지 않는다.** 커스텀 헤더(`x-api-key`)를 지정할 수 없다.
   SSE는 `fetch` + `ReadableStream`으로 직접 파싱한다.
2. **채팅 응답 본문에서 표 데이터를 파싱하지 않는다.** `answer`는 사람이 읽는 문장이고
   `preview`는 5행뿐이다. 전체 행은 `/datasets/{id}/rows`에서 가져온다.
3. **모든 세션 종속 리소스는 `/v1/sessions/{session_id}/` 아래에 있다.**
   `session_id`를 쿼리 파라미터로 보내지 않는다.
4. **HTTP 타임아웃을 300초 이상으로 설정한다.** 채팅 응답은 실측 7~43초이며 더 걸릴 수 있다.
5. **`locale`은 `ko` 또는 `ja`만 허용한다.** `kr`, `jp`, `ko-KR` 등은 400을 받는다.
6. **API 키를 클라이언트 번들에 포함하지 않는다.** 백엔드 프록시를 경유한다.

---

## 1. 엔드포인트 목록

| Method | Path | LLM 경유 | 실측 응답시간 |
|---|---|---|---|
| GET | `/v1/health` | — | 즉시 (인증 불필요) |
| GET | `/v1/ready` | — | 즉시 |
| POST | `/v1/sessions` | — | 즉시 |
| DELETE | `/v1/sessions/{session_id}` | — | 즉시 |
| POST | `/v1/sessions/{session_id}/messages` | **예** | **7~43초** |
| GET | `/v1/sessions/{session_id}/catalog/tables` | 아니오 | 0.058초 |
| GET | `/v1/sessions/{session_id}/catalog/tables/{table}/columns` | 아니오 | 즉시 |
| GET | `/v1/sessions/{session_id}/datasets/{dataset_id}/rows` | 아니오 | 0.025초 |
| GET | `/v1/sessions/{session_id}/datasets/{dataset_id}/meta` | 아니오 | 즉시 |

SSE를 지원하는 경로: `messages`, `catalog/tables`, `datasets/{id}/rows`
SSE를 지원하지 않는 경로: `catalog/tables/{table}/columns` (JSON만)

---

## 2. 세션

### 2.1 생성

```http
POST /v1/sessions
x-api-key: {key}
content-type: application/json

{"locale": "ko"}
```

`locale`은 선택. 생략 시 서버 기본값(`ko`).

**응답 201** (실측)

```json
{
  "session_id": "dls_VSd3ZJGpdrbga3RZ1UZFRa2O",
  "created_at": "2026-09-14T07:33:59.599203Z",
  "expires_at": "2026-09-14T08:03:59.599203Z",
  "locale": "ko"
}
```

- `session_id`는 `dls_` 접두사 + 22자
- 유효기간 30분. `expires_at` 기준으로 갱신 타이밍을 판단한다
- **상태 코드가 200이 아니라 201이다.** `status === 200` 검사를 하지 않는다

**오류**: `locale`이 허용값 밖이면 400 `DL_INVALID_LOCALE`, 응답에 허용 목록 포함

### 2.2 삭제

```http
DELETE /v1/sessions/{session_id}
x-api-key: {key}
```

### 2.3 세션 관리 규칙

- 앱 시작 시 세션을 하나 만들고 `session_id`를 보관한다
- 카탈로그 조회도 세션이 필요하다. 채팅을 하지 않아도 세션은 먼저 만든다
- `DL_SESSION_NOT_FOUND`(404)를 받으면 새 세션을 만들고 **원래 요청을 한 번 재시도**한다
- 세션당 동시 요청은 1건이다. 진행 중이면 `DL_SESSION_BUSY`
- `locale` 변경은 불가능하다. 새 세션을 만든다

---

## 3. 채팅 (LLM 경유)

### 3.1 JSON 모드

```http
POST /v1/sessions/{session_id}/messages
x-api-key: {key}
content-type: application/json

{"message": "CD_CAUSE_ORIGINAL 테이블 데이터 보여줘"}
```

**응답 200** (실측)

```json
{
  "request_id": "dlr_oxkrDbPwjwmZE5sHrPpPAmat",
  "session_id": "dls_VSd3ZJGpdrbga3RZ1UZFRa2O",
  "status": "completed",
  "answer": "CD_CAUSE_ORIGINAL 테이블에는 323건이 격납되어 있습니다...",
  "datasets": [
    {
      "dataset_id": "ds_000000002",
      "role": "primary",
      "row_count": 323,
      "columns": [
        {"name": "CAUSE_CODE", "type": "String", "nullable": false},
        {"name": "CAUSE_TYPE_CODE", "type": "String", "nullable": false},
        {"name": "CAUSE_TYPE_NAME", "type": "String", "nullable": false},
        {"name": "DESCRIPTION", "type": "String", "nullable": false}
      ],
      "preview": [
        {"__index": 0, "CAUSE_CODE": "C_02000201", "CAUSE_TYPE_CODE": "02",
         "CAUSE_TYPE_NAME": "S1AP", "DESCRIPTION": "UNSPECIFIED"}
      ]
    }
  ],
  "warnings": [],
  "metadata": {
    "duration_ms": 25654,
    "tool_calls": 2,
    "recovery_count": 0,
    "stop_reason": "completed"
  },
  "error": null
}
```

**실패 시**

```json
{
  "request_id": "dlr_...",
  "session_id": "dls_...",
  "status": "failed",
  "error": {
    "code": "DL_UPSTREAM_TIMEOUT",
    "message": "Request processing timed out",
    "retryable": true,
    "details": null
  }
}
```

### 3.2 필드 규칙

| 필드 | 규칙 |
|---|---|
| `status` | `completed` \| `failed`. `error`가 null인지로도 판별 가능 |
| `answer` | 마크다운. 렌더링해서 표시한다 |
| `datasets` | 배열. **비어 있을 수 있다** (단순 대화, 조회 실패 시) |
| `datasets[].preview` | **최대 5행 고정.** 전체 행은 §5 경로로 가져온다 |
| `datasets[].role` | `primary` 등. 여러 개면 `primary`를 기본 표시한다 |
| `preview[].__index` | 행 번호(0부터). 표시하지 않는다 |
| `columns[].type` | Polars 타입명 (`String`, `Int64`, `Float64`, `Date`, `Datetime` 등) |
| `warnings[]` | 배열. 있으면 사용자에게 알린다. `code` 필드로 분기 |
| `metadata.tool_calls` | 0이면 DB 조회 없이 대화만 한 것 |

### 3.3 SSE 모드

`Accept: text/event-stream` 헤더를 추가한다. URL과 body는 동일하다.

**이벤트 순서**: `start` → (`tool_call` 0회 이상) → (`token` 다수) → (`dataset` 0회 이상) → `done`
오류 시 어느 지점에서든 `error` 후 종료.

```
event: start
data: {"request_id":"dlr_...","session_id":"dls_..."}

event: tool_call
data: {"index":1,"tool":"schema","status":"started"}

event: tool_call
data: {"index":1,"tool":"schema","status":"completed","elapsed_ms":1240}

event: token
data: {"text":"안녕하세요"}

event: token
data: {"text":","}

event: dataset
data: {"dataset_id":"ds_000000003","row_count":323,"columns":[...]}

event: done
data: {"status":"completed","metadata":{"duration_ms":25654,"tool_calls":2}}
```

**처리 규칙**

- `token.text`를 **줄바꿈 없이 이어붙인다.** 조각 단위이며 단어 중간에서 끊길 수 있다
- `: ping` 으로 시작하는 줄은 주석이다. 무시한다 (15초 간격 keep-alive)
- `tool_call`은 진행 표시에 사용한다. `status`가 `started`/`completed`로 두 번 온다
- `dataset` 이벤트를 받으면 **즉시** §5 rows를 호출해 표를 채운다. `done`을 기다리지 않는다
- `error` 이벤트의 data는 §3.1 실패 응답의 `error` 객체와 동일한 구조다
- thinking 블록은 서버가 제거한다. 클라이언트에서 필터링할 필요 없다

**중요 — 첫 토큰까지 약 19초 걸린다.** `start` 직후부터 첫 `token`까지 공백이 길다.
이 구간에 로딩 UI를 반드시 표시한다.

---

## 4. 카탈로그 (LLM 비경유)

### 4.1 테이블 목록

```http
GET /v1/sessions/{session_id}/catalog/tables?offset=0&limit=1000&pattern=pm_
x-api-key: {key}
```

| 파라미터 | 기본 | 설명 |
|---|---|---|
| `offset` | 0 | |
| `limit` | 100 | 최대 1000 |
| `pattern` | 없음 | 테이블명 부분 일치, 대소문자 무관 |

**응답 200** (실측, 526 테이블 환경)

```json
{
  "offset": 0,
  "limit": 1000,
  "total_count": 526,
  "returned_count": 526,
  "tables": [
    {"name": "PM_ENB_KPI_1M", "comment": null,
     "row_count_estimate": 781487, "partitioned": true},
    {"name": "XDR_LTE_CALL_KPI", "comment": "ONGOING_FLAG='2' AND MME_ID='16'",
     "row_count_estimate": 2843872, "partitioned": true}
  ],
  "truncated": false,
  "warnings": []
}
```

- `row_count_estimate`는 **추정치**다. 정확한 값이 아니라고 표시한다 ("약 781,487행")
- `comment`는 null일 수 있다
- `partitioned: true`는 시계열 파티션 테이블. 대용량인 경우가 많다
- `truncated`가 true면 `total_count` > `returned_count`. 페이징 안내를 표시한다

### 4.2 컬럼 목록

```http
GET /v1/sessions/{session_id}/catalog/tables/{table}/columns
x-api-key: {key}
```

```json
{
  "table": "orders",
  "total_count": 2,
  "returned_count": 2,
  "columns": [{"name": "id", "type": "int64"}, {"name": "amount", "type": "decimal"}],
  "truncated": false,
  "warnings": []
}
```

- **필터 파라미터가 없다.** 전체가 온다. 테이블에 따라 167개까지 있으므로
  **클라이언트에서 검색 UI를 제공한다**
- SSE를 지원하지 않는다. JSON만 사용한다

### 4.3 SSE 모드 (tables만)

```
event: meta
data: {"offset":0,"limit":1000,"total_count":526}

event: tables
data: {"offset":0,"tables":[ ...100개... ]}

event: tables
data: {"offset":100,"tables":[ ...100개... ]}

event: done
data: {...}
```

100개 단위 청크. 전체 조회가 0.058초이므로 **SSE를 쓸 실익이 거의 없다. JSON을 권장한다.**

---

## 5. Dataset (LLM 비경유)

### 5.1 행 조회

```http
GET /v1/sessions/{session_id}/datasets/{dataset_id}/rows?offset=0&limit=1000
x-api-key: {key}
```

| 파라미터 | 기본 | 최대 |
|---|---|---|
| `offset` | 0 | — |
| `limit` | 100 | **1000** |

1000 초과 지정 시 서버가 1000으로 제한한다.

```json
{
  "dataset_id": "ds_000000001",
  "offset": 0,
  "limit": 1000,
  "rows": [
    {"CAUSE_CODE": "C_02000201", "CAUSE_TYPE_CODE": "02",
     "CAUSE_TYPE_NAME": "S1AP", "DESCRIPTION": "UNSPECIFIED"}
  ],
  "warnings": []
}
```

- 전체 행수는 이 응답에 없다. §3의 `datasets[].row_count` 또는 §5.2 `meta`를 사용한다
- 1000행을 넘으면 `offset`을 올려 반복 호출한다

### 5.2 메타

```http
GET /v1/sessions/{session_id}/datasets/{dataset_id}/meta
```

```json
{
  "dataset_id": "ds_000000001",
  "row_count": 323,
  "columns": [{"name": "value", "type": "int64"}],
  "parent_dataset_id": null
}
```

### 5.3 SSE 모드

```
event: meta
data: {"dataset_id":"ds_...","row_count":323,"columns":[...]}

event: rows
data: {"offset":0,"rows":[ ...100행... ]}

event: done
data: {"returned":323,"total":323}
```

100행 단위. 대량 데이터를 점진 렌더링할 때 유용하다.

### 5.4 수명

- Dataset TTL 30분, 세션당 최대 20개
- 초과 시 오래된 것부터 제거된다
- 만료된 `dataset_id` 접근은 404 `DL_DATASET_NOT_FOUND`
- 세션이 만료되면 그 세션의 Dataset도 모두 접근 불가

---

## 6. 오류

### 6.1 공통 형식

```json
{
  "request_id": "dlr_...",
  "status": "failed",
  "error": {
    "code": "DL_UPSTREAM_TIMEOUT",
    "message": "Request processing timed out",
    "retryable": true,
    "details": null
  }
}
```

`retryable: true`면 동일 요청 재시도가 가능하다.

### 6.2 코드 전체

| HTTP | 코드 | 의미 | 클라이언트 처리 |
|---|---|---|---|
| 400 | `DL_INVALID_REQUEST` | 요청 형식 오류 | 수정 필요. 재시도 무의미 |
| 400 | `DL_INVALID_LOCALE` | locale 값 오류 | `ko`/`ja`만 허용 |
| 400 | `DL_INVALID_PAGINATION` | offset/limit 오류 | 파라미터 수정 |
| 401 | `DL_UNAUTHORIZED` | API Key 누락/불일치 | 설정 확인 |
| 404 | `DL_SESSION_NOT_FOUND` | 세션 없음/만료 | **새 세션 생성 후 1회 재시도** |
| 404 | `DL_DATASET_NOT_FOUND` | Dataset 없음/만료/타 세션 | "만료됨" 안내 |
| 404 | `DL_CATALOG_NOT_FOUND` | 테이블 없음/미허용 | 테이블명 확인 안내 |
| 409 | `DL_SESSION_BUSY` | 해당 세션 처리 중 | 입력 잠금, 완료 대기 |
| 500 | `DL_INTERNAL_ERROR` | 서버 내부 오류 | 재시도 버튼 |
| 502 | `DL_CATALOG_UNAVAILABLE` | QueryForge 카탈로그 오류 | 재시도 |
| 503 | `DL_UPSTREAM_UNAVAILABLE` | QueryForge 연결 불가 | 재시도. `retryable: true` |
| 504 | `DL_UPSTREAM_TIMEOUT` | 처리 시간 초과 | 재시도. `retryable: true` |
| — | `DL_AGENT_LIMIT` | Agent 스텝 한도 초과 | 질문을 더 구체적으로 안내 |
| — | `DL_AGENT_INVALID_TOOL` | LLM이 잘못된 도구 호출 | 재시도 |
| — | `DL_AGENT_NOT_READY` | Agent 미준비 | 재시도 |
| — | `DL_QUERY_REJECTED` | QueryForge가 요청을 거부함 | `details.upstream_code`에 따라 안내. `retryable` 반영 |
| — | `DL_UPSTREAM_INVALID_RESPONSE` | 업스트림 응답 형식 오류 | 재시도 |
| — | `DL_SESSION_CLEANUP_FAILED` | 세션 정리 실패 | 무시 가능 |

### 6.3 필수 처리 로직

```
1. DL_SESSION_NOT_FOUND → 새 세션 생성 → 원 요청 1회 재시도 → 또 실패하면 사용자에게 노출
2. retryable === true  → 재시도 버튼 제공
3. DL_SESSION_BUSY     → 입력창 비활성화, 진행 중 요청 완료까지 대기
4. 그 외               → error.message를 그대로 표시하지 말고 코드별 안내 문구로 변환
```

`DL_QUERY_REJECTED`는 QueryForge의 안전한 교정 정보를 다음 구조로 보존한다.

```json
{
  "code": "DL_QUERY_REJECTED",
  "message": "The data request was rejected",
  "retryable": true,
  "details": {
    "upstream_code": "MISSING_PARTITION_SCOPE",
    "hint": "<QueryForge 교정 힌트>",
    "table": "example_table"
  }
}
```

- `MISSING_PARTITION_SCOPE`, `INVALID_PARTITION_SCOPE`: "조회 범위 지정에 문제가 있습니다"
- `TABLE_NOT_ALLOWED` 등 정책 오류: "허용되지 않은 데이터 요청입니다"
- 그 외 또는 `upstream_code` 없음: "데이터 요청을 처리하지 못했습니다"
- `details`에는 교정용 구조 필드만 포함되며 SQL 원문, 접속 문자열, 비밀번호, 행 데이터는 포함하지 않는다.

---

## 7. SSE 파서 참조 구현

```js
async function streamMessage(sessionId, message, handlers) {
  const res = await fetch(`${BASE}/v1/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "accept": "text/event-stream",
      "x-api-key": API_KEY,
    },
    body: JSON.stringify({ message }),
  });

  if (!res.ok && res.headers.get("content-type")?.includes("json")) {
    handlers.onError(await res.json());
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // 이벤트는 빈 줄로 구분된다
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      let eventName = "message";
      let dataLines = [];

      for (const line of chunk.split("\n")) {
        if (line.startsWith(":")) continue;           // ping 주석 무시
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;

      let data;
      try { data = JSON.parse(dataLines.join("\n")); } catch { continue; }

      switch (eventName) {
        case "start":     handlers.onStart(data); break;
        case "tool_call": handlers.onToolCall(data); break;
        case "token":     handlers.onToken(data.text); break;   // 이어붙인다
        case "dataset":   handlers.onDataset(data); break;      // 즉시 rows 호출
        case "done":      handlers.onDone(data); break;
        case "error":     handlers.onError(data); break;
      }
    }
  }
}
```

**주의점**

- 이벤트 경계는 **빈 줄(`\n\n`)** 이다. 줄 단위로만 자르면 안 된다
- `decoder.decode(value, {stream: true})` — `stream: true`를 빼면 멀티바이트 문자가 깨진다
- 마지막 불완전 청크는 `buffer`에 남겨 다음 read와 합친다
- 연결을 끊으면 서버가 LLM 요청도 취소한다. `AbortController`로 취소 버튼을 구현할 수 있다

---

## 8. 권장 구현 흐름

```
앱 시작
 └ POST /v1/sessions {locale}          → session_id 보관
 └ GET  .../catalog/tables?limit=1000  → 사이드바 테이블 목록 (0.06초)

사용자 질문
 └ POST .../messages (Accept: text/event-stream)
    ├ start      → 로딩 표시 시작
    ├ tool_call  → "테이블 조회 중..." 진행 표시 갱신
    ├ token      → 채팅 영역에 이어붙이며 표시
    ├ dataset    → 즉시 GET .../datasets/{id}/rows 호출 → 표 렌더링
    └ done       → 로딩 종료, metadata 기록

표에서 1000행 초과
 └ GET .../datasets/{id}/rows?offset=1000&limit=1000 반복
```

---

## 9. 하지 말 것

- `EventSource` 사용
- `answer` 텍스트에서 표 데이터 추출
- `session_id`를 쿼리 파라미터로 전송 (구 버전 방식, 현재 404)
- HTTP 타임아웃 30초 이하 설정
- `locale`에 `kr` / `jp` / `ko-KR` 전송
- 세션 생성 응답을 `status === 200`으로 검사 (실제 201)
- `preview`를 전체 데이터로 취급
- 이 문서에 없는 파라미터 추측 호출
- API 키를 클라이언트 번들에 포함

---

## 10. 참고 — 서버 측 제약 근거

| 제약 | 값 | 이유 |
|---|---|---|
| preview 5행 | 고정 | LLM 컨텍스트 예산(8K 토큰) 보호 |
| rows 최대 1000 | 고정 | 단일 응답 크기 제한 |
| 세션 TTL 30분 | 설정값 | in-memory 저장, 서버 재시작 시 소멸 |
| 채팅 7~43초 | 실측 | 로컬 GPU(A2 15GB) 추론 속도 35 tok/s |
| SSE TTFT 19초 | 실측 | 프롬프트 처리(약 1,400토큰 @ 500 tok/s) + 추론 시작 |

이 값들은 하드웨어 교체나 설정 변경으로 달라질 수 있다. 클라이언트는 상한을 하드코딩하지
말고 응답의 `total_count` / `row_count` / `truncated`를 기준으로 동작한다.
