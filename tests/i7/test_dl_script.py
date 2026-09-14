from pathlib import Path
import subprocess


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
