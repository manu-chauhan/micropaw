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


class Echo:
    """A model that repeats the context it was given, so a leak shows in the saved reply."""
    def __init__(self, host, log):
        self.name, self.model, self.host, self.log = "mock", "m", host, log

    async def chat(self, messages, tools=None):
        from micropaw.providers.base import AIResponse
        self.log.append(" ".join(m.content for m in messages))
        return AIResponse(content="You told me: " + messages[-1].content, tool_calls=[], usage={})


async def test_a_turn_that_saw_local_data_saves_its_rows_as_local_only(tmp_path):
    from micropaw.config import Config
    from micropaw.core.agent import Agent
    from micropaw.tools.base import ToolRegistry
    store = MemoryStore(tmp_path / "data.db", NullEmbedding())
    await store.init()
    go_local()
    await store.save_memory("diag", "secret diagnosis lymphoma")
    go_normal()
    log: list[str] = []
    agent = Agent(Config(), Echo("http://127.0.0.1:11434", log), ToolRegistry(), store)
    reply = await agent.handle_message("what do you remember?", "owner", "terminal")
    assert "lymphoma" in reply  # the local model saw the memory
    async with store.db.execute("SELECT role, local_only FROM messages ORDER BY id") as cur:
        assert await cur.fetchall() == [("user", 1), ("assistant", 1)]
    cloud_log: list[str] = []
    cloud = Agent(Config(), Echo("https://api.anthropic.com", cloud_log), ToolRegistry(), store)
    await cloud.handle_message("hello again", "owner", "terminal")
    assert "lymphoma" not in cloud_log[0]
    await store.close()


async def test_a_local_model_with_no_local_data_saves_plain_rows(tmp_path):
    from micropaw.config import Config
    from micropaw.core.agent import Agent
    from micropaw.tools.base import ToolRegistry
    store = MemoryStore(tmp_path / "data.db", NullEmbedding())
    await store.init()
    go_normal()
    agent = Agent(Config(), Echo("http://127.0.0.1:11434", []), ToolRegistry(), store)
    await agent.handle_message("hi", "owner", "terminal")
    async with store.db.execute("SELECT local_only FROM messages") as cur:
        assert [r[0] for r in await cur.fetchall()] == [0, 0]
    await store.close()


async def test_a_tainted_turn_saves_memories_as_local_only(tmp_path):
    from micropaw.tools import memory_tool
    from micropaw.tools.base import ToolContext
    store = MemoryStore(tmp_path / "data.db", NullEmbedding())
    await store.init()
    memory_tool.set_store(store)
    go_normal()
    tool = memory_tool.MemoryTool()
    await tool.execute({"action": "save", "key": "a", "value": "plain"}, ToolContext("c", "u", "terminal"))
    await tool.execute({"action": "save", "key": "b", "value": "from a tainted turn"},
                       ToolContext("c", "u", "terminal", tainted=True))
    assert [m["key"] for m in await store.get_all_memories(include_local=False)] == ["a"]
    await store.close()


async def test_reading_a_local_mode_file_taints_the_turn(tmp_path, monkeypatch):
    import micropaw.tools.files_tool as ft
    monkeypatch.setattr(ft, "_FILES_DIR", tmp_path / "files")
    tool = ft.FilesTool()
    go_local()
    await tool.execute({"action": "save", "path": "n.txt", "content": "private"}, ctx(False))
    go_normal()
    c = ctx(False)
    await tool.execute({"action": "read", "path": "n.txt"}, c)
    assert c.tainted


def _raw(db) -> bytes:
    from pathlib import Path
    db = Path(db)
    return b"".join(p.read_bytes() for p in (db, db.with_name(db.name + "-wal")) if p.exists())


WORDS = (b"zebraquartz", b"flamingoxylo", b"lymphoma")
TEXT = "zebraquartz lymphoma diagnosis is confidential flamingoxylo " * 30


async def test_forget_all_leaves_no_document_words_in_the_search_index_and_clears_jobs(tmp_path):
    import sqlite3

    from micropaw import storage
    from micropaw.scheduler import Scheduler
    db = tmp_path / "data.db"
    store = MemoryStore(db, NullEmbedding())
    await store.init()
    assert await docsearch.ensure_index(store.db)
    await store.ingest_document("tax.txt", TEXT)
    sched = Scheduler(str(db), None, None, [])
    await sched.init()
    await sched.add_job("remind me about the chemo appointment", 3600, one_shot=True)
    await sched.close()
    await store.close()
    assert all(w in _raw(db) for w in WORDS)
    assert storage.forget_all(db, tmp_path) == []
    raw = _raw(db)
    assert not any(w in raw for w in WORDS) and b"chemo" not in raw
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM cron_jobs").fetchone()[0] == 0
    con.close()


