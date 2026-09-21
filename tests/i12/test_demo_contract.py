from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).parents[2]
DEMO = ROOT / "demo" / "datalens-demo.html"


class DemoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts = []
        self.styles = []
        self._capture = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script":
            assert "src" not in values
            self._capture = self.scripts
        elif tag == "style":
            self._capture = self.styles
        elif tag in {"link", "img"}:
            source = values.get("href") or values.get("src") or ""
            assert not source.startswith(("http://", "https://", "//"))

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._capture = None

    def handle_data(self, data):
        if self._capture is not None:
            self._capture.append(data)


def test_ui_ac1_ac9_demo_is_single_self_contained_agent_tab_file() -> None:
    parser = DemoParser()
    parser.feed(DEMO.read_text())
    source = DEMO.read_text()
    assert len(parser.scripts) == 1
    assert len(parser.styles) == 1
    assert "const agents =" in source
    assert "sessionStorage" in source
    assert "localStorage" not in source
    assert "＋ 준비 중" in source


def test_ui_ac2_through_ac10_use_only_documented_paths_and_stream_contract() -> None:
    source = DEMO.read_text()
    for required in (
        "fetch(", ".body.getReader()", "TextDecoder", "text/event-stream", "AbortController",
        'name==="token"', 'name==="tool_call"', 'name==="dataset"', 'name==="done"',
        "TTFT", "limit=1000", "offset=${offset}", 'locale:"ko"', 'setLocale("ja")',
        "/v1/sessions", "/messages", "/catalog/tables", "/datasets/",
    ):
        assert required in source
    assert "EventSource(" not in source
    assert "session_id=" not in source
    assert "loadDataset(data,answer)" in source


def test_ui_error_messages_do_not_render_raw_server_message() -> None:
    source = DEMO.read_text()
    assert "errorMessages" in source
    assert ".textContent=error.message" not in source
    assert "payload?.error?.message" not in source


def test_dbg_ac1_through_ac5_debug_console_contract() -> None:
    source = DEMO.read_text()
    for required in (
        'id="debugConsole"', "collapsed", 'storageKey("debug")', "debugLogs.unshift",
        "received_at", "elapsed_ms", "ttft_ms", "total_ms", "token_count",
        "tokens_per_second", "maskedHeaders", '.slice(0,6)', "navigator.clipboard.writeText",
        "token 이벤트", "최종 응답 원문", "retryable", "details", '"ping"',
    ):
        assert required in source


def test_fix_ac1_through_ac4_partial_response_is_preserved_on_stream_failure() -> None:
    source = DEMO.read_text()
    assert 'if(name==="error")streamError=data' in source
    assert 'if(!doneData)throw Object.assign(new Error("STREAM_INTERRUPTED")' in source
    assert 'const partial=tokenCount>0||Boolean(answer.querySelector(".dataset"))' in source
    assert 'badge.textContent=error.name==="AbortError"?"응답을 취소했습니다":"응답이 중단되었습니다"' in source
    assert 'addError(error,lastRetry,partial)' in source
    assert 'answer.remove();progress.remove()' in source  # 세션 재생성 경로에만 사용
    assert 'code==="DL_AGENT_LIMIT"&&partial?"답변 생성이 중단되었습니다. 이어서 질문해 주세요."' in source


def test_msg_ac1_query_rejection_message_uses_upstream_code() -> None:
    source = DEMO.read_text()
    assert "details?.upstream_code" in source
    assert "MISSING_PARTITION_SCOPE:" in source and "INVALID_PARTITION_SCOPE:" in source
    assert "PARTITION_SCOPE_TOO_WIDE:" in source
    assert "TABLE_NOT_ALLOWED:" in source and "POLICY_VIOLATION:" in source
    assert "조회 범위 지정에 문제가 있습니다." in source
    assert "조회 기간이 보유 구간을 벗어났습니다." in source
    assert "허용되지 않은 데이터 요청입니다." in source
    assert "데이터 요청을 처리하지 못했습니다." in source


def test_msg_failed_step_is_rendered_for_the_user() -> None:
    source = DEMO.read_text()
    assert "metadata?.failed_step" in source
    assert "번째 도구 호출" in source
    assert "failureTrace(error)" in source


def test_progress_trace_is_rendered_for_every_tool_call() -> None:
    source = DEMO.read_text()
    # 진행 트레이스는 시작/완료/실패를 모두 렌더링하고, 실패 시 사유와 재시도를 보여준다.
    assert 'if(name==="tool_call"){renderTrace(trace,data)' in source
    assert 'row.className="trace-step bad"' in source
    assert "재시도 ${data.recovery.attempt}/${data.recovery.budget}" in source
    assert "더 시도하지 않음" in source
    # 실패해도 어디까지 갔는지가 남아야 한다 — progress만 제거하고 trace는 유지한다.
    assert "trace.remove()" not in source


def test_progress_trace_names_what_each_step_tried() -> None:
    source = DEMO.read_text()
    for label in ("테이블 목록", "구조", "조회", "관계 탐색"):
        assert label in source
    assert "intent.partition_scope" in source and "intent.group_by" in source
    assert "function intentText(" in source and "function resultText(" in source


def test_progress_trace_stays_one_line_per_step() -> None:
    source = DEMO.read_text()
    # 스키마 조각 같은 모델용 원문은 줄에 펼치지 않고 title 툴팁으로 보낸다.
    assert "const briefly=" in source
    assert "row.title=" in source
    assert "white-space:nowrap" in source and "text-overflow:ellipsis" in source
    assert 'what.textContent=`${label} → ${briefly(data.error?.code||"실패")}${retry}`' in source


def test_memory_recall_is_shown_in_the_trace() -> None:
    source = DEMO.read_text()
    assert 'if(name==="memory")renderMemory(trace,data)' in source
    assert "기억 참고:" in source
    assert "과거 풀이 " in source and "용어 " in source
