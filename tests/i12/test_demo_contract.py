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
    assert "error.message" not in source
    assert "payload?.error?.message" not in source
