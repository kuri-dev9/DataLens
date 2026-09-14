# DataLens HTTP API

모든 엔드포인트는 `/v1/health`를 제외하고 `x-api-key` 헤더가 필요하다. Dataset 및
세션 종속 리소스는 모두 `/v1/sessions/{session_id}/` 아래에 있다. 세션 ID를 쿼리
파라미터로 받지 않는다. Dataset과 카탈로그 경로는 Ollama나 다른 LLM을 호출하지 않는다.

## Dataset 행 조회

```http
GET /v1/sessions/dls_example_session_12345/datasets/ds_000000001/rows?offset=0&limit=1000
x-api-key: <DataLens API key>
```

`offset` 기본값은 0, `limit` 기본값은 100이며 최대값은 1000이다. 1000을 초과한
`limit`은 1000으로 제한한 뒤 QueryForge에 전달한다. 성공 응답은 QueryForge payload를
가공하거나 요약하지 않고 그대로 반환한다.

```json
{
  "dataset_id": "ds_000000001",
  "offset": 0,
  "limit": 1000,
  "rows": [{"event_time": "2026-09-14T00:00:00", "value": 7}],
  "warnings": []
}
```

## Dataset 메타데이터

```http
GET /v1/sessions/dls_example_session_12345/datasets/ds_000000001/meta
x-api-key: <DataLens API key>
```

```json
{
  "dataset_id": "ds_000000001",
  "row_count": 323,
  "columns": [{"name": "value", "type": "int64"}],
  "parent_dataset_id": null
}
```

유효하지 않은 세션, 타 세션 소유 Dataset, 존재하지 않는 Dataset은 모두 동일한 404
`DL_DATASET_NOT_FOUND`로 반환하여 Dataset 존재 여부를 노출하지 않는다.

## 카탈로그 테이블 조회

```http
GET /v1/sessions/dls_example_session_12345/catalog/tables?offset=0&limit=1000&pattern=report
x-api-key: <DataLens API key>
```

`pattern`은 대소문자를 구분하지 않는 부분 일치 필터다. DataLens는 QueryForge의 `schema`
Tool을 MCP로 직접 호출하며 QueryForge가 적용한 allowlist 결과를 추가로 변경하지 않는다.
아직 QueryForge 컨텍스트가 없는 새 DataLens 세션이면 이 호출에서 컨텍스트를 만들고 이후
Dataset 소유권 검사에 사용할 수 있도록 세션에 연결한다.

```json
{
  "offset": 0,
  "limit": 1000,
  "total_count": 2,
  "returned_count": 2,
  "tables": [{"name": "daily_report"}, {"name": "monthly_report"}],
  "truncated": false,
  "warnings": []
}
```

## 카탈로그 컬럼 조회

```http
GET /v1/sessions/dls_example_session_12345/catalog/tables/orders/columns
x-api-key: <DataLens API key>
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

## 오류 매핑

| HTTP | DataLens 코드 | 의미 |
|---|---|---|
| 400 | `DL_INVALID_PAGINATION` | offset 또는 limit이 유효하지 않음 |
| 401 | `DL_UNAUTHORIZED` | DataLens API Key 누락 또는 불일치 |
| 404 | `DL_DATASET_NOT_FOUND` | 세션/Dataset이 없거나 소유권이 다름 |
| 404 | `DL_CATALOG_NOT_FOUND` | 테이블이 없거나 접근이 허용되지 않음 |
| 504 | `DL_UPSTREAM_TIMEOUT` | QueryForge 요청 시간 초과 |
| 503 | `DL_UPSTREAM_UNAVAILABLE` | QueryForge 연결 불가 |
| 502 | `DL_CATALOG_UNAVAILABLE` | QueryForge 카탈로그 오류 |

## SSE 스트리밍

메시지, Dataset rows, 카탈로그 tables 경로는 같은 URL에서 `Accept` 헤더로 응답 방식을
협상한다. `Accept: text/event-stream`이면 SSE, `application/json` 또는 헤더 미지정이면
기존 JSON 응답이다. SSE 응답은 `Cache-Control: no-cache`와
`X-Accel-Buffering: no`를 포함하고, 장시간 이벤트가 없으면 15초마다 `: ping` 주석을 보낸다.

메시지 스트림 이벤트는 `start`, `tool_call`, `token`, `dataset`, `done` 순서로 발생할 수
있다. 오류는 기존 `DL_*` 본문을 담은 `error` 이벤트 후 종료된다. thinking 블록은 제거되어
`token` 이벤트에 포함되지 않는다.

```text
event: start
data: {"request_id":"dlr_...","session_id":"dls_..."}

event: token
data: {"text":"안녕하세요"}

event: done
data: {"status":"completed","metadata":{"duration_ms":1200,"tool_calls":0}}
```

rows 스트림은 `meta` → 100행 단위 `rows` → `done`, 카탈로그 스트림은 `meta` →
100개 단위 `tables` → `done` 이벤트를 사용한다. `limit`이 있으면 그 범위까지만 보낸다.

브라우저 `EventSource`는 `x-api-key` 같은 커스텀 헤더를 지정할 수 없으므로 사용할 수 없다.
UI에서는 `fetch`와 `ReadableStream`을 사용해야 한다.

```js
const response = await fetch(`/v1/sessions/${sessionId}/messages`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    "accept": "text/event-stream",
    "x-api-key": apiKey,
  },
  body: JSON.stringify({message: "테이블을 보여줘"}),
});

const reader = response.body.getReader();
const decoder = new TextDecoder();
while (true) {
  const {value, done} = await reader.read();
  if (done) break;
  consumeSseText(decoder.decode(value, {stream: true}));
}
```
