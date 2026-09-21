from pathlib import Path
import re


ROOT = Path(__file__).parents[2]


def test_dockerfile_is_non_root_and_excludes_development_runtime() -> None:
    source = (ROOT / "Dockerfile").read_text()
    assert "USER datalens" in source
    assert 'ENTRYPOINT ["datalens"]' in source
    assert "EXPOSE 8000" in source
    assert "pytest" not in source
    assert ".env" not in source


def test_build_context_excludes_secrets_and_local_artifacts() -> None:
    ignored = set((ROOT / ".dockerignore").read_text().splitlines())
    assert {".env", ".git", ".datalens", ".venv", "tests", "spikes"} <= ignored


def services_of(source: str) -> list[str]:
    block = source.split("services:", 1)[1].split("\nvolumes:", 1)[0]
    return re.findall(r"^  ([a-z][a-z0-9_-]*):$", block, re.MULTILINE)


def test_compose_owns_only_versioned_datalens_service() -> None:
    source = (ROOT / "docker-compose.yml").read_text()
    assert services_of(source) == ["datalens"]
    assert "datalens:0.1.0" in source
    assert "/v1/health" in source
    assert "/v1/ready" not in source
    assert "restart: unless-stopped" in source
    assert "stop_grace_period: 40s" in source


def test_runtime_lock_is_exactly_pinned() -> None:
    for filename in ("requirements.lock", "build-requirements.lock"):
        requirements = (ROOT / filename).read_text().splitlines()
        assert requirements
        assert all(re.fullmatch(r"[a-z0-9-]+==[^=\s]+", line) for line in requirements)


def test_locale_prompts_are_declared_as_wheel_artifacts() -> None:
    source = (ROOT / "pyproject.toml").read_text()
    assert 'artifacts = ["src/datalens/prompts/**/*.txt"]' in source
    assert (ROOT / "src/datalens/prompts/ko/system.txt").is_file()
    assert (ROOT / "src/datalens/prompts/ja/system.txt").is_file()


def test_compose_persists_learned_memory() -> None:
    # 컨테이너를 다시 만들어도 학습한 성공 플랜과 용어 사전은 남아야 한다.
    source = (ROOT / "docker-compose.yml").read_text()
    assert "datalens-memory:/var/lib/datalens" in source
    assert re.search(r"^volumes:\n  datalens-memory:", source, re.MULTILINE)
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "install -d -o datalens -g datalens /var/lib/datalens" in dockerfile
