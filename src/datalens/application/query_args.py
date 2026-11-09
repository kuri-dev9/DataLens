"""QueryForge `query` 인자 — 작은 모델이 반복해서 틀리는 모양을 고치고, 못 고치면 바른 모양을 알린다 (2026-11-08).

## 왜 생겼나

graphio 일반 질의에서 gemma4:12b 가 `query` 인자를 같은 자리에서 거듭 틀렸다. 에스터 재현
(18번 사건 "기지국별 영향 통계")에서 도구를 7번 부르다 `DL_AGENT_LIMIT` 으로 멈췄고 134초가
걸렸다. 틀린 모양은 매번 같았다 —

  filters          조건 목록을 `all` 로 감싸지 않음 (목록 그대로 · 조건 하나를 그대로)
  in / not_in      `values` 배열 대신 `value` 에 목록을 담음
  group_by         열 이름 문자열 대신 `{alias, column}` 객체
  partition_scope  `column` 이 빠짐

검증 오류만 돌려주면 anyOf 스키마라 "is not valid under any of the given schemas" 수준이고
기대 스키마가 400자를 넘어 생략된다 — 모델은 무엇을 고칠지 모른 채 다시 추측한다.

## 무엇을 하나

1. **고쳐도 뜻이 같은 모양만 서버에서 고친다**(`normalize`). 위 넷 중 앞의 셋이다.
   * 조건 목록 · 조건 하나 → `{"all": [...]}` — AND 로 묶는 것 외의 해석이 없다
   * `in`/`not_in` 의 `value` → `values` (스칼라면 한 원소 목록)
   * `group_by` 의 `{column}` 또는 `{column, alias}`(bucket 없음) → `"column"` — 시간 버킷이 아닌
     객체는 열 하나를 뜻할 수밖에 없다. alias 는 버린다: 문자열 group_by 에는 별칭 자리가 없다
2. **뜻을 추측해야 하는 것은 고치지 않는다.** `partition_scope.column` 은 파티션 키를 알아야
   채울 수 있고, 시간 창을 넓히거나 좁히는 것도 모델의 결정이다. 대신 검증 오류에 그 자리의
   바른 모양을 붙인다(`shape_hint`).

QueryForge 가 실행 단계에서 거절한 경우(`INVALID_QUERY_SPEC` · `details.path`)에도 같은 문장을 붙인다 —
`select`·`aggregations` 가 둘 다 없는 호출은 스키마로는 통과하고 QueryForge 에서만 걸린다.

고친 내역은 호출 로그와 화면 이벤트에 남긴다 — 서버가 인자를 바꾼 사실이 감춰지면 안 된다.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# 검증 오류가 난 최상위 인자별 바른 모양. 도구 오류 응답에만 실린다(시스템 프롬프트가 아니다).
_SHAPES: dict[str, str] = {
    "filters": (
        'filters 는 {"all":[조건,...]} 이다. 조건: {"column":"C","operator":"eq","value":"x"} · '
        '{"column":"C","operator":"in","values":["a","b"]} (in/not_in 은 values 배열) · '
        '{"column":"T","operator":"between","value_from":"…","value_to":"…"}. 문자열 열에는 문자열 값을 준다'
    ),
    "group_by": (
        'group_by 는 열 이름 문자열 목록이다: ["C1","C2"]. 시간 버킷만 객체다: '
        '{"column":"T","bucket":"minute|hour|day|month","alias":"t"}'
    ),
    "partition_scope": (
        'partition_scope 는 {"kind":"time_range","column":"<partition.keys 의 시간 열>","from":"YYYY-MM-DD HH:MM:SS",'
        '"to":"YYYY-MM-DD HH:MM:SS","inclusive_end":false} 이다. column 은 schema describe_table 의 partition.keys 에서 고른다'
    ),
    "aggregations": (
        'aggregations 는 [{"function":"count","alias":"n"},{"function":"sum","column":"C","alias":"s"}] 모양이다. '
        "alias 는 반드시 준다"
    ),
    "order_by": 'order_by 는 [{"column":"열 또는 집계 alias","direction":"desc"}] 이다',
    "select": (
        'select 는 [{"column":"C"}] 모양이다. select 와 aggregations 중 하나는 반드시 준다(둘 다 없으면 거절된다). '
        "집계와 함께면 select 의 열은 group_by 에도 있어야 한다"
    ),
}


def shape_hint(error_paths: list[str]) -> str | None:
    """검증 오류가 난 최상위 인자들의 바른 모양. 해당 없으면 None."""
    keys: list[str] = []
    for path in error_paths:
        head = path.split("/", 1)[0]
        if head in _SHAPES and head not in keys:
            keys.append(head)
    if not keys:
        return None
    return " || ".join(_SHAPES[key] for key in keys)


def normalize(arguments: Any, partition_keys: dict[str, list[str]] | None = None) -> tuple[Any, list[str]]:
    """뜻이 바뀌지 않는 모양 고침만 한다. (고친 인자, 고친 내역) — 고칠 것이 없으면 내역이 빈다.

    ``partition_keys`` — 이번 턴에 `schema` 로 본 표의 파티션 키(표 → 키 목록). 있으면 아래 두 가지도 고친다.
    """
    if not isinstance(arguments, dict):
        return arguments, []
    fixed = deepcopy(arguments)
    notes: list[str] = []

    # time_range 에 column 이 빠진 경우(2026-11-10 · graphio 16번) — 표의 파티션 키가 **하나뿐**이면 그 키다.
    # 여럿이거나 이번 턴에 표를 보지 않았으면 고치지 않는다(지금처럼 거부되어 모델이 고친다)
    scope = fixed.get("partition_scope")
    source = fixed.get("source")
    table = source.get("table") if isinstance(source, dict) else None
    keys = (partition_keys or {}).get(str(table)) if table else None
    if (isinstance(scope, dict) and scope.get("kind") == "time_range" and not scope.get("column")
            and isinstance(keys, list) and len(keys) == 1):
        scope["column"] = keys[0]
        notes.append(f"partition_scope/column: 빠짐 → 표 {table} 의 파티션 키 {keys[0]}")

    filters = fixed.get("filters")
    if isinstance(filters, list):
        fixed["filters"] = {"all": filters}
        notes.append("filters: 목록 → {all: 목록}")
    elif isinstance(filters, dict) and "operator" in filters and "column" in filters:
        fixed["filters"] = {"all": [filters]}
        notes.append("filters: 조건 하나 → {all: [조건]}")
    group = fixed.get("filters")
    if isinstance(group, dict):
        conditions = group.get("all")
        if isinstance(conditions, list):
            _fix_conditions(conditions, "filters/all", notes)
        any_groups = group.get("any_groups")
        if isinstance(any_groups, list):
            for index, inner in enumerate(any_groups):
                if isinstance(inner, list):
                    _fix_conditions(inner, f"filters/any_groups/{index}", notes)

    for index, item in enumerate(fixed.get("aggregations") or ()):
        if isinstance(item, dict) and isinstance(item.get("where"), dict):
            _fix_in(item["where"], f"aggregations/{index}/where", notes)

    group_by = fixed.get("group_by")
    if isinstance(group_by, str):
        fixed["group_by"] = [group_by]
        notes.append("group_by: 문자열 → [문자열]")
    elif isinstance(group_by, list):
        for index, item in enumerate(group_by):
            if (
                isinstance(item, dict)
                and isinstance(item.get("column"), str)
                and "bucket" not in item
                and set(item) <= {"column", "alias"}
            ):
                group_by[index] = item["column"]
                notes.append(f"group_by/{index}: {{column}} 객체 → 열 이름 문자열")
    # 집계와 함께 보낸 select 열 중 **eq 거름으로 값이 하나로 고정된 열**은 뺀다(2026-11-10 · graphio 16번).
    # QueryForge 는 group_by 에 없는 select 열을 거부한다(INVALID_QUERY_SPEC). 값이 하나인 열은 결과 행을
    # 바꾸지 않으니 빼도 뜻이 같다. 고정되지 않은 열은 group_by 로 옮기면 합계가 분해로 바뀌어 뜻이 달라지므로 둔다
    if fixed.get("aggregations") and isinstance(fixed.get("select"), list):
        grouped = {g for g in fixed.get("group_by") or () if isinstance(g, str)}
        pinned = {c.get("column") for c in ((fixed.get("filters") or {}).get("all") or ())
                  if isinstance(c, dict) and c.get("operator") == "eq"} if isinstance(fixed.get("filters"), dict) else set()
        # group_by 가 없는 합계에서 **집계 대상 열 자체**를 select 에 둔 것도 뺀다(2026-11-10 · graphio 16번
        # `sum(ATTEMPT)` + `select ATTEMPT` · 맥스 재현 10회 중 5회). 결과가 한 행 합계라 같은 열의 raw 값을 함께
        # 고를 방법이 없고 QueryForge 가 거부한다. group_by 가 있으면 뜻이 모호해 두고 거부·hint 에 맡긴다
        agg_cols = set() if grouped else {a.get("column") for a in fixed["aggregations"] if isinstance(a, dict)}
        keep = []
        for item in fixed["select"]:
            col = item.get("column") if isinstance(item, dict) else None
            if col and col not in grouped and col in pinned:
                notes.append(f"select: {col} 는 eq 거름으로 고정된 열이라 집계와 함께 빼냄")
                continue
            if col and col in agg_cols:
                notes.append(f"select: {col} 는 집계 대상 열이라 group_by 없는 합계에서 빼냄")
                continue
            keep.append(item)
        if len(keep) != len(fixed["select"]):
            if keep:
                fixed["select"] = keep
            else:
                fixed.pop("select")
    return fixed, notes


def _fix_conditions(conditions: list[Any], path: str, notes: list[str]) -> None:
    for index, condition in enumerate(conditions):
        if isinstance(condition, dict):
            _fix_in(condition, f"{path}/{index}", notes)


def _fix_in(condition: dict[str, Any], path: str, notes: list[str]) -> None:
    if condition.get("operator") not in {"in", "not_in"} or "values" in condition or "value" not in condition:
        return
    value = condition.pop("value")
    condition["values"] = value if isinstance(value, list) else [value]
    notes.append(f"{path}: in 의 value → values")
