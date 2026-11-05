"""Text saved in local mode stays away from cloud models and cloud embedders after the mode is loosened."""

from __future__ import annotations

import asyncio

import pytest

from micropaw import docsearch, privacy
from micropaw.config import PrivacyConfig
from micropaw.memory.embeddings import NullEmbedding
from micropaw.memory.store import MemoryStore

SECRET = "zebraquartz lymphoma diagnosis is confidential"


class Fake(NullEmbedding):
    def __init__(self, dims, name="local", base_url=""):
        self.dimensions, self.name, self.base_url, self.seen = dims, name, base_url, []

    async def embed(self, text):
        self.seen.append(text)
        return [0.1 * (i + 1) for i in range(self.dimensions)]


def go_local():
    privacy._reset_for_tests()
    privacy.freeze("local")
    privacy.configure(PrivacyConfig(mode="local"))


def go_normal():
    privacy._reset_for_tests()
    privacy.freeze("normal")


@pytest.fixture(autouse=True)
def _reset():
    yield
    privacy._reset_for_tests()


async def settle(store):
    await asyncio.gather(*list(store._tasks))


async def test_reembed_after_a_size_change_skips_local_mode_text_for_a_cloud_embedder(tmp_path):
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "data.db"
    go_local()
    store = MemoryStore(db, Fake(8))
    await store.init()
    await store.ingest_document("tax.txt", SECRET)
    await store.close()
    go_normal()
    cloud = Fake(16, "openai", "https://api.openai.com")
    store = MemoryStore(db, cloud)
    await store.init()
    await settle(store)
    assert cloud.seen == []
    await store.close()
    local = Fake(32, "ollama", "http://127.0.0.1:11434")  # a local server may embed it
    store = MemoryStore(db, local)
    await store.init()
    await settle(store)
    assert local.seen == [SECRET]
    await store.close()


async def test_reembed_of_normal_mode_text_still_runs_for_a_cloud_embedder(tmp_path):
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "data.db"
    go_normal()
    store = MemoryStore(db, Fake(8))
    await store.init()
    await store.ingest_document("a.txt", "plain public text")
    await store.close()
    cloud = Fake(16, "openai", "https://api.openai.com")
    store = MemoryStore(db, cloud)
    await store.init()
    await settle(store)
    assert cloud.seen == ["plain public text"]
    await store.close()


async def test_docsearch_hides_local_mode_documents_from_a_cloud_context(tmp_path):
    store = MemoryStore(tmp_path / "data.db", NullEmbedding())
    await store.init()
    go_local()
    await store.ingest_document("tax.txt", SECRET)
    go_normal()
    await store.ingest_document("pub.txt", "lymphoma is a public word here")
    hits = await docsearch.search(store, "lymphoma", include_local=False)
    assert [h["source"] for h in hits] == ["pub.txt"]
    assert "tax.txt" in [h["source"] for h in await docsearch.search(store, "lymphoma")]
    assert await docsearch.has_documents(store)
    only_local = MemoryStore(tmp_path / "b.db", NullEmbedding())
    await only_local.init()
    go_local()
    await only_local.ingest_document("tax.txt", SECRET)
    assert not await docsearch.has_documents(only_local, include_local=False)
    await only_local.close()
    await store.close()


async def test_web_search_docs_respect_a_cloud_context(tmp_path, monkeypatch):
    from micropaw.tools import web_search
    from micropaw.tools.base import ToolContext
    store = MemoryStore(tmp_path / "data.db", NullEmbedding())
    await store.init()
    go_local()
    await store.ingest_document("tax.txt", SECRET)
    go_normal()
    monkeypatch.setattr(web_search, "_store", store)
    cloud = ToolContext("c", "u", "terminal", local_data=False)
    assert await web_search._search_docs("lymphoma", 5, cloud) == []
    assert "lymphoma" in (await web_search._search_docs("lymphoma", 5, ToolContext("c", "u", "terminal")))[0]
    await store.close()
