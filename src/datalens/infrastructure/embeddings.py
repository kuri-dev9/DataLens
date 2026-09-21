from __future__ import annotations

import math
from typing import Any

import httpx

from datalens.ports.memory import EmbeddingUnavailable


class HttpEmbedder:
    """임베딩 서버 어댑터.

    LLM과 같은 Ollama를 써도 되고, CPU 전용 인스턴스나 llama.cpp의
    OpenAI 호환 서버를 따로 띄워도 된다. 주소와 API 형식만 바꾸면 된다.
    """

    OLLAMA = "ollama"
    OPENAI = "openai"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api: str = OLLAMA,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if api not in {self.OLLAMA, self.OPENAI}:
            raise ValueError("embedding api must be 'ollama' or 'openai'")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api = api
        self._api_key = api_key
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    def __repr__(self) -> str:
        return f"HttpEmbedder(api={self._api!r}, model={self._model!r}, base_url={self._base_url!r})"

    async def embed(self, text: str, timeout_seconds: float) -> tuple[float, ...]:
        if timeout_seconds <= 0:
            raise EmbeddingUnavailable("embedding deadline exhausted")
        path = "/api/embed" if self._api == self.OLLAMA else "/v1/embeddings"
        headers = {"authorization": f"Bearer {self._api_key}"} if self._api_key else None
        try:
            response = await self._client.post(
                f"{self._base_url}{path}",
                json={"model": self._model, "input": text},
                headers=headers,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingUnavailable(f"{self._api} embedding request failed") from exc
        vector = self._first_vector(payload)
        if not vector:
            raise EmbeddingUnavailable(f"{self._api} returned an empty embedding")
        return normalize(vector)

    @staticmethod
    def _first_vector(payload: Any) -> list[float]:
        if not isinstance(payload, dict):
            return []
        # Ollama /api/embed는 embeddings[[...]], 구버전 /api/embeddings는 embedding[...],
        # OpenAI 호환 서버는 data[0].embedding을 돌려준다.
        embeddings = payload.get("embeddings")
        if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
            return _floats(embeddings[0])
        single = payload.get("embedding")
        if isinstance(single, list):
            return _floats(single)
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return _floats(data[0].get("embedding"))
        return []

    async def ready(self, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            return False
        try:
            await self.embed("ping", timeout_seconds)
            return True
        except EmbeddingUnavailable:
            return False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _floats(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    return [float(item) for item in value if isinstance(item, (int, float)) and not isinstance(item, bool)]


def normalize(vector: list[float]) -> tuple[float, ...]:
    """코사인 유사도를 내적 한 번으로 끝내기 위해 저장 전에 단위 벡터로 만든다."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise EmbeddingUnavailable("embedding vector has zero magnitude")
    return tuple(value / norm for value in vector)
