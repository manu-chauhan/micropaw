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


def ctx(cloud: bool):
    from micropaw.tools.base import ToolContext
    return ToolContext("conv", "user", "terminal", local_data=not cloud)


async def test_contacts_saved_in_local_mode_are_hidden_from_a_cloud_context(tmp_path):
    from micropaw.tools import contacts_tool
    store = MemoryStore(tmp_path / "data.db", NullEmbedding())
    await store.init()
    contacts_tool.set_store(store)
    tool = contacts_tool.ContactsTool()
    go_local()
    await tool.execute({"action": "add", "name": "Dr Rao", "phone": "555-0100", "notes": "chemo oncologist"},
                       ctx(False))
    go_normal()
    await tool.execute({"action": "add", "name": "Pat Plain"}, ctx(False))
    cloud = await tool.execute({"action": "list"}, ctx(True))
    assert "Pat Plain" in cloud and "Rao" not in cloud and "chemo" not in cloud
    assert "No contacts found" in await tool.execute({"action": "search", "query": "Rao"}, ctx(True))
    assert "Dr Rao" in await tool.execute({"action": "search", "query": "Rao"}, ctx(False))
    await store.close()


async def test_cron_jobs_made_in_local_mode_are_hidden_from_a_cloud_context(tmp_path):
    from micropaw.scheduler import Scheduler
    from micropaw.tools import cron_tool
    sched = Scheduler(str(tmp_path / "s.db"), None, None, [])
    await sched.init()
    cron_tool.set_scheduler(sched)
    go_local()
    await sched.add_job("remind me about the chemo appointment", 3600, one_shot=True)
    go_normal()
    await sched.add_job("water the plants", 3600, one_shot=True)
    out = await cron_tool.CronTool().execute({"action": "list"}, ctx(True))
    assert "water the plants" in out and "chemo" not in out
    assert "chemo" in await cron_tool.CronTool().execute({"action": "list"}, ctx(False))
    await sched.close()


async def test_files_saved_in_local_mode_are_hidden_from_a_cloud_context(tmp_path, monkeypatch):
    import micropaw.tools.files_tool as ft
    monkeypatch.setattr(ft, "_FILES_DIR", tmp_path / "files")
    tool = ft.FilesTool()
    go_local()
    await tool.execute({"action": "save", "path": "notes.txt", "content": "zebraquartz patient list"}, ctx(False))
    go_normal()
    await tool.execute({"action": "save", "path": "todo.txt", "content": "buy milk"}, ctx(False))
    assert "not found" in await tool.execute({"action": "read", "path": "notes.txt"}, ctx(True))
    listing = await tool.execute({"action": "list"}, ctx(True))
    assert "todo.txt" in listing and "notes.txt" not in listing
    assert await tool.execute({"action": "read", "path": "notes.txt"}, ctx(False)) == "zebraquartz patient list"
    listing = await tool.execute({"action": "list"}, ctx(False))
    assert "todo.txt" in listing and "notes.txt" in listing
    await tool.execute({"action": "save", "path": "notes.txt", "content": "new"}, ctx(False))  # one copy per name
    assert await tool.execute({"action": "read", "path": "notes.txt"}, ctx(True)) == "new"


async def test_saved_outputs_made_in_local_mode_are_hidden_from_a_cloud_context(tmp_path, monkeypatch):
    from micropaw import config
    from micropaw.tools import tool_output
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    tool = tool_output.ToolOutputTool()
    go_local()
    await tool_output.shorten("conv", "zebraquartz private page " * 2000)
    go_normal()
    await tool_output.shorten("conv", "public page " * 2000)
    local = await tool.execute({}, ctx(False))
    assert "Output 2" in local  # a local model reads the newest, numbers do not clash
    assert "public page" in await tool.execute({}, ctx(True))
    assert "zebraquartz" not in await tool.execute({"n": 1}, ctx(True))
    assert "No saved output 1" in await tool.execute({"n": 1}, ctx(True))
    assert "zebraquartz" in await tool.execute({"n": 1}, ctx(False))


def test_share_history_clears_every_flag_and_moves_the_local_files(tmp_path):
    import sqlite3

    from micropaw import storage
    db = tmp_path / "data.db"
    con = sqlite3.connect(db)
    for t in ("messages", "memories", "document_chunks", "contacts", "cron_jobs"):
        con.execute(f"CREATE TABLE {t} (id INTEGER, local_only INTEGER)")
        con.execute(f"INSERT INTO {t} VALUES (1, 1)")
    con.commit()
    con.close()
    (tmp_path / "files-local").mkdir()
    (tmp_path / "files-local" / "n.txt").write_text("x")
    (tmp_path / "outputs-local" / "conv").mkdir(parents=True)
    (tmp_path / "outputs-local" / "conv" / "1.txt").write_text("y")
    counts = storage.share_local_history(db)
    assert counts == {"messages": 1, "memories": 1, "document_chunks": 1, "contacts": 1, "cron_jobs": 1,
                      "summaries": 0, "files": 2}
    assert (tmp_path / "files" / "n.txt").read_text() == "x"
    assert (tmp_path / "outputs" / "conv" / "1.txt").read_text() == "y"
    assert not (tmp_path / "files-local").exists() and not (tmp_path / "outputs-local").exists()


def test_share_history_copes_with_an_old_or_empty_database(tmp_path):
    import sqlite3

    from micropaw import storage
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE messages (id INTEGER, content TEXT)")  # v0.3: no local_only column
    con.commit()
    con.close()
    assert sum(storage.share_local_history(old).values()) == 0
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert sum(storage.share_local_history(empty).values()) == 0
    assert sum(storage.share_local_history(tmp_path / "none.db").values()) == 0


def test_share_history_command_says_no_history_for_an_empty_file(monkeypatch, tmp_path):
    from click.testing import CliRunner

    import micropaw.cli as cli
    (tmp_path / "data.db").write_bytes(b"")
    monkeypatch.setattr(cli, "DB_FILE", tmp_path / "data.db")
    r = CliRunner().invoke(cli.main, ["privacy", "share-history", "--yes"])
    assert r.exit_code == 0 and "no saved history" in r.output
