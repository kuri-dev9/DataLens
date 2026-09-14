# DataLens HTTP API

모든 엔드포인트는 `/v1/health`를 제외하고 `x-api-key` 헤더가 필요하다. Dataset 및
카탈로그 직접 조회는 DataLens 세션 소유권을 확인하기 위해 `session_id` 쿼리 파라미터를
필수로 받는다. 이 경로들은 Ollama나 다른 LLM을 호출하지 않는다.

## Dataset 행 조회

```http
GET /v1/datasets/ds_000000001/rows?session_id=dls_example_session_12345&offset=0&limit=1000
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
GET /v1/datasets/ds_000000001/meta?session_id=dls_example_session_12345
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
GET /v1/catalog/tables?session_id=dls_example_session_12345&offset=0&limit=1000&pattern=report
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
GET /v1/catalog/tables/orders/columns?session_id=dls_example_session_12345
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
