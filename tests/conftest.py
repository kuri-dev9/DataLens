from __future__ import annotations

import pytest

from datalens.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_key="data-secret",
        queryforge_api_key="query-secret",
        queryforge_endpoint="http://queryforge.test:8080/mcp",
        queryforge_timeout_seconds=0.2,
    )


class ReadyProbe:
    def __init__(self, value: bool = True) -> None:
        self.value = value
        self.connected = False
        self.closed = False

    async def connect(self, timeout_seconds: float | None = None) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    async def ready(self, timeout_seconds: float) -> bool:
        return self.value

