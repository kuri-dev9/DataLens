from __future__ import annotations

import math
from typing import Any

import httpx

from datalens.ports.memory import EmbeddingUnavailable


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    def __repr__(self) -> str:
        return f"OllamaEmbedder(model={self._model!r})"

    async def embed(self, text: str, timeout_seconds: float) -> tuple[float, ...]:
        if timeout_seconds <= 0:
            raise EmbeddingUnavailable("embedding deadline exhausted")
        try:
            response = await self._client.post(
                f"{self._base_url}/api/embed",
                json={"model": self._model, "input": text},
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingUnavailable("Ollama embedding request failed") from exc
        vector = self._first_vector(payload)
        if not vector:
            raise EmbeddingUnavailable("Ollama returned an empty embedding")
        return normalize(vector)

    @staticmethod
    def _first_vector(payload: Any) -> list[float]:
        if not isinstance(payload, dict):
            return []
        # /api/embed는 embeddings[[...]], 구버전 /api/embeddings는 embedding[...]을 돌려준다.
        embeddings = payload.get("embeddings")
        if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
            return [float(value) for value in embeddings[0] if isinstance(value, (int, float))]
        single = payload.get("embedding")
        if isinstance(single, list):
            return [float(value) for value in single if isinstance(value, (int, float))]
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


def normalize(vector: list[float]) -> tuple[float, ...]:
    """코사인 유사도를 내적 한 번으로 끝내기 위해 저장 전에 단위 벡터로 만든다."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise EmbeddingUnavailable("embedding vector has zero magnitude")
    return tuple(value / norm for value in vector)
