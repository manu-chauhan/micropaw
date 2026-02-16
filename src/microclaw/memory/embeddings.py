"""Embedding providers for vector memory search.

Supports local sentence-transformers (BAAI/bge-small-en-v1.5), OpenAI, and
Ollama via httpx (no SDK dependencies), plus a null fallback.
"""

from __future__ import annotations

import json
import logging
import math
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"


class EmbeddingProvider(ABC):
    name: str
    dimensions: int

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text."""
        ...


class OpenAIEmbedding(EmbeddingProvider):
    """OpenAI embeddings via raw httpx."""
    name = "openai"
    dimensions = 1536

    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=30.0)

    async def embed(self, text: str) -> list[float]:
        resp = await self._client.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"input": text, "model": self.model},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]

    async def close(self) -> None:
        await self._client.aclose()


class OllamaEmbedding(EmbeddingProvider):
    """Ollama embeddings via raw httpx — for local models."""
    name = "ollama"
    dimensions = 768  # nomic-embed-text default

    def __init__(self, host: str = "http://localhost:11434", model: str = "nomic-embed-text") -> None:
        self.host = host.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(timeout=60.0)

    async def embed(self, text: str) -> list[float]:
        resp = await self._client.post(
            f"{self.host}/api/embed",
            json={"model": self.model, "input": text},
        )
        resp.raise_for_status()
        return resp.json()["embeddings"][0]

    async def close(self) -> None:
        await self._client.aclose()


class LocalEmbedding(EmbeddingProvider):
    """Local embeddings via sentence-transformers — no API key, runs on CPU."""
    name = "local"
    dimensions = 384  # bge-small-en-v1.5

    def __init__(self, model: str = _DEFAULT_LOCAL_MODEL) -> None:
        self.model_name = model
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise RuntimeError(
                    "sentence-transformers not installed. "
                    "Install with: pip install sentence-transformers"
                )
            logger.info("Loading embedding model: %s", self.model_name)
            self._model = SentenceTransformer(self.model_name)
            self.dimensions = self._model.get_sentence_embedding_dimension()
        return self._model

    async def embed(self, text: str) -> list[float]:
        import asyncio
        model = self._load()
        # Run in thread pool — model.encode is CPU-bound
        loop = asyncio.get_event_loop()
        vec = await loop.run_in_executor(None, lambda: model.encode(text, normalize_embeddings=True))
        return vec.tolist()


class NullEmbedding(EmbeddingProvider):
    """No-op embedding provider — disables vector search gracefully."""
    name = "none"
    dimensions = 0

    async def embed(self, text: str) -> list[float]:
        return []


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Pure Python, no deps."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def serialize_embedding(embedding: list[float]) -> str | None:
    if not embedding:
        return None
    return json.dumps(embedding)


def deserialize_embedding(data: str | None) -> list[float]:
    if not data:
        return []
    return json.loads(data)
