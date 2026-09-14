from pathlib import Path
import subprocess
import importlib.util


ROOT = Path(__file__).parents[2]


def test_dl_script_has_required_commands_and_valid_shell_syntax() -> None:
    script = ROOT / "dl.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    source = script.read_text()
    for command in ("up", "down", "restart", "rebuild", "status", "logs", "health", "smoke", "agent-check"):
        assert command in source
    assert "DATALENS_QUERYFORGE_API_KEY" not in source
    assert "/mcp" not in source
    assert "/data/" not in source


def load_chat():
    path = ROOT / "scripts" / "chat.py"
    spec = importlib.util.spec_from_file_location("datalens_test_chat", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sse_parser_handles_chunked_events_and_ignores_ping(monkeypatch) -> None:
    chat = load_chat()

    class Response:
        status = 200

        def __enter__(self): return self
        def __exit__(self, *args): return None
        def __iter__(self):
            return iter([
                b": ping\n", b"\n", b"event: token\n", b'data: {"text":"\xec\x95\x88"}\n', b"\n",
                b"event: token\n", b'data: {"text":"\xeb\x85\x95"}\n', b"\n",
                b"event: done\n", b'data: {"status":"completed"}\n', b"\n",
            ])

    monkeypatch.setattr(chat.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert list(chat.sse_events("POST", "/v1/sessions/s/messages", {"message": "x"})) == [
        ("token", {"text": "안"}),
        ("token", {"text": "녕"}),
        ("done", {"status": "completed"}),
    ]


def test_cli_uses_documented_session_scoped_paths(monkeypatch, capsys) -> None:
    chat = load_chat()
    monkeypatch.setattr(chat, "get_session", lambda: "dls_test")
    calls = []

    def fake_call(method, path, payload=None, timeout=600, base=None):
        calls.append((method, path))
        if path.endswith("/meta"):
            return 200, {"dataset_id": "ds_one", "row_count": 1, "columns": []}, 0
        if "/rows?" in path:
            return 200, {"dataset_id": "ds_one", "rows": [{"id": 1}]}, 0
        return 200, {"tables": [], "total_count": 0, "returned_count": 0, "truncated": False}, 0

    monkeypatch.setattr(chat, "call", fake_call)
    chat.tables("PM_", 1000)
    chat.dataset_rows("ds_one", 0, 1000)
    assert calls[0] == ("GET", "/v1/sessions/dls_test/catalog/tables?limit=1000&pattern=PM_")
    assert calls[1][1].startswith("/v1/sessions/dls_test/datasets/ds_one/rows?")
    assert calls[2] == ("GET", "/v1/sessions/dls_test/datasets/ds_one/meta")
