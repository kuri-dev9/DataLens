from __future__ import annotations

import json
import logging
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator
from jsonschema.exceptions import relevance

from datalens.domain.session import Session
from datalens.prompts import load_system_prompt
from datalens.application.runtime_config import HISTORY_STORE_LIMIT, Mode, Profile, RuntimeConfig
from datalens.ports.llm import (
    LLMEmptyResponse,
    LLMInvalidResponse,
    LLMProvider,
    LLMTimeout,
    LLMUnavailable,
    OutputPolicy,
    ProviderMessage,
    TokenUsage,
)
from datalens.ports.queryforge import (
    QueryForgeClient,
    QueryForgeFailure,
    QueryForgeResult,
    QueryForgeToolDefinition,
    QueryForgeUnavailable,
)
from datalens.application import query_args
from datalens.application.grounding import collect_facts, safe_measure
from datalens.observability import current_request_id, safe_log_value

#: INFO 로그의 조회 조건 요약 상한(글자)
QUERY_ARGS_LOG_MAX_CHARS = 500
_QUERY_ARGS_SECRET = frozenset({"password", "secret", "api_key", "authorization", "dsn",
                                "connection_string", "confirmation_token", "sql"})


def _query_args_summary(arguments: dict[str, Any]) -> str:
    """query 도구 인자에서 원인 추적에 필요한 칸만 — 테이블·거름(값 포함)·시간 범위·집계·묶음·선택 열.

    `safe_log_value` 는 거름 값(`value`·`from`·`to`)까지 가린다. 그 값이 바로 "어떤 조건이 갔나"라서
    여기서는 비밀 키만 가린다. 행·미리보기는 인자에 없다. 길면 `QUERY_ARGS_LOG_MAX_CHARS` 에서 자른다
    """
    def scrub(v: Any, key: str | None = None) -> Any:
        if key in _QUERY_ARGS_SECRET:
            return "***"
        if isinstance(v, dict):
            return {str(k): scrub(x, str(k).lower()) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [scrub(x) for x in v]
        return v

    source = arguments.get("source")
    picked = {
        "table": source.get("table") if isinstance(source, dict) else source,
        "filters": arguments.get("filters"),
        "partition_scope": arguments.get("partition_scope"),
        "aggregations": [
            {k: a.get(k) for k in ("function", "column", "alias", "where") if a.get(k) is not None}
            for a in arguments.get("aggregations") or () if isinstance(a, dict)
        ] or None,
        "group_by": arguments.get("group_by"),
        "select": [x.get("column") if isinstance(x, dict) else x for x in arguments.get("select") or ()] or None,
    }
    text = json.dumps(scrub({k: v for k, v in picked.items() if v is not None}),
                      ensure_ascii=False, default=str, separators=(",", ":"))
    return text if len(text) <= QUERY_ARGS_LOG_MAX_CHARS else text[:QUERY_ARGS_LOG_MAX_CHARS] + "…"


LOG = logging.getLogger("datalens.agent")
# 애플리케이션 계층이 가로채는 내부 이벤트. SSE로 내보내지 않는다.
QUERYFORGE_SESSION_EVENT = "_queryforge_session"
# 세션 컨텍스트로 노출하는 데이터셋 계보 상한. 컨텍스트는 매 턴 모델 입력에 들어가므로
# 무한정 키우지 않고, 오래된 항목부터 떨어뜨린다.
_LINEAGE_LIMIT = 8
# 도구 결과 중 데이터가 아닌 안내용 필드. 모델에는 주고 근거 집합에서는 뺀다.
# notice의 응답 크기 추정치("~120 KB")나 next_page의 페이징 인자(count/offset)를
# 근거로 인정하면 ADR-034 계측이 느슨해진다.
_NON_FACT_KEYS = frozenset({"notice", "next_page"})
# 도구 정의 lazy 모드의 메타 도구(2026-11-04). QueryForge 도구가 아니다 — DataLens 가 직접 답한다.
TOOL_SPEC_NAME = "get_tool_spec"
# 빈 출력이 재시도 뒤에도 이어질 때 도구 없이 한 번 더 부르며 붙이는 지시(2026-11-08).
# 사용자 차례로 넣는다 — 대화 중간의 system 은 gemma 템플릿에서 첫 system 과 합쳐지지 않는다.
# 실행하지 않은 조회를 "실행했지만 얻지 못했다"고 쓰지 않게 한 문장을 더했다(2026-11-09 · graphio 5번 F-222 ·
# query 호출 0회인데 "クエリを実行しましたが…取得できなかった"로 답했다)
_FINAL_WITHOUT_TOOLS = {
    "ko": "[DataLens 안내] 도구를 더 부를 수 없습니다. 지금까지의 도구 결과와 위 맥락만으로 질문에 답하세요. "
          "조회하지 못한 것은 조회하지 못했다고 말하고, 받지 않은 수치는 쓰지 마세요. "
          "실행하지 못한 조회를 실행했다고 쓰지 마세요 — 결과를 받은 조회만 실행한 것입니다.",
    "ja": "[DataLens 案内] これ以上ツールは呼べません。これまでのツール結果と上の文脈だけで質問に答えてください。"
          "照会できなかったものは照会できなかったと述べ、受け取っていない数値は書かないでください。"
          "実行できなかった照会を実行したと書かないでください — 結果を受け取った照会だけが実行したものです。",
}
# 첫 빈 응답 뒤 다시 부를 때 붙이는 안내(2026-11-09). 전에는 **같은 입력**으로 다시 불러 같은 출력이 나왔다
# (graphio 5번 · 토큰 149·145 · 청크 1 · 원문 0자 — Ollama 가 읽지 못한 도구 호출). 입력을 바꿔 다른 생성을 받는다
_EMPTY_RETRY_NUDGE = {
    "ko": "[DataLens 안내] 직전 출력을 읽지 못했습니다(본문도 도구 호출도 없음). 도구를 부르려던 것이면 도구를 다시 "
          "호출하세요 — 인자는 사용자 메시지의 조회 안내대로, 한 번에 하나의 도구만 부릅니다. 도구가 필요 없으면 답을 쓰세요.",
    "ja": "[DataLens 案内] 直前の出力を読み取れませんでした(本文もツール呼び出しもありません)。ツールを呼ぶつもりだったなら、"
          "もう一度ツールを呼んでください — 引数はユーザーメッセージの照会案内どおりに、一度に一つのツールだけを呼びます。"
          "ツールが不要なら回答を書いてください。",
}
# 도구를 한 번도 부르지 않은 답에 근거 없는 수치가 있을 때 한 번 더 부르며 붙이는 지시(2026-11-09 · graphio
# 5·18번). 이어진 대화에서 모델이 앞 턴(도구 없이 답한 결론·상황 답)의 모양을 따라 도구 호출 0회로
# 2,358/1,460·1,800/1,632 를 지어냈다(21~28초). 같은 질문을 새 대화로 보내면 도구 3회로 맞았다.
_UNBACKED_RETRY = {
    "ko": "[DataLens 안내] 방금 답에 도구로 조회하지 않은 수치({v})가 있습니다. 데이터 수치는 도구로 조회한 결과만 "
          "씁니다. 질문이 데이터를 묻는다면 지금 query 도구를 호출해 조회한 뒤 답하세요. 사용자가 보낸 맥락에 있는 "
          "수치만으로 답할 수 있는 질문이면 그 수치만 쓰고, 조회하지 못한 값은 0 이 아니라 '확인하지 못했다'고 쓰세요.",
    "ja": "[DataLens 案内] 直前の回答に、ツールで照会していない数値({v})があります。データの数値はツールで照会した結果だけを"
          "書きます。質問がデータを尋ねているなら、今 query ツールを呼んで照会してから答えてください。ユーザーが送った文脈に"
          "ある数値だけで答えられる質問ならその数値だけを使い、照会できなかった値は 0 ではなく「確認できなかった」と書いてください。",
}


# query 를 한 번도 부르지 않고 "확인하지 못했다"로 끝낸 답에 한 번 더 부르며 붙이는 지시(2026-11-10 · graphio
# 25번). 셀 질문에서 schema 만 한 번 보고 "条件に合致するデータを確認できませんでした"로 답했다(원시 39건).
# 같은 틀의 다른 셀은 query 3회로 조회했다 — 시도 자체가 빠진 경우만 되묻는다(두 번째 답은 그대로 낸다)
_NO_QUERY_RETRY = {
    "ko": "[DataLens 안내] 방금 답은 query 도구를 한 번도 부르지 않고 '확인하지 못했다'고 했습니다. 질문이 데이터를 "
          "묻는다면 사용자 메시지의 조회 안내대로 지금 query 도구를 호출해 조회한 뒤 답하세요. 조회해도 행이 없으면 "
          "그때 '확인하지 못했다'고 쓰세요.",
    "ja": "[DataLens 案内] 直前の回答は query ツールを一度も呼ばずに「確認できなかった」としています。質問がデータを"
          "尋ねているなら、ユーザーメッセージの照会案内どおりに今 query ツールを呼んで照会してから答えてください。照会しても"
          "行がなければ、そのとき「確認できなかった」と書いてください。",
}
#: "조회하지 못했다" 계열 표현 — 조회 없이 포기한 답을 가린다(초기값)
_GAVE_UP_PHRASES = (
    "확인하지 못", "확인할 수 없", "조회하지 못", "조회할 수 없", "확인되지 않",
    "確認できません", "確認できなかった", "確認できませんでした", "確認することができません",
    "照会できません", "照会することができません", "照会できなかった", "取得できません",
    "取得することができません", "取得できなかった",
    "取得できていない", "取得できていません", "照会結果が取得",
)


def _gave_up_without_query(answer: str) -> bool:
    return any(p in answer for p in _GAVE_UP_PHRASES)


def _unbacked_numbers(answer: str, facts: set[str], user_texts: list[str]) -> list[str]:
    """도구 없이 쓴 답의 수치 중 **어디에도 근거가 없는 것** — 이전 답·세션 상태(`facts`)와 사용자가 보낸 글
    (맥락·질문)에 없는 수치. 날짜·작은 정수(목록 번호 등)는 세지 않는다. 계측이 실패하면 빈 목록이다."""
    known = set(facts)
    collect_facts(user_texts, known)
    g = safe_measure(answer, known) or {}
    out: list[str] = []
    for u in g.get("ungrounded") or ():
        if u.get("date") or u.get("small_int"):
            continue
        v = str(u.get("value", "")).strip()
        if v and v not in out:
            out.append(v)
    return out


#: 조회 모드 이력에 싣는 **사용자 메시지 한 건의 상한(글자)** — 초기값(2026-11-09 · graphio 18번 F-218).
#: 증거 모드로 시작한 세션을 조회 모드로 이어 물으면 첫 증거팩(15,360자)이 이력에 그대로 실려 컨텍스트가
#: 16,384 토큰 중 15,683(`tight`)이 됐고, 모델이 도구를 부르지 않은 채 "照会結果は以下の通り"만 썼다. 다음
#: 공지문 질의는 18,340 토큰(`truncated`)으로 앞이 잘려 근거 없는 약속("全力で対応")이 다시 나왔다.
#: 호출부(graphio)는 이어 묻는 메시지마다 화면 맥락을 다시 싣는다 — 이력의 긴 원문은 중복이다.
#: 넘는 사용자 메시지는 **끝부분**(질문이 있는 자리)만 남긴다. 증거 모드 이력은 자르지 않는다(이어 묻기가
#: 앞 증거팩을 근거로 쓴다 · `ask_check`)
HISTORY_USER_MAX_CHARS = 1200
HISTORY_USER_TAIL_CHARS = 600
_HISTORY_CUT = {
    "ko": "[앞 질문 — 본문 {n}자 중 끝 {k}자만 싣는다. 증거·화면 맥락은 지금 메시지에 다시 있다]\n…",
    "ja": "[前の質問 — 本文 {n}字のうち末尾 {k}字だけを載せる。証拠・画面の文脈は今のメッセージに改めてある]\n…",
}


def _history_content(role: str, content: str, mode: str, locale: str) -> str:
    """이력 항목 하나를 싣는 모양 — 조회 모드의 긴 사용자 메시지만 끝부분으로 줄인다(`HISTORY_USER_MAX_CHARS`)."""
    if mode == "evidence" or role != "user" or len(content) <= HISTORY_USER_MAX_CHARS:
        return content
    head = _HISTORY_CUT.get(locale, _HISTORY_CUT["ko"]).format(n=len(content), k=HISTORY_USER_TAIL_CHARS)
    return head + content[-HISTORY_USER_TAIL_CHARS:]


_LAZY_HINT = {
    "ko": "[도구 정의] 도구 목록의 정의는 요약본이다(이름·한 줄 설명·최상위 인자). 인자 구조가 확실하지 않으면 "
          f"그 도구를 부르기 전에 {TOOL_SPEC_NAME}(name)으로 전체 정의를 받아라. 한 번 받은 정의는 이후 도구 목록에 그대로 실린다.",
    "ja": "[ツール定義] ツール一覧の定義は要約版です(名前・一行説明・最上位の引数)。引数の構造が確かでなければ、"
          f"そのツールを呼ぶ前に {TOOL_SPEC_NAME}(name) で完全な定義を受け取ってください。一度受け取った定義は以後のツール一覧にそのまま載ります。",
}


class AgentError(RuntimeError):
    def __init__(self, message: str, *, tool_calls: int = 0, recovery_count: int = 0) -> None:
        super().__init__(message)
        self.tool_calls = tool_calls
        self.recovery_count = recovery_count
        # 실패해도 그때까지 확보한 결과는 버리지 않는다.
        self.datasets: tuple[DatasetReference, ...] = ()
        self.warnings: tuple[Any, ...] = ()
        self.failed_step: dict[str, Any] | None = None


class AgentLimitError(AgentError):
    pass


class AgentTimeoutError(AgentError):
    pass


class AgentUpstreamUnavailable(AgentError):
    pass


class AgentInvalidUpstreamResponse(AgentError):
    pass


class AgentPolicyError(AgentError):
    pass


class AgentQueryRejected(AgentError):
    def __init__(self, failure: QueryForgeFailure | str | None = None, *, tool_calls: int = 0, recovery_count: int = 0) -> None:
        normalized = failure if isinstance(failure, QueryForgeFailure) else None
        super().__init__(normalized.code if normalized else str(failure or "QUERYFORGE_REJECTED"), tool_calls=tool_calls, recovery_count=recovery_count)
        self.failure = normalized


class AgentInternalError(AgentError):
    pass


@dataclass(frozen=True, slots=True)
class DatasetReference:
    dataset_id: str
    role: str = "primary"
    row_count: int | None = None
    columns: tuple[dict[str, Any], ...] = ()
    preview: tuple[dict[str, Any], ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "role": self.role,
            "row_count": self.row_count,
            "columns": list(self.columns),
            "preview": list(self.preview),
        }


@dataclass(frozen=True, slots=True)
class AgentResult:
    answer: str
    datasets: tuple[DatasetReference, ...]
    warnings: tuple[Any, ...]
    tool_calls: int
    recovery_count: int
    updated_session: Session
    # 성공한 도구 호출만 담는다. 다음 번 같은 질문에서 탐색을 건너뛰기 위한 재료다.
    plan: tuple[dict[str, Any], ...] = ()
    # 답변 수치가 도구 결과에 있었는지 재기만 한 결과. 답변은 바꾸지 않는다 (ADR-034).
    grounding: dict[str, Any] | None = None
    # 이번 턴에서 프롬프트가 컨텍스트 창을 얼마나 먹었는지. 절단은 에러가 아니라
    # 침묵으로 나타나므로, 잘렸는지를 사후에 알 수 있는 유일한 통로다.
    context: dict[str, Any] | None = None
    # 이번 턴에 무엇을 실었나(모드·맥락 구성·파라미터·도구 수). 호출부가 "무엇을 보냈는지"를
    # 사후에 확인하는 통로다 — 설정 화면 값은 바뀔 수 있으므로 턴마다 남긴다.
    profile: dict[str, Any] | None = None
    # 마지막 LLM 호출에 실제로 실린 메시지·도구·응답(application/trace.py 가 블록으로 나눈다).
    # 메모리에만 보관되는 진단용 값이고 API 응답 본문에는 싣지 않는다.
    trace: dict[str, Any] | None = None


class BoundedAgent:
    # 모델이 인자를 고쳐도 결과가 달라지지 않는 실패. 그 외는 도구 결과로 되돌려 자기수정시킨다.
    _SUMMARY_KEYS = {
        "schema": ("action", "table", "name_pattern", "limit"),
        "relationship": ("from_table", "to_table", "max_depth"),
        "describe": ("dataset_id", "level"),
        "transform": ("dataset_id",),
    }
    _TERMINAL_CODES = frozenset(
        {
            "UNAUTHORIZED",
            "INVALID_SESSION",
            "TABLE_NOT_ALLOWED",
            "CAPABILITY_NOT_ENABLED",
            "NOT_IMPLEMENTED",
            "DB_CONNECTION_ERROR",
            "CATALOG_NOT_INITIALIZED",
            "UPSTREAM_UNSTRUCTURED_ERROR",
        }
    )

    def __init__(
        self,
        provider: LLMProvider,
        queryforge: QueryForgeClient,
        *,
        max_tool_calls: int,
        recovery_budget: int,
        preview_rows: int = 5,
        timezone: str = "Asia/Seoul",
        runtime: RuntimeConfig | None = None,
    ) -> None:
        self._provider = provider
        self._queryforge = queryforge
        self._max_tool_calls = max_tool_calls
        self._recovery_budget = recovery_budget
        self._preview_rows = preview_rows
        self._timezone = timezone
        # None 이면 모드 설정이 없는 예전 동작(agent 기본값 · 기동 설정 파라미터)이다.
        self._runtime = runtime

    async def run(
        self,
        session: Session,
        message: str,
        deadline: float,
        event_sink: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        hints: str | None = None,
        mode: Mode = "agent",
    ) -> AgentResult:
        self._remaining(deadline)
        profile = self.profile(mode)
        if profile.tools == "off":
            # 증거 전용 모드는 QueryForge 를 부르지도 않는다 — QueryForge 가 내려가 있어도 답한다.
            tools: tuple[QueryForgeToolDefinition, ...] = ()
        else:
            try:
                tools = tuple(await self._queryforge.discover_tools(self._remaining(deadline)))
            except (TimeoutError, QueryForgeUnavailable) as exc:
                raise AgentUpstreamUnavailable("QueryForge capability discovery failed") from exc
        tool_map = {tool.name: tool for tool in tools}
        # lazy 모드에서 전체 정의를 받은 도구. 세션에 남아 다음 턴에도 펼쳐진 채로 간다.
        expanded: list[str] = [name for name in session.expanded_tools if name in tool_map]
        sent_tools = self._sent_tools(tools, profile.tools, expanded)
        spec_calls = 0
        messages = self._context(session, message, hints if profile.memory else None, profile)
        # 여기까지가 조립된 입력이다. 뒤에 붙는 것은 도구 호출 루프가 만든 메시지다.
        base_len = len(messages)
        policy = self._output_policy(profile)
        sent = {
            **profile.public(),
            "tool_count": len(sent_tools),
            "tool_chars": sum(len(tool.description) + len(json.dumps(tool.input_schema, ensure_ascii=False)) for tool in sent_tools),
            "history_used": sum(1 for item in messages if item.role in {"user", "assistant"}) - 1,
        }
        plan: list[dict[str, Any]] = []
        tool_count = 0
        recovery_count = 0
        qf_session_id = session.queryforge_session_id
        datasets: dict[str, DatasetReference] = {}
        # 이번 턴에 만들어진 데이터셋의 계보 항목. 세션에 병합되어 다음 턴 컨텍스트가 된다.
        lineage: dict[str, dict[str, Any]] = {}
        warnings: list[Any] = []
        # 모델이 실제로 받은 수치만 모은다. 여기 없는 수치는 모델이 지어낸 것이다.
        facts: set[str] = set()
        # 세션 상태(now/today/active_period)와 회상 힌트, 이전 턴 답변까지가 모델이 본 사실이다.
        # 시스템 프롬프트 본문은 제외한다 — 예시 수치까지 근거로 인정하면 계측이 무의미해진다.
        collect_facts(
            [
                item.content
                for item in messages
                if item.role == "assistant"
                or (item.role == "system" and item.content.startswith("DataLens "))
                # 증거 전용 모드에서는 사용자가 보낸 자료가 곧 사실이다. 이전 턴의 자료도 같다.
                or (profile.mode == "evidence" and item.role == "user")
            ],
            facts,
        )
        active_table = session.active_table
        active_period = deepcopy(session.active_period)
        step: dict[str, Any] = {}
        # 한 턴은 LLM을 여러 번 부른다. 도구 결과가 쌓일수록 프롬프트가 커지므로
        # 가장 압박이 심했던 호출을 들고 간다 — 평균을 내면 절단이 묻힌다.
        worst_usage: TokenUsage | None = None
        # 본문도 도구 호출도 없는 생성은 한 턴에 한 번만 다시 보낸다(LLMEmptyResponse · 2026-11-08).
        empty_retried = False
        # 재시도도 비면 도구 없이 마지막 답을 한 번 더 받는다 — 빈손으로 502 를 내지 않는다.
        final_without_tools = False
        # 빈 응답마다 프로바이더가 남긴 진단(`LLMEmptyResponse.diagnostics`) — trace 에만 싣는다(2026-11-09)
        empty_outputs: list[dict[str, Any]] = []
        # 근거 없는 수치 되묻기(`_UNBACKED_RETRY`)는 한 턴에 한 번 — 두 번째 답은 그대로 내보낸다
        unbacked_retry: dict[str, Any] | None = None
        # 조회 없이 "확인하지 못했다"로 끝난 답 되묻기(`_NO_QUERY_RETRY`) — 한 턴에 한 번
        no_query_retry: dict[str, Any] | None = None
        query_calls = 0
        partition_keys: dict[str, list[str]] = {}

        try:
            while True:
                # deadline은 신규 작업 마감이다: 여기(호출 사이)서만 검사하고,
                # 진행 중인 생성은 프로바이더의 유휴 워치독만 끊을 수 있다(ADR-033).
                self._remaining(deadline)
                # LLM 호출 도중 끊기면 어느 단계에서 멈췄는지 failed_step으로 남는다.
                step.clear()
                step.update({"index": tool_count + 1, "tool": "llm"})
                sent_tools = () if final_without_tools else self._sent_tools(tools, profile.tools, expanded)
                try:
                    if event_sink is not None and hasattr(self._provider, "complete_stream"):
                        turn = await self._provider.complete_stream(
                            messages,
                            sent_tools,
                            deadline,
                            policy,
                            lambda text: event_sink("token", {"text": text}),
                            lambda data: event_sink("progress", data),
                        )
                    else:
                        turn = await self._provider.complete(messages, sent_tools, deadline, policy)
                except LLMTimeout as exc:
                    step["upstream_code"] = "LLM_IDLE_TIMEOUT"
                    step["detail"] = str(exc)
                    raise AgentTimeoutError("LLM deadline exceeded") from exc
                except LLMUnavailable as exc:
                    step["upstream_code"] = "LLM_UNAVAILABLE"
                    step["detail"] = str(exc)
                    raise AgentUpstreamUnavailable("LLM provider failed") from exc
                except LLMEmptyResponse as exc:
                    empty_outputs.append({"step": tool_count + 1, **(getattr(exc, "diagnostics", None) or {})})
                    # gemma4 의 도구 호출을 Ollama 가 읽지 못하면 아무것도 없이 끝난다. 흘린 토큰이
                    # 없으니 안내 한 줄(`_EMPTY_RETRY_NUDGE`)을 붙여 한 번 더 부른다. 두 번째도 비면 도구를 빼고 지금까지 받은 것으로
                    # 답하게 한 번 더 부른다. 그것도 비면 예전처럼 실패로 끝낸다.
                    if final_without_tools:
                        step["upstream_code"] = "LLM_INVALID_RESPONSE"
                        step["detail"] = str(exc)
                        raise AgentInvalidUpstreamResponse("LLM provider returned an invalid response") from exc
                    if empty_retried:
                        final_without_tools = True
                        messages.append(
                            ProviderMessage("user", _FINAL_WITHOUT_TOOLS.get(session.locale, _FINAL_WITHOUT_TOOLS["ko"]))
                        )
                        warnings.append(
                            {
                                "code": "DATALENS_FINAL_WITHOUT_TOOLS",
                                "action": "answered_without_tools",
                                "detail": "the model returned empty output twice; answered from tool results gathered so far",
                            }
                        )
                        LOG.warning(
                            "llm_final_without_tools",
                            extra={"request_id": current_request_id(), "session_id": session.session_id, "step": tool_count + 1, "tool_calls": tool_count},
                        )
                        continue
                    empty_retried = True
                    # 같은 입력이면 같은 출력이 나온다 — 안내 한 줄로 입력을 바꾼다. trace 의 `empty_outputs` 에
                    # `retry: "nudged"` 로 남고, 안내 문장 자체도 보낸 메시지(`agent_step`)에 보인다
                    empty_outputs[-1]["retry"] = "nudged"
                    messages.append(ProviderMessage("user", _EMPTY_RETRY_NUDGE.get(session.locale, _EMPTY_RETRY_NUDGE["ko"])))
                    LOG.warning(
                        "llm_empty_retry",
                        extra={"request_id": current_request_id(), "session_id": session.session_id, "step": tool_count + 1,
                               "nudged": True},
                    )
                    continue
                except LLMInvalidResponse as exc:
                    step["upstream_code"] = "LLM_INVALID_RESPONSE"
                    step["detail"] = str(exc)
                    raise AgentInvalidUpstreamResponse("LLM provider returned an invalid response") from exc
                step.clear()
                worst_usage = self._worst_usage(worst_usage, turn.usage)

                if final_without_tools and turn.tool_calls:
                    # 도구 목록을 비웠는데도 호출을 냈다 — 본문이 있으면 그것을 답으로 쓰고 호출은 버린다.
                    if not (turn.content or "").strip():
                        step["upstream_code"] = "LLM_INVALID_RESPONSE"
                        step["detail"] = "tool call after tools were withdrawn"
                        raise AgentInvalidUpstreamResponse("LLM provider returned an invalid response")
                    turn = replace(turn, tool_calls=())
                if not turn.tool_calls:
                    answer = (turn.content or "").strip()
                    if not answer:
                        raise AgentInternalError("LLM returned an empty final answer")
                    if (profile.mode != "evidence" and tool_count == 0 and unbacked_retry is None
                            and not final_without_tools and sent_tools):
                        unbacked = _unbacked_numbers(
                            answer, facts, [m.content for m in messages if m.role == "user"])
                        if unbacked:
                            unbacked_retry = {"step": tool_count + 1, "values": unbacked[:10],
                                              "first_answer_head": answer[:300]}
                            warnings.append({
                                "code": "DATALENS_UNBACKED_NUMBERS_RETRY",
                                "action": "regenerated_once",
                                "detail": "numbers not found in tool results or the provided context were answered "
                                          "without any tool call; asked the model once to query",
                                "values": unbacked[:10],
                            })
                            LOG.warning("agent_unbacked_numbers_retry",
                                        extra={"request_id": current_request_id(), "session_id": session.session_id,
                                               "values": unbacked[:10]})
                            # 첫 답은 대화에 넣지 않는다 — 지어낸 수치가 다음 생성의 근거처럼 보이지 않게
                            notice = _UNBACKED_RETRY.get(session.locale, _UNBACKED_RETRY["ko"])
                            messages.append(ProviderMessage("user", notice.format(v=", ".join(unbacked[:5]))))
                            continue
                    # `final_without_tools`(빈 출력 두 번 → 도구를 빼고 답하게 한 경로)도 대상이다(2026-11-10 · graphio
                    # 16번). 처음엔 도구가 빠진 요청에 "query 를 불러라"가 무의미해 뺐는데, 바로 그 경로가 "조회 없이
                    # 포기"였다. 그때는 도구를 되돌려 한 번 더 부른다. 되묻기는 한 턴에 한 번이고, 다시 빈 출력이면
                    # 빈 출력 처리(`empty_retried` 이미 참)가 곧바로 도구 없는 답으로 보내므로 끝없이 돌지 않는다
                    if (profile.mode != "evidence" and query_calls == 0 and no_query_retry is None
                            and "query" in tool_map and (sent_tools or final_without_tools)
                            and _gave_up_without_query(answer)):
                        no_query_retry = {"step": tool_count + 1, "first_answer_head": answer[:300],
                                          **({"tools_restored": True} if final_without_tools else {})}
                        final_without_tools = False
                        warnings.append({
                            "code": "DATALENS_NO_QUERY_RETRY",
                            "action": "regenerated_once",
                            "detail": "answered 'could not confirm' without any query call; asked the model once to query",
                        })
                        LOG.warning("agent_no_query_retry",
                                    extra={"request_id": current_request_id(), "session_id": session.session_id,
                                           "tool_calls": tool_count})
                        messages.append(ProviderMessage("user", _NO_QUERY_RETRY.get(session.locale, _NO_QUERY_RETRY["ko"])))
                        continue
                    updated = self._updated_session(
                        session,
                        message,
                        answer,
                        qf_session_id,
                        tuple(lineage.values()),
                        active_table,
                        active_period,
                        tuple(expanded),
                    )
                    return AgentResult(
                        answer=answer,
                        grounding=safe_measure(answer, facts),
                        datasets=tuple(datasets.values()),
                        warnings=tuple(warnings),
                        tool_calls=tool_count,
                        recovery_count=recovery_count,
                        updated_session=updated,
                        plan=tuple(plan),
                        context=worst_usage.public() if worst_usage else None,
                        profile=sent,
                        trace={
                            "messages": list(messages),
                            "tools": sent_tools,
                            "base_len": base_len,
                            "usage": turn.usage,
                            "stop_reason": turn.stop_reason,
                            **({"empty_outputs": empty_outputs} if empty_outputs else {}),
                            **({"unbacked_retry": unbacked_retry} if unbacked_retry else {}),
                            **({"no_query_retry": no_query_retry} if no_query_retry else {}),
                        },
                    )

                messages.append(ProviderMessage("assistant", turn.content or "", turn.tool_calls))
                should_retry = False
                for call in turn.tool_calls:
                    if profile.tools == "lazy" and call.name == TOOL_SPEC_NAME:
                        # QueryForge 호출이 아니므로 도구 예산을 쓰지 않는다. 대신 도구 수만큼만 허용해 루프를 막는다.
                        spec_calls += 1
                        if spec_calls > len(tool_map) + 2:
                            raise AgentLimitError("Tool spec request budget exhausted")
                        messages.append(self._tool_spec_message(call.arguments, tool_map, expanded))
                        should_retry = True
                        continue
                    if tool_count >= self._max_tool_calls:
                        raise AgentLimitError("Tool call budget exhausted")
                    tool_count += 1
                    step.clear()
                    step.update({"index": tool_count, "tool": call.name})
                    arguments = call.arguments
                    fixes: list[str] = []
                    if call.name == "query":
                        # 뜻이 같은 모양 고침만 한다(query_args 머리말). 고친 인자가 검증·호출·계보에 쓰인다.
                        arguments, fixes = query_args.normalize(arguments, partition_keys)
                        if fixes:
                            step["normalized"] = list(fixes)
                            LOG.info(
                                "tool_arguments_normalized",
                                extra={"request_id": current_request_id(), "session_id": session.session_id, "step": tool_count, "tool": call.name, "fixes": fixes},
                            )
                    # 무엇을 하려는 단계인지 먼저 알린다. 인자 검증에서 걸러지는 호출도
                    # 사용자 화면에서는 하나의 시도로 보여야 한다.
                    intent = self._call_summary(call.name, arguments)
                    if intent:
                        step["intent"] = deepcopy(intent)
                    if event_sink is not None:
                        await event_sink(
                            "tool_call",
                            {"index": tool_count, "tool": call.name, "status": "started", "intent": intent},
                        )
                    definition = tool_map.get(call.name)
                    if definition is None:
                        raise AgentPolicyError("LLM requested a tool outside the allowlist")
                    validation_error = self._validate(definition, arguments)
                    if validation_error is not None:
                        self._log_tool_call(session, tool_count, call.name, arguments, False, 0, recovery_count < self._recovery_budget, code="INVALID_TOOL_ARGUMENTS", details={"reason": "schema_validation_failed"})
                        step["upstream_code"] = "INVALID_TOOL_ARGUMENTS"
                        step["detail"] = validation_error
                        if event_sink is not None:
                            await event_sink(
                                "tool_call",
                                {
                                    "index": tool_count, "tool": call.name, "status": "rejected",
                                    "elapsed_ms": 0, "intent": intent,
                                    "error": {"code": "INVALID_TOOL_ARGUMENTS", "detail": validation_error},
                                    "recovery": self._recovery_state(recovery_count, True),
                                },
                            )
                        recovery_count = self._claim_recovery(recovery_count)
                        recovery = self._recovery_message(call.name, "INVALID_TOOL_ARGUMENTS", validation_error)
                        if profile.tools == "lazy" and call.name not in expanded:
                            # 요약본만 보고 인자를 틀렸다 — 전체 정의를 오류와 함께 주고 이후 펼쳐 둔다.
                            expanded.append(call.name)
                            recovery = self._with_definition(recovery, definition)
                        messages.append(recovery)
                        should_retry = True
                        break
                    tool_arguments = self._bounded_arguments(call.name, arguments)
                    tool_started = time.monotonic()
                    try:
                        result = await self._queryforge.call_tool(
                            call.name,
                            tool_arguments,
                            application_session_id=qf_session_id,
                            timeout_seconds=self._remaining(deadline),
                        )
                    except TimeoutError as exc:
                        raise AgentTimeoutError("QueryForge call deadline exceeded") from exc
                    except QueryForgeUnavailable as exc:
                        raise AgentUpstreamUnavailable("QueryForge call failed") from exc
                    previous_qf_session_id = qf_session_id
                    qf_session_id = result.application_session_id or qf_session_id
                    if event_sink is not None and qf_session_id != previous_qf_session_id:
                        # 턴이 끝나기 전에도 dataset을 조회할 수 있도록 즉시 알린다.
                        await event_sink(QUERYFORGE_SESSION_EVENT, {"application_session_id": qf_session_id})
                    elapsed_ms = int((time.monotonic() - tool_started) * 1000)
                    if not result.ok:
                        failure = self._failure_of(result)
                        step["upstream_code"] = failure.code
                        step["detail"] = failure.hint or failure.message
                        will_recover = self._recoverable(failure) and recovery_count < self._recovery_budget
                        self._log_tool_call(session, tool_count, call.name, tool_arguments, False, elapsed_ms, will_recover, failure=failure)
                        if event_sink is not None:
                            await event_sink(
                                "tool_call",
                                {
                                    "index": tool_count, "tool": call.name, "status": "rejected",
                                    "elapsed_ms": elapsed_ms, "intent": intent,
                                    "error": {
                                        "code": failure.code,
                                        "detail": failure.hint or failure.message,
                                        "details": deepcopy(failure.safe_metadata),
                                    },
                                    "recovery": self._recovery_state(recovery_count, will_recover),
                                },
                            )
                        if self._recoverable(failure):
                            if recovery_count >= self._recovery_budget:
                                raise AgentQueryRejected(failure, tool_calls=tool_count, recovery_count=recovery_count)
                            recovery_count += 1
                            recovery = self._queryforge_recovery_message(call.name, failure)
                            if profile.tools == "lazy" and call.name not in expanded:
                                expanded.append(call.name)
                                recovery = self._with_definition(recovery, definition)
                            messages.append(recovery)
                            should_retry = True
                            break
                        raise AgentQueryRejected(failure, tool_calls=tool_count, recovery_count=recovery_count)
                    self._log_tool_call(session, tool_count, call.name, tool_arguments, True, elapsed_ms, False)
                    step.clear()
                    plan.append({"tool": call.name, "intent": deepcopy(intent)})
                    bounded = self._bounded_tool_result(result)
                    collect_facts(
                        {key: value for key, value in bounded.items() if key not in _NON_FACT_KEYS},
                        facts,
                    )
                    if fixes:
                        # 근거 수집 뒤에 붙인다 — 고친 자리의 색인 숫자가 근거로 잡히면 안 된다.
                        # 모델에 알려 다음 호출부터 바른 모양을 쓰게 한다.
                        bounded["arguments_normalized"] = list(fixes)
                    if event_sink is not None:
                        await event_sink(
                            "tool_call",
                            {
                                "index": tool_count, "tool": call.name, "status": "completed",
                                "elapsed_ms": elapsed_ms, "intent": intent,
                                "result": self._result_summary(bounded),
                            },
                        )
                    messages.append(
                        ProviderMessage(
                            "tool",
                            json.dumps(bounded, ensure_ascii=False, separators=(",", ":")),
                            tool_name=call.name,
                        )
                    )
                    warnings.extend(result.payload.get("warnings") or [])
                    reference = self._dataset_reference(result)
                    if reference is not None:
                        datasets[reference.dataset_id] = reference
                        lineage[reference.dataset_id] = self._lineage_entry(call.name, tool_arguments, result, reference)
                        if event_sink is not None:
                            await event_sink("dataset", reference.public())
                    if call.name == "schema" and isinstance(bounded.get("partition"), dict) and bounded.get("table"):
                        pkeys = bounded["partition"].get("keys")
                        if isinstance(pkeys, list):
                            partition_keys[str(bounded["table"])] = [str(k) for k in pkeys]
                    if call.name == "query":
                        query_calls += 1
                        active_table = tool_arguments.get("source", {}).get("table", active_table)
                        scope = tool_arguments.get("partition_scope")
                        if isinstance(scope, dict) and scope.get("kind") == "time_range":
                            active_period = {key: str(scope[key]) for key in ("from", "to") if key in scope}
                if should_retry:
                    continue
        except AgentError as exc:
            # 실패 지점과 그때까지 확보한 결과를 예외에 실어 API 계층으로 넘긴다.
            exc.tool_calls = exc.tool_calls or tool_count
            exc.recovery_count = exc.recovery_count or recovery_count
            exc.datasets = tuple(datasets.values())
            exc.warnings = tuple(warnings)
            exc.failed_step = dict(step) or None
            raise


    @staticmethod
    def _log_tool_call(
        session: Session,
        step: int,
        tool: str,
        arguments: Any,
        ok: bool,
        elapsed_ms: int,
        recovery_attempt: bool,
        *,
        failure: QueryForgeFailure | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "request_id": current_request_id(),
            "session_id": session.session_id,
            "step": step,
            "tool": tool,
            "argument_keys": sorted(arguments) if isinstance(arguments, dict) else [],
            "ok": ok,
            "error_code": failure.code if failure else code,
            "error_details": safe_log_value(failure.safe_metadata if failure else details),
            "hint": failure.hint if failure else None,
            "elapsed_ms": elapsed_ms,
            "recovery_attempt": recovery_attempt,
        }
        if LOG.isEnabledFor(logging.DEBUG):
            fields["arguments"] = safe_log_value(arguments)
        elif tool == "query" and isinstance(arguments, dict):
            # 조회 조건 요약 — 값까지 남긴다(2026-11-10 · graphio 25·16번). 셀 하나를 물은 조회가 "조회할 수
            # 없었다"로 끝났는데 키 이름만 남아 어떤 조건이 갔는지 알 수 없었다. 비밀 키는 가리고 500자에서 자른다
            fields["query_args"] = _query_args_summary(arguments)
        LOG.log(logging.INFO if ok else logging.WARNING, "tool_call", extra=fields)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentTimeoutError("Agent turn deadline exhausted")
        return remaining

    @staticmethod
    def _validate(tool: QueryForgeToolDefinition, arguments: Any) -> str | None:
        if not isinstance(arguments, dict):
            return "arguments must be a JSON object"
        errors = sorted(Draft202012Validator(tool.input_schema).iter_errors(arguments), key=lambda error: list(error.path))
        if not errors:
            return None
        # oneOf/anyOf 스키마에서 최상위 오류는 "is not valid under any of the given schemas"
        # 수준이라 모델이 무엇을 고쳐야 하는지 알 수 없다. 판별자가 맞는 가지까지 내려가
        # 구체적인 오류를 앞세우고, 해당 위치의 기대 스키마를 함께 돌려준다.
        primary = BoundedAgent._most_specific(max(errors, key=relevance))
        ordered = [primary, *(item for item in errors if item is not primary)]
        reported = [f"{BoundedAgent._error_path(item)}: {item.message}" for item in ordered[:3]]
        detail = " | ".join(reported)
        if tool.name == "query":
            # anyOf 가지가 긴 자리(filters 등)는 아래 기대 스키마가 400자를 넘어 빠진다.
            # 그 자리의 바른 모양을 문장으로 붙인다(query_args 머리말 · 2026-11-08).
            hint = query_args.shape_hint([BoundedAgent._error_path(item) for item in ordered[:3]])
            if hint:
                detail = f"{detail} | fix: {hint}"
        expected = json.dumps(primary.schema, ensure_ascii=False, separators=(",", ":"))
        if len(expected) <= 400:
            detail = f"{detail} | expected at {BoundedAgent._error_path(primary)}: {expected}"
        return detail[:2048]

    @staticmethod
    def _worst_usage(current: TokenUsage | None, candidate: TokenUsage | None) -> TokenUsage | None:
        if candidate is None:
            return current
        if current is None or (candidate.prompt_tokens or 0) > (current.prompt_tokens or 0):
            return candidate
        return current

    def _recovery_state(self, recovery_count: int, will_retry: bool) -> dict[str, Any]:
        return {"attempt": recovery_count + 1, "budget": self._recovery_budget, "will_retry": will_retry}

    @classmethod
    def _call_summary(cls, tool_name: str, arguments: Any) -> dict[str, Any]:
        """무엇을 하려는 호출인지 사용자에게 보여줄 최소 요약. 행 데이터는 담지 않는다."""
        if not isinstance(arguments, dict):
            return {}
        if tool_name == "query":
            return cls._query_summary(arguments)
        keys = cls._SUMMARY_KEYS.get(tool_name, ())
        return {key: arguments[key] for key in keys if isinstance(arguments.get(key), (str, int, bool))}

    @staticmethod
    def _query_summary(arguments: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        source = arguments.get("source")
        if isinstance(source, dict) and isinstance(source.get("table"), str):
            summary["table"] = source["table"]
        select = arguments.get("select")
        if isinstance(select, list):
            columns = [item["column"] for item in select if isinstance(item, dict) and isinstance(item.get("column"), str)]
            if columns:
                summary["select"] = columns[:10]
        aggregations = arguments.get("aggregations")
        if isinstance(aggregations, list):
            rendered = [
                f"{item.get('function')}({item.get('column')})" if item.get("column") else str(item.get("function"))
                for item in aggregations
                if isinstance(item, dict)
            ]
            if rendered:
                summary["aggregations"] = rendered[:10]
        group_by = arguments.get("group_by")
        if isinstance(group_by, list):
            grouped = [item for item in group_by if isinstance(item, str)]
            if grouped:
                summary["group_by"] = grouped[:10]
        scope = arguments.get("partition_scope")
        if isinstance(scope, dict):
            rendered_scope = {
                key: scope[key]
                for key in ("kind", "column", "from", "to")
                if isinstance(scope.get(key), (str, int))
            }
            if rendered_scope:
                summary["partition_scope"] = rendered_scope
        return summary

    @staticmethod
    def _result_summary(bounded: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for key in ("action", "table", "dataset_id", "row_count"):
            if isinstance(bounded.get(key), (str, int)):
                summary[key] = bounded[key]
        for key in ("tables", "columns", "relationships"):
            value = bounded.get(key)
            if isinstance(value, list):
                summary[f"{key}_count"] = len(value)
        if bounded.get("partitioned") is not None:
            summary["partitioned"] = bool(bounded["partitioned"])
        if bounded.get("truncated"):
            summary["truncated"] = True
        # 행 데이터는 담지 않지만, 경고 코드는 "0행이 왜 0행인지"를 trace에서
        # 바로 보여주는 단서라 요약에 남긴다.
        codes = [
            item["code"]
            for item in bounded.get("warnings") or []
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        ]
        if codes:
            summary["warning_codes"] = codes[:5]
        return summary

    @staticmethod
    def _error_path(error: Any) -> str:
        return "/".join(str(item) for item in error.absolute_path) or "$"

    @staticmethod
    def _most_specific(error: Any) -> Any:
        """판별자(const)가 어긋난 anyOf/oneOf 가지를 버리고 의도한 가지의 오류까지 내려간다."""
        while error.context:
            branches: dict[Any, list[Any]] = {}
            for item in error.context:
                branches.setdefault(item.schema_path[0] if item.schema_path else 0, []).append(item)
            viable = {
                key: items
                for key, items in branches.items()
                if not any(sub.validator in {"const", "enum"} for sub in items)
            }
            chosen = viable or branches
            error = min(chosen.values(), key=len)[0]
        return error

    def _claim_recovery(self, current: int) -> int:
        if current >= self._recovery_budget:
            raise AgentLimitError("Recovery budget exhausted")
        return current + 1

    @classmethod
    def _recoverable(cls, failure: QueryForgeFailure | None) -> bool:
        # retryable=False여도 인자를 고치면 통과할 수 있는 실패(UNKNOWN_COLUMN 등)가 많다.
        # 재시도로 결과가 달라질 수 없는 코드만 즉시 종료한다.
        return failure is not None and failure.code not in cls._TERMINAL_CODES

    @staticmethod
    def _failure_of(result: QueryForgeResult) -> QueryForgeFailure:
        if result.error is not None:
            return result.error
        # QueryForge가 structuredContent 없이 실패를 돌려준 경우에도 상류 코드를 남긴다.
        return QueryForgeFailure(
            code="UPSTREAM_UNSTRUCTURED_ERROR",
            message="QueryForge returned a failure without a structured error",
            retryable=False,
            hint="QueryForge tool response carried no error object",
        )

    @staticmethod
    def _recovery_message(tool_name: str, code: str, detail: str) -> ProviderMessage:
        return ProviderMessage(
            "tool",
            json.dumps({"ok": False, "error": {"code": code, "detail": detail}}, ensure_ascii=False),
            tool_name=tool_name,
        )

    @staticmethod
    def _queryforge_recovery_message(
        tool_name: str, failure: QueryForgeFailure
    ) -> ProviderMessage:
        error = {
            "code": failure.code,
            # 무엇이 틀렸는지 한 줄("query requires select columns or aggregations")이 여기만 있다.
            # 빼고 보내면 모델은 path 만 보고 추측한다(dlr_rDTXUMnNJoKruy0q4b38rHCK · 2026-11-08).
            "message": failure.message,
            "retryable": failure.retryable,
            "hint": failure.hint,
            "details": deepcopy(failure.safe_metadata),
        }
        if tool_name == "query":
            # 스키마 검증을 지나 QueryForge 실행 단계에서 거절된 경우에도 그 자리의 바른 모양을 붙인다.
            path = failure.safe_metadata.get("path")
            if isinstance(path, list) and path:
                fix = query_args.shape_hint(["/".join(str(item) for item in path)])
                if fix:
                    error["fix"] = fix
        return ProviderMessage(
            "tool",
            json.dumps({"ok": False, "error": error}, ensure_ascii=False, separators=(",", ":")),
            tool_name=tool_name,
        )

    def _bounded_tool_result(self, result: QueryForgeResult) -> dict[str, Any]:
        allowed = {
            "ok", "action", "tables", "table", "columns", "relationships", "dataset_id",
            "row_count", "preview", "summary", "statistics", "warnings", "truncated",
            # query.partition_scope를 만들려면 반드시 필요한 필드들. 빼면 모델이 파티션 키를 추측한다.
            "partitioned", "partition", "primary_key", "comment", "entity_labels",
            "parent_dataset_id",
            # QueryForge가 "showing 20 of 1000 rows"처럼 preview가 전부인지 일부인지 한 줄로 알린다.
            # 이 한 줄이 없으면 모델은 받은 몇 행을 전체 결과로 오인한다.
            # next_page는 나머지를 받는 transform 호출 인자이고, MCP만 쓰는 모델에게는
            # 유일한 페이징 경로다. 도구 설명도 둘을 보고 판단하라고 지시한다.
            "notice", "next_page",
        }
        bounded = {key: deepcopy(value) for key, value in result.payload.items() if key in allowed}
        if isinstance(bounded.get("preview"), list):
            bounded["preview"] = bounded["preview"][: self._preview_rows]
        # 목록을 말없이 자르면 모델은 전부 봤다고 착각한다. 찾던 컬럼이 잘려나간 뒤에도
        # 같은 자리를 계속 뒤지게 되므로, 잘랐다는 사실과 좁히는 방법을 함께 알린다.
        notices: list[dict[str, Any]] = []
        for key, limit in {"tables": 200, "columns": 120, "relationships": 20}.items():
            value = bounded.get(key)
            if isinstance(value, list) and len(value) > limit:
                notices.append(
                    {
                        "code": "DATALENS_TRUNCATED",
                        "field": key,
                        "action": "truncated",
                        "original_items": len(value),
                        "returned_items": limit,
                        "hint": f"{key} is truncated; narrow with name_pattern and call the tool again",
                    }
                )
                bounded[key] = value[:limit]
        if notices:
            bounded["truncated"] = True
            bounded["warnings"] = [*(bounded.get("warnings") or []), *notices]
        return bounded

    def _bounded_arguments(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        bounded = deepcopy(arguments)
        # Session identity is owned by DataLens, never by model-generated arguments.
        bounded.pop("session_id", None)
        if tool_name in {"query", "transform"}:
            requested = bounded.get("preview_rows", self._preview_rows)
            bounded["preview_rows"] = min(requested, self._preview_rows)
        return bounded

    @staticmethod
    def _lineage_entry(
        tool_name: str,
        arguments: dict[str, Any],
        result: QueryForgeResult,
        reference: DatasetReference,
    ) -> dict[str, Any]:
        """데이터셋이 어디서 왔는지 — 전부 도구 결과·인자에서 나온 사실이다."""
        entry: dict[str, Any] = {"dataset_id": reference.dataset_id, "action": tool_name}
        parent = result.payload.get("parent_dataset_id")
        if isinstance(parent, str):
            entry["parent_dataset_id"] = parent
        table = result.payload.get("table")
        if not isinstance(table, str):
            source = arguments.get("source") if isinstance(arguments, dict) else None
            table = source.get("table") if isinstance(source, dict) else None
        if isinstance(table, str):
            entry["table"] = table
        if isinstance(reference.row_count, int):
            entry["row_count"] = reference.row_count
        return entry

    def _dataset_reference(self, result: QueryForgeResult) -> DatasetReference | None:
        dataset_id = result.payload.get("dataset_id")
        if not isinstance(dataset_id, str):
            return None
        columns = result.payload.get("columns") or []
        preview = result.payload.get("preview") or []
        return DatasetReference(
            dataset_id=dataset_id,
            row_count=result.payload.get("row_count") if isinstance(result.payload.get("row_count"), int) else None,
            columns=tuple(item for item in columns if isinstance(item, dict)),
            preview=tuple(item for item in preview[: self._preview_rows] if isinstance(item, dict)),
        )

    async def preview(
        self, session: Session, message: str, profile: Profile, timeout_seconds: float = 5.0
    ) -> tuple[list[ProviderMessage], tuple[QueryForgeToolDefinition, ...], int, str | None]:
        """모델을 부르지 않고 이 설정으로 조립될 입력을 돌려준다(설정 화면 미리보기).

        회상 힌트는 실제 회상을 돌리지 않으므로 싣지 않는다 — 그 사실을 note 로 알린다.
        """
        note = None
        tools: tuple[QueryForgeToolDefinition, ...] = ()
        if profile.tools != "off":
            try:
                tools = tuple(await self._queryforge.discover_tools(timeout_seconds))
            except (TimeoutError, QueryForgeUnavailable):
                note = "QueryForge 도구 목록을 받지 못해 도구 정의를 0으로 셌다"
            names = {tool.name for tool in tools}
            tools = self._sent_tools(tools, profile.tools, [n for n in session.expanded_tools if n in names])
        messages = self._context(session, message, None, profile)
        if profile.memory:
            note = (note + " · " if note else "") + "회상 힌트는 질문마다 달라 미리보기에 싣지 않았다"
        return messages, tools, len(messages), note

    def profile(self, mode: Mode = "agent") -> Profile:
        if self._runtime is not None:
            return self._runtime.profile(mode)
        defaults = {"system_prompt": True, "session_context": True, "memory": True, "tools": "full", "history_items": 6}
        if mode == "evidence":
            defaults = {"system_prompt": True, "session_context": False, "memory": False, "tools": "off", "history_items": 6}
        return Profile(mode=mode, params={}, **defaults)

    @staticmethod
    def _output_policy(profile: Profile) -> OutputPolicy:
        params = profile.params
        if not params:
            return OutputPolicy()
        return OutputPolicy(
            options={key: params.get(key) for key in ("num_ctx", "temperature", "top_p", "top_k", "repeat_penalty", "num_predict")},
            think=params.get("think"),
            idle_timeout_seconds=params.get("idle_timeout_seconds"),
            keep_alive=params.get("keep_alive"),
        )

    def _sent_tools(
        self, tools: tuple[QueryForgeToolDefinition, ...], level: str, expanded: list[str]
    ) -> tuple[QueryForgeToolDefinition, ...]:
        if level == "summary":
            return tuple(self._summarize_tool(tool) for tool in tools)
        if level != "lazy" or not tools:
            return tools
        sent = tuple(tool if tool.name in expanded else self._lazy_tool(tool) for tool in tools)
        return (*sent, self._tool_spec_definition(tools))

    @staticmethod
    def _lazy_tool(tool: QueryForgeToolDefinition) -> QueryForgeToolDefinition:
        """lazy 요약 — 이름 · 설명 첫 줄(200자) · 최상위 인자의 type/enum/required 만.

        실측(2026-11-04): 도구 5개 32,058자 → 약 3,100자. 인자 검증은 여전히 전체 스키마로 한다
        (`tool_map`) — 요약은 모델에게 보여 주는 모양일 뿐 허용 범위를 넓히지 않는다.
        """

        def shallow(node: Any, depth: int) -> Any:
            if not isinstance(node, dict):
                return node
            out = {key: node[key] for key in ("type", "enum", "required") if key in node}
            if depth == 0 and isinstance(node.get("properties"), dict):
                out["properties"] = {name: shallow(value, depth + 1) for name, value in node["properties"].items()}
            return out

        first = tool.description.strip().split("\n", 1)[0][:200]
        description = f"{first} (요약본 — 전체 정의는 {TOOL_SPEC_NAME})"
        return QueryForgeToolDefinition(name=tool.name, description=description, input_schema=shallow(tool.input_schema, 0))

    @staticmethod
    def _tool_spec_definition(tools: tuple[QueryForgeToolDefinition, ...]) -> QueryForgeToolDefinition:
        return QueryForgeToolDefinition(
            name=TOOL_SPEC_NAME,
            description="도구의 전체 정의(설명·인자 스키마 전체)를 돌려준다. 요약본만으로 인자를 확신할 수 없을 때 그 도구를 부르기 전에 쓴다.",
            input_schema={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string", "enum": [tool.name for tool in tools]}},
            },
        )

    @staticmethod
    def _tool_spec_message(
        arguments: Any, tool_map: dict[str, QueryForgeToolDefinition], expanded: list[str]
    ) -> ProviderMessage:
        name = arguments.get("name") if isinstance(arguments, dict) else None
        tool = tool_map.get(name) if isinstance(name, str) else None
        if tool is None:
            body = {"ok": False, "error": {"code": "UNKNOWN_TOOL", "detail": f"available: {sorted(tool_map)}"}}
        else:
            if tool.name not in expanded:
                expanded.append(tool.name)
            body = {
                "ok": True,
                "tool": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "note": "이제 이 도구의 전체 정의가 도구 목록에도 실린다",
            }
        return ProviderMessage("tool", json.dumps(body, ensure_ascii=False, separators=(",", ":")), tool_name=TOOL_SPEC_NAME)

    @staticmethod
    def _with_definition(message: ProviderMessage, tool: QueryForgeToolDefinition) -> ProviderMessage:
        body = json.loads(message.content)
        body["tool_definition"] = {"description": tool.description, "input_schema": tool.input_schema}
        body["note"] = "요약본만 보고 부른 호출이 거부됐다. 위 전체 정의를 보고 인자를 고쳐 다시 불러라"
        return replace(message, content=json.dumps(body, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _summarize_tool(tool: QueryForgeToolDefinition) -> QueryForgeToolDefinition:
        """스키마 "요약" — 도구 설명은 첫 문단만, 입력 스키마는 중첩 설명·예시를 걷는다.

        도구 정의 5개가 약 31,000자다(2026-11-04 실측 · query 10K · transform 12K). 대부분이
        중첩 필드의 description 과 examples 라 구조(type·enum·required)는 남기고 그것만 뺀다.
        구조를 지우면 모델이 인자를 맞출 수 없어 검증 실패 → 복구 루프가 된다.
        """

        def strip(node: Any, depth: int, names: bool = False) -> Any:
            # names=True 는 `properties` 아래 — 키가 필드 이름이라 "description" 필드도 지우면 안 된다.
            if isinstance(node, dict):
                return {
                    key: strip(value, depth + 1, not names and key in {"properties", "$defs", "definitions"})
                    for key, value in node.items()
                    if names or not (key in {"examples", "example"} or (key == "description" and depth > 0))
                }
            if isinstance(node, list):
                return [strip(item, depth + 1) for item in node]
            return node

        description = tool.description.strip().split("\n\n", 1)[0][:400]
        return QueryForgeToolDefinition(name=tool.name, description=description, input_schema=strip(tool.input_schema, 0))

    def _context(
        self, session: Session, message: str, hints: str | None = None, profile: Profile | None = None
    ) -> list[ProviderMessage]:
        profile = profile or self.profile("agent")
        now = datetime.now(ZoneInfo(self._timezone))
        state = {
            "now": now.isoformat(timespec="seconds"),
            "today": now.date().isoformat(),
            "timezone": self._timezone,
            "active_table": session.active_table,
            "active_period": session.active_period,
            "active_dataset_id": session.active_dataset_id,
            # 이 세션에서 만들어진 데이터셋 목록(오래된 것부터). 모델은 여기 있는
            # dataset_id만 재사용할 수 있고, 없는 id를 지어내면 안 된다.
            "datasets": list(session.dataset_lineage),
        }
        if profile.mode == "evidence":
            # 데이터셋 계보·활성 테이블은 조회 에이전트의 상태다. 도구가 없는 모드에는 시각만 의미가 있다.
            state = {key: state[key] for key in ("now", "today", "timezone")}
        messages: list[ProviderMessage] = []
        if profile.system_prompt:
            kind = "evidence" if profile.mode == "evidence" else "system"
            prompt = load_system_prompt(session.locale, kind)
            if profile.tools == "lazy":
                prompt = f"{prompt}\n\n{_LAZY_HINT.get(session.locale, _LAZY_HINT['ko'])}"
            messages.append(ProviderMessage("system", prompt))
        if profile.session_context:
            messages.append(ProviderMessage("system", f"DataLens session context: {json.dumps(state, ensure_ascii=False)}"))
        if hints:
            messages.append(ProviderMessage("system", f"DataLens memory:\n{hints}"))
        history = session.turn_state[-profile.history_items:] if profile.history_items > 0 else ()
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str):
                messages.append(ProviderMessage(role, _history_content(role, content, profile.mode, session.locale)))
        messages.append(ProviderMessage("user", message))
        return messages

    @staticmethod
    def _updated_session(
        session: Session,
        user_message: str,
        answer: str,
        qf_session_id: str | None,
        new_lineage: tuple[dict[str, Any], ...],
        active_table: str | None,
        active_period: dict[str, str] | None,
        expanded_tools: tuple[str, ...] | None = None,
    ) -> Session:
        # 저장 상한은 설정 화면이 고를 수 있는 history_items 의 최대치다(실을 수 있는 만큼은 남긴다).
        history = (*session.turn_state, {"role": "user", "content": user_message}, {"role": "assistant", "content": answer})[-HISTORY_STORE_LIMIT:]
        # 같은 데이터셋이 다시 쓰이면 최신 자리로 옮긴다. 상한을 자를 때
        # 방금 재사용한 항목이 먼저 떨어져 나가면 안 되기 때문이다.
        merged: dict[str, dict[str, Any]] = {}
        for item in (*session.dataset_lineage, *new_lineage):
            merged.pop(item["dataset_id"], None)
            merged[item["dataset_id"]] = item
        lineage = tuple(merged.values())[-_LINEAGE_LIMIT:]
        return replace(
            session,
            queryforge_session_id=qf_session_id,
            active_dataset_id=new_lineage[-1]["dataset_id"] if new_lineage else session.active_dataset_id,
            active_table=active_table,
            active_period=active_period,
            turn_state=tuple(history),
            dataset_lineage=lineage,
            expanded_tools=session.expanded_tools if expanded_tools is None else expanded_tools,
        )