async def test_retention_leaves_no_old_document_words_in_the_search_index(tmp_path):
    db = tmp_path / "data.db"
    store = MemoryStore(db, NullEmbedding())
    await store.init()
    assert await docsearch.ensure_index(store.db)
    await store.ingest_document("tax.txt", TEXT)
    await store.db.execute("UPDATE document_chunks SET created_at = datetime('now', '-40 days')")
    await store.db.commit()
    assert (await store.purge_older_than(30))["chunks"] > 0
    await store.close()
    raw = _raw(db)
    assert not any(w in raw for w in WORDS)


def test_delegate_asks_before_a_cloud_helper_gets_a_task_from_a_local_model():
    from micropaw.tools.base import ToolContext, ToolRegistry
    from micropaw.tools.delegate import DelegateTool
    cloud = DelegateTool().bound(Echo("https://api.anthropic.com", []), ToolRegistry())
    local = DelegateTool().bound(Echo("http://127.0.0.1:11434", []), ToolRegistry())
    args = {"task": "Summarise: memory diag = secret diagnosis lymphoma"}
    go_local()
    asked = cloud.needs_approval(args, ToolContext("c", "u", "terminal"))
    assert asked and "lymphoma" in asked
    assert local.needs_approval(args, ToolContext("c", "u", "terminal")) is None
    assert cloud.needs_approval(args, ToolContext("c", "u", "terminal", local_data=False)) is None
    go_normal()
    assert cloud.needs_approval(args, ToolContext("c", "u", "terminal")) is None  # no local data was seen
    assert "lymphoma" in cloud.needs_approval(args, ToolContext("c", "u", "terminal", tainted=True))


async def test_share_history_on_a_database_made_by_the_store(tmp_path):
    from micropaw import storage
    from micropaw.providers.base import Message
    db = tmp_path / "data.db"
    store = MemoryStore(db, NullEmbedding())
    await store.init()
    go_local()
    await store.get_or_create_conversation("dm:owner", "dm", "owner")
    await store.save_messages("dm:owner", [Message(role="user", content="hello")])
    await store.save_memory("diag", "secret")
    await store.ingest_document("tax.txt", SECRET)
    await store.add_contact("Dr Rao")
    await store.close()
    go_normal()
    assert storage.local_row_count(db) == 4
    counts = storage.share_local_history(db)
    assert (counts["messages"], counts["memories"], counts["document_chunks"], counts["contacts"]) == (1, 1, 1, 1)
    assert storage.local_row_count(db) == 0
    store = MemoryStore(db, NullEmbedding())
    await store.init()
    assert [m["key"] for m in await store.get_all_memories(include_local=False)] == ["diag"]
    await store.close()


class _Peek:
    """A tool that reads local-only data: it taints the turn it runs in."""
    name, description, parameters, marks = "peek", "Read a local file.", {"type": "object", "properties": {}}, False

    def needs_approval(self, args, context):
        return None

    def to_tool_def(self):
        from micropaw.providers.base import ToolDef
        return ToolDef(name=self.name, description=self.description, parameters=self.parameters)

    async def execute(self, args, context):
        context.tainted = True
        return "private text"


class _CallsThenAnswers:
    def __init__(self, tool_name):
        self.name, self.model, self.host, self.calls = "mock", "m", "http://127.0.0.1:11434", 0
        self.tool_name = tool_name

    async def chat(self, messages, tools=None):
        from micropaw.providers.base import AIResponse, ToolCall
        self.calls += 1
        if self.calls == 1 and self.tool_name:
            return AIResponse(content="", tool_calls=[ToolCall("1", self.tool_name, {})], usage={})
        return AIResponse(content="done", tool_calls=[], usage={})


@pytest.mark.parametrize("tool_name,tainted", [("peek", True), ("", False)])
async def test_an_inline_helper_that_saw_local_data_taints_the_parent_turn(tool_name, tainted):
    from micropaw.tools.base import ToolContext, ToolRegistry
    from micropaw.tools.delegate import DelegateTool
    registry = ToolRegistry()
    registry.register(_Peek())
    helper = DelegateTool().bound(_CallsThenAnswers(tool_name), registry)
    ctx = ToolContext("c", "u", "terminal")
    assert await helper.execute({"task": "look"}, ctx) == "done"
    assert ctx.tainted is tainted


class _Reader(_Peek):
    """A tool that reads outside content (web, email): run_tool marks the turn."""
    name, marks = "reader", True

    async def execute(self, args, context):
        return "outside text"


async def test_an_inline_helper_that_read_outside_content_marks_the_parent_turn():
    from micropaw.tools.base import ToolContext, ToolRegistry
    from micropaw.tools.delegate import DelegateTool
    registry = ToolRegistry()
    registry.register(_Reader())
    helper = DelegateTool().bound(_CallsThenAnswers("reader"), registry)
    ctx = ToolContext("c", "u", "terminal")
    assert await helper.execute({"task": "look"}, ctx) == "done"
    assert ctx.marked is True
    quiet = ToolContext("c", "u", "terminal")
    assert await DelegateTool().bound(_CallsThenAnswers(""), registry).execute({"task": "x"}, quiet) == "done"
    assert quiet.marked is False
