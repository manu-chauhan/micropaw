"""Embedding providers for vector memory search.

Supports local fastembed/ONNX, OpenAI-style servers and Ollama via httpx (no SDK
dependencies), plus a null fallback.  Auto-detect picks Ollama, then local, never a cloud service.
"""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import time
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_LOCAL_MODEL = "nomic-ai/nomic-embed-text-v1.5"  # what installs before this setting existed use
_NEW_INSTALL_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"  # 67 MB, 384 numbers per text: right for a small board
_DEFAULT_OLLAMA_MODEL = "nomic-embed-text"
_MODEL_DIMS = {
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}
_TIMEOUT = httpx.Timeout(60.0, connect=5.0)  # a dead host fails in 5 s, a slow model gets 60 s
_RETRY_AFTER = 60.0  # seconds to leave a failed server alone: one log line, not one per message

_NO_MODEL = (
    "Embeddings are off: the model is not in {dir}. On a connected machine run 'paw models fetch embeddings', "
    "then copy that folder here."
)


class EmbeddingProvider(ABC):
    name: str
    dimensions: int

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text."""
        ...


class _Remote(EmbeddingProvider):
    """An embedding server reached over HTTP. After a failed call it is left alone for a minute."""

    label = "Embedding server"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        self._down_until = 0.0

    async def _request(self, text: str) -> list[float]:
        raise NotImplementedError

    async def embed(self, text: str) -> list[float]:
        if time.monotonic() < self._down_until:
            return []
        try:
            return await self._request(text)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError,
                httpx.UnsupportedProtocol, KeyError, IndexError, ValueError) as e:
            self._down_until = time.monotonic() + _RETRY_AFTER
            logger.warning("%s failed, memory search skips it for a minute: %s", self.label, e)
            return []

    async def close(self) -> None:
        await self._client.aclose()


class OpenAIEmbedding(_Remote):
    """OpenAI, or any OpenAI-style /v1/embeddings server (LM Studio, llama.cpp, vLLM), via raw httpx."""
    name = "openai"
    dimensions = 1536
    label = "OpenAI-style embedding server"

    def __init__(self, api_key: str = "", model: str = "text-embedding-3-small",
                 base_url: str = "https://api.openai.com") -> None:
        super().__init__()
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/").removesuffix("/v1")

    async def _request(self, text: str) -> list[float]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = await self._client.post(
            f"{self.base_url}/v1/embeddings", headers=headers, json={"input": text, "model": self.model},
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


class OllamaEmbedding(_Remote):
    """Ollama embeddings via raw httpx, on this machine or another one."""
    name = "ollama"
    dimensions = 768  # nomic-embed-text default
    label = "Ollama embeddings (is Ollama running?)"

    def __init__(self, host: str = "http://localhost:11434", model: str = "nomic-embed-text",
                 keep_alive: str = "1m") -> None:
        super().__init__()
        host = host.rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        self.host = host
        self.model = model
        # The chat model stays loaded for 30 minutes. This one leaves memory soon after the last message,
        # so on a small machine the two are not resident side by side.
        self.keep_alive = keep_alive

    async def _request(self, text: str) -> list[float]:
        body = {"model": self.model, "input": text}
        if self.keep_alive:
            body["keep_alive"] = int(self.keep_alive) if self.keep_alive.lstrip("-").isdigit() else self.keep_alive
        resp = await self._client.post(f"{self.host}/api/embed", json=body)
        resp.raise_for_status()
        return resp.json()["embeddings"][0]


def models_dir():
    """Where the embedding model files live: ~/.micropaw/models/fastembed."""
    from micropaw.config import CONFIG_DIR
    return CONFIG_DIR / "models" / "fastembed"


def choose_local_model(db_path, configured: str = "") -> str:
    """The fastembed model to use. An explicit choice wins. A database that already holds vectors keeps the
    model they were made with (nomic before this setting existed): another size would wipe them. A new
    one gets the small model."""
    if configured:
        return configured
    import sqlite3
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return _NEW_INSTALL_LOCAL_MODEL
    def first(sql: str):
        try:
            return con.execute(sql).fetchone()
        except sqlite3.Error:  # no such table: an empty or very old database
            return None

    try:
        row = first("SELECT value FROM kv_meta WHERE key = 'embedding_model'")
        if row and row[0].startswith("local:"):
            return row[0].removeprefix("local:")
        if first("SELECT 1 FROM document_chunks WHERE embedding IS NOT NULL LIMIT 1") or \
                first("SELECT 1 FROM memories WHERE embedding IS NOT NULL LIMIT 1"):
            return _DEFAULT_LOCAL_MODEL
        return _NEW_INSTALL_LOCAL_MODEL
    finally:
        con.close()


def offline_env() -> None:
    """Stop Hugging Face and onnxruntime from phoning home. Set before the libraries are imported, and
    forced (not setdefault) in local and airgapped mode, so a value in .env cannot turn it back on."""
    from micropaw import privacy
    values = {"HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "HF_HUB_DISABLE_XET": "1"}
    if privacy.local_only():
        values["HF_HUB_OFFLINE"] = "1"
    for key, value in values.items():
        os.environ[key] = value


class LocalEmbedding(EmbeddingProvider):
    """Local embeddings via fastembed/ONNX: no API key, runs on CPU, no PyTorch.

    The model loads on the first message, not at start, and only from files already on this machine in
    local and airgapped mode. If it cannot load, embeddings stay off and the log says how to fix it.
    """
    name = "local"

    def __init__(self, model: str = _DEFAULT_LOCAL_MODEL) -> None:
        import threading
        self.model_name = model
        self.dimensions = _MODEL_DIMS.get(model, 768)
        self._model = None
        self._failed = False
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            offline_env()
            try:
                from fastembed import TextEmbedding
            except ImportError:
                import platform
                raise RuntimeError(
                    f"fastembed is not installed or has no build for this machine ({platform.machine()}). "
                    "Install it with: pip install 'micropaw[embeddings]'. "
                    "On a 32-bit board use Ollama embeddings instead."
                )
            from micropaw import privacy
            from micropaw.config import private_dir
            folder = models_dir()
            private_dir(folder)
            # Files already here are used as they are. Without them, local and airgapped never download.
            offline = privacy.local_only() or any(folder.iterdir())
            logger.info("Loading embedding model: %s", self.model_name)
            kwargs = {"cache_dir": str(folder), "threads": 2}
            try:
                self._model = TextEmbedding(model_name=self.model_name, local_files_only=offline, **kwargs)
            except Exception:
                if privacy.local_only():
                    raise
                self._model = TextEmbedding(model_name=self.model_name, **kwargs)  # normal mode: download it
            try:  # onnxruntime can send telemetry events on Windows
                import onnxruntime
                onnxruntime.disable_telemetry_events()
            except (ImportError, AttributeError):
                pass
        return self._model

    async def warmup(self) -> None:
        """Load the model now (used by `paw models fetch embeddings`, not at start)."""
        import asyncio
        await asyncio.get_running_loop().run_in_executor(None, self._load)

    async def embed(self, text: str) -> list[float]:
        import asyncio
        if self._failed:
            return []
        try:
            vec = await asyncio.get_running_loop().run_in_executor(
                None, lambda: list(self._load().embed([text]))[0].tolist()
            )
        except Exception as e:
            self._failed = True
            logger.warning("%s (%s)", _NO_MODEL.format(dir=models_dir()), e)
            return []
        if len(vec) != self.dimensions:  # a model that is not in the table: never mix sizes in one index
            self._failed = True
            logger.warning("Embeddings are off: %s gives %d numbers, expected %d.", self.model_name, len(vec),
                           self.dimensions)
            return []
        return vec


def fetch_local_model(model: str) -> None:
    """Download an embedding model into models_dir(). The only place that goes online for model files."""
    from fastembed import TextEmbedding

    from micropaw.config import private_dir
    folder = models_dir()
    private_dir(folder)
    os.environ.pop("HF_HUB_OFFLINE", None)
    list(TextEmbedding(model_name=model, cache_dir=str(folder), threads=2).embed(["test"]))


class NullEmbedding(EmbeddingProvider):
    """No-op embedding provider — disables vector search gracefully."""
    name = "none"
    dimensions = 0

    async def embed(self, text: str) -> list[float]:
        return []


async def probe_ollama(host: str, model: str = _DEFAULT_OLLAMA_MODEL) -> bool:
    """Check if Ollama is running and has the specified embedding model pulled."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.0)) as client:  # a down host costs 1 s
            resp = await client.get(f"{host.rstrip('/')}/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
            return any(m.get("name", "").startswith(model) for m in models)
    except Exception:
        return False


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


# --- BLOB format (for document_chunks table — 5-10x faster than JSON) ---

def serialize_embedding_blob(embedding: list[float]) -> bytes | None:
    if not embedding:
        return None
    return struct.pack(f"{len(embedding)}f", *embedding)


def deserialize_embedding_blob(data: bytes | None) -> list[float]:
    if not data:
        return []
    return list(struct.unpack(f"{len(data) // 4}f", data))
