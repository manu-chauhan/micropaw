"""Store and startup changes for small machines: optional sqlite-vec, fewer embeds, bounded ingest, local-only rows."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from micropaw import privacy
from micropaw.bus import MessageBus, OutboundMessage
from micropaw.memory.embeddings import NullEmbedding
from micropaw.memory.store import MemoryStore
from micropaw.providers.base import Message


class Counting(NullEmbedding):
    """A 4-number embedder that counts calls and how many ran at once."""

    name = "fake"
    dimensions = 4

    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.peak = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.005)
        self.active -= 1
        h = hash(text) & 0xFFFFFFFF
        return [((h >> (i * 8)) & 0xFF) / 255.0 + 0.01 for i in range(4)]


@pytest.fixture(autouse=True)
def _normal_mode():
    privacy._reset_for_tests()
    privacy.freeze("normal")
    yield
    privacy._reset_for_tests()


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(tmp_path / "data.db", Counting())
    await s.init()
    yield s
    await s.close()


# --- sqlite-vec optional ---

def test_vec_requirement_marker_covers_only_arches_with_a_wheel():
    from packaging.markers import Marker
    from packaging.requirements import Requirement

    try:
        import tomllib
        from pathlib import Path
        deps = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["dependencies"]
    except (ImportError, OSError):
        pytest.skip("pyproject.toml is not next to the tests")
    req = next(Requirement(d) for d in deps if d.startswith("sqlite-vec"))
    marker: Marker = req.marker
    for machine in ("x86_64", "AMD64", "aarch64", "arm64"):
        assert marker.evaluate({"platform_machine": machine}), machine
    for machine in ("armv7l", "armv6l", "riscv64", "i686"):
        assert not marker.evaluate({"platform_machine": machine}), machine


async def test_store_works_with_sqlite_vec_blocked(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)  # import raises ImportError
    s = MemoryStore(tmp_path / "data.db", Counting())
    await s.init()
    assert not s._has_vec
    await s.ingest_document("/tmp/a.txt", "Revenue grew in the fourth quarter. " * 40, "u")
    found = await s.search_documents("revenue", 3, "u")
    assert found and found[0]["source"] == "/tmp/a.txt"
    await s.close()


async def test_database_with_a_vec_table_opens_when_the_extension_is_missing(tmp_path, monkeypatch):
    pytest.importorskip("sqlite_vec")
    first = MemoryStore(tmp_path / "data.db", Counting())
    await first.init()
    assert first._has_vec
    await first.ingest_document("/tmp/a.txt", "Alpha beta gamma. " * 60, "u")
    await first.close()
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    again = MemoryStore(tmp_path / "data.db", Counting())
    await again.init()
    assert not again._has_vec
    assert await again.search_documents("alpha", 3, "u")
    await again.ingest_document("/tmp/a.txt", "Delta epsilon. " * 60, "u")  # replaces, never touches vec_chunks
    assert "Delta" in (await again.search_documents("delta", 3, "u"))[0]["content"]
    await again.close()


async def test_extension_is_not_loaded_without_embeddings(tmp_path, monkeypatch):
    loaded = []
    monkeypatch.setattr(MemoryStore, "_try_load_vec", lambda self: loaded.append(1) or asyncio.sleep(0, False))
    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    assert not loaded
    await s.close()


async def test_new_vector_table_is_small(tmp_path):
    pytest.importorskip("sqlite_vec")
    s = MemoryStore(tmp_path / "data.db", Counting())
    await s.init()
    await s.ingest_document("/tmp/a.txt", "One chunk of text.", "u")
    await s.db.commit()
    await s.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await s.close()
    assert (tmp_path / "data.db").stat().st_size < 1_000_000


# --- the bus and the scheduler ---

async def test_outbound_consumer_without_timeout_sleeps_until_a_message_comes():
    bus = MessageBus(timeout=0.05)
    task = asyncio.create_task(bus.consume_outbound(timeout=None))
    await asyncio.sleep(0.3)  # several default timeouts pass: no wake-up, no None
    assert not task.done()
    await bus.publish_outbound(OutboundMessage(channel="c", chat_id="1", content="late"))
    got = await asyncio.wait_for(task, 1)
    assert got is not None and got.content == "late"


async def test_consumer_without_timeout_ends_when_cancelled():
    bus = MessageBus()
    task = asyncio.create_task(bus.consume_outbound(timeout=None))
    await asyncio.sleep(0)
    bus.close()
    task.cancel()
    start = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - start < 0.1


async def test_scheduler_connection_uses_normal_sync(tmp_path):
    from micropaw.scheduler import Scheduler

    sched = Scheduler(str(tmp_path / "data.db"), None, None, [])
    await sched.init()
    async with sched._db.execute("PRAGMA synchronous") as cur:
        assert (await cur.fetchone())[0] == 1
    await sched.close()


# --- documents: embeds, caps, tracked tasks ---

async def test_no_documents_no_embedding_call(store):
    assert await store.search_documents("anything", 5, "u") == []
    assert store.embedder.calls == 0
    await store.ingest_document("/tmp/a.txt", "Some stored text about cats. " * 30, "u")
    calls = store.embedder.calls
    assert await store.search_documents("cats", 5, "u")
    assert store.embedder.calls == calls + 1


async def test_deleting_the_last_document_stops_the_embedding_again(store):
    await store.ingest_document("/tmp/a.txt", "Some stored text about cats. " * 30, "u")
    await store.db.execute("DELETE FROM document_chunks")
    await store.db.commit()
    calls = store.embedder.calls
    assert await store.search_documents("cats", 5, "u") == []
    assert store.embedder.calls == calls


async def test_query_is_embedded_once_when_the_vector_index_finds_nothing(store):
    await store.ingest_document("/tmp/a.txt", "Some stored text about cats. " * 30, "u")
    store._has_vec = True  # pretend the index is there but returns no rows: the second path must reuse the vector
    calls = store.embedder.calls
    await store.search_documents("zzz", 5, "u")
    assert store.embedder.calls == calls + 1


async def test_auto_ingest_keeps_only_the_first_chunks(store):
    text = "\n\n".join(f"Paragraph {i} " + "word " * 150 for i in range(40))
    all_chunks = await store.ingest_document("/tmp/big.txt", text, "u")
    assert all_chunks > 5
    store.embedder.calls = 0
    kept = await store.ingest_document("/tmp/big.txt", text, "u", max_chunks=5)
    assert kept == 5 and store.embedder.calls == 5
    async with store.db.execute("SELECT COUNT(*) FROM document_chunks") as cur:
        assert (await cur.fetchone())[0] == 5


async def test_two_big_ingests_never_embed_at_once(store):
    text = "\n\n".join(f"Paragraph {i} " + "word " * 150 for i in range(6))
    await asyncio.gather(
        store.ingest_document("/tmp/a.txt", text, "u"), store.ingest_document("/tmp/b.txt", text + "x", "u"),
    )
    assert store.embedder.peak == 1


async def test_oldest_chunks_go_first_past_the_total_limit(tmp_path):
    s = MemoryStore(tmp_path / "data.db", Counting(), max_chunks=3)
    await s.init()
    for name in ("a", "b", "c", "d"):
        await s.ingest_document(f"/tmp/{name}.txt", f"text of file {name}", "u")
        await asyncio.sleep(0.002)
    async with s.db.execute("SELECT source FROM document_chunks ORDER BY id") as cur:
        assert [r[0] for r in await cur.fetchall()] == ["/tmp/b.txt", "/tmp/c.txt", "/tmp/d.txt"]
    if s._has_vec:
        async with s.db.execute("SELECT COUNT(*) FROM vec_chunks") as cur:
            assert (await cur.fetchone())[0] == 3
    await s.close()


async def test_background_work_is_tracked_and_cancelled_on_close(tmp_path):
    s = MemoryStore(tmp_path / "data.db", Counting())
    await s.init()
    gate = asyncio.Event()
    task = s.spawn(gate.wait())
    assert task in s._tasks
    await s.close()
    assert task.cancelled() and not s._tasks


async def test_secure_delete_pragma_follows_the_setting(tmp_path):
    for flag, expected in ((True, 1), (False, 0)):
        s = MemoryStore(tmp_path / f"{flag}.db", secure_delete=flag)
        await s.init()
        async with s.db.execute("PRAGMA secure_delete") as cur:
            assert (await cur.fetchone())[0] == expected
        await s.close()


# --- memory search runs off the event loop ---

async def test_memory_search_ranks_in_a_thread(store, monkeypatch):
    for i in range(5):
        await store.save_memory(f"key{i}", f"value about topic {i}")
    seen = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        seen.append(fn.__name__)
        return await real(fn, *a, **k)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    results = await store.search_memories("topic", limit=3)
    assert seen == ["_rank_memories"] and len(results) == 3


# --- rows made in local or airgapped mode stay away from cloud models ---

async def test_rows_made_in_local_mode_are_withheld_from_a_cloud_model(tmp_path):
    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    privacy._reset_for_tests()
    privacy.freeze("local")
    await s.save_memory("secret", "made locally")
    await s.ingest_document("/tmp/l.txt", "locally ingested words here", "u")
    await s.save_messages("c1", [Message(role="user", content="said locally"), Message(role="assistant", content="ok")])
    privacy._reset_for_tests()
    privacy.freeze("normal")
    await s.save_memory("public", "made in normal mode")
    await s.save_messages("c1", [Message(role="user", content="said normally")])

    assert {m["key"] for m in await s.get_recent_memories(10)} == {"secret", "public"}
    assert [m["key"] for m in await s.get_recent_memories(10, include_local=False)] == ["public"]
    assert [m["key"] for m in await s.search_memories("made", include_local=False)] == ["public"]
    assert [m["key"] for m in await s.get_all_memories(include_local=False)] == ["public"]
    assert await s.search_documents("locally", 5, "u")
    assert await s.search_documents("locally", 5, "u", include_local=False) == []
    assert [m.content for m in await s.get_messages("c1", 10, include_local=False)] == ["said normally"]
    assert len(await s.get_messages("c1", 10)) == 3
    assert len(await s.export_messages("c1", include_local=False)) == 1
    await s.close()


async def test_a_summary_of_local_talk_is_withheld_and_stays_flagged(tmp_path):
    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    privacy._reset_for_tests()
    privacy.freeze("local")
    await s.save_messages("c1", [Message(role="user", content=f"m{i}") for i in range(6)])
    await s.compact_messages("c1", "summary of local talk", keep_recent=2)
    privacy._reset_for_tests()
    privacy.freeze("normal")
    assert await s.get_summary("c1") == "summary of local talk"
    assert await s.get_summary("c1", include_local=False) is None
    await s.save_messages("c1", [Message(role="user", content="n1"), Message(role="user", content="n2")])
    await s.compact_messages("c1", "newer summary", keep_recent=2)  # drops local rows, so it stays flagged
    assert await s.get_summary("c1", include_local=False) is None
    await s.close()


async def test_old_database_gains_the_local_columns(tmp_path):
    import aiosqlite
    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, role TEXT, "
            "content TEXT, tool_calls TEXT, tool_call_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO messages (conversation_id, role, content) VALUES ('c', 'user', 'from before');")
        await db.commit()
    s = MemoryStore(path)
    await s.init()
    assert [m.content for m in await s.get_messages("c", 5, include_local=False)] == ["from before"]
    await s.close()


async def test_memory_tool_hides_local_memories_from_a_cloud_model(tmp_path):
    from micropaw.tools import memory_tool
    from micropaw.tools.base import ToolContext

    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    privacy._reset_for_tests()
    privacy.freeze("local")
    await s.save_memory("loc", "kept at home")
    privacy._reset_for_tests()
    privacy.freeze("normal")
    memory_tool.set_store(s)
    cloud = ToolContext(conversation_id="c", user_id="u", platform="terminal", local_data=False)
    home = ToolContext(conversation_id="c", user_id="u", platform="terminal", local_data=True)
    assert "kept at home" not in await memory_tool.MemoryTool().execute({"action": "list"}, cloud)
    assert "kept at home" in await memory_tool.MemoryTool().execute({"action": "list"}, home)
    assert "kept at home" not in await memory_tool.MemoryTool().execute({"action": "search", "query": "home"}, cloud)
    await s.close()


# --- a failover chain is local when every provider in it is ---

def test_a_failover_chain_of_local_providers_is_local():
    from micropaw.config import BudgetConfig
    from micropaw.memory.ledger import LateLedger
    from micropaw.providers.failover import FallbackProvider
    from micropaw.providers.metered import meter
    from micropaw.providers.ollama import OllamaProvider
    from micropaw.utils.prices import is_local_provider

    class Lan:
        name, model, host = "lan", "m", "http://127.0.0.1:8080"

    class Cloud:
        name, model, host = "claude", "m", "https://api.anthropic.com"

    local = OllamaProvider("http://localhost:11434", "m", 1000, 4096, "30m")
    assert is_local_provider(FallbackProvider([local, Lan()]))
    assert not is_local_provider(FallbackProvider([local, Cloud()]))  # a mixed chain may reach the cloud
    assert not is_local_provider(FallbackProvider([Cloud(), local]))
    metered = [meter(p, agent="main", role="chat", ledger=LateLedger(), budget=BudgetConfig()) for p in (local, Lan())]
    assert is_local_provider(FallbackProvider(metered))
    assert is_local_provider(meter(FallbackProvider([local, Lan()]), agent="main", role="chat",
                                   ledger=LateLedger(), budget=BudgetConfig()))


# --- a model that is not local compacts only what it saw ---

async def _local_then_normal(s: MemoryStore) -> None:
    privacy._reset_for_tests()
    privacy.freeze("local")
    await s.get_or_create_conversation("c1", "dm", "u")
    await s.save_messages("c1", [Message(role="user", content=f"local{i}") for i in range(6)])
    await s.compact_messages("c1", "local summary", keep_recent=2)
    privacy._reset_for_tests()
    privacy.freeze("normal")
    await s.save_messages("c1", [Message(role="user", content=f"normal{i}") for i in range(6)])


async def test_a_cloud_summary_keeps_the_local_messages_and_the_local_summary(tmp_path):
    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    await _local_then_normal(s)
    local_kept = [m.content for m in await s.get_messages("c1", 50) if m.content.startswith("local")]
    boundary = await s.keep_boundary("c1", 2, include_local=False)
    await s.compact_messages("c1", "cloud summary", keep_recent=2, before_id=boundary, include_local=False)
    left = [m.content for m in await s.get_messages("c1", 50)]
    assert [c for c in left if c.startswith("local")] == local_kept  # none of them went
    assert [c for c in left if c.startswith("normal")] == ["normal4", "normal5"]
    assert await s.get_summary("c1", include_local=False) == "cloud summary"  # a cloud model gets its own
    assert await s.get_summary("c1") == "local summary\n\ncloud summary"  # a local one gets both
    await s.close()


async def test_a_local_summary_after_a_cloud_one_folds_it_in(tmp_path):
    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    await _local_then_normal(s)
    await s.compact_messages("c1", "cloud summary", keep_recent=2,
                             before_id=await s.keep_boundary("c1", 2, include_local=False), include_local=False)
    await s.compact_messages("c1", "everything summary", keep_recent=2)  # a local model made it from both
    assert await s.get_summary("c1") == "everything summary"
    assert await s.get_summary("c1", include_local=False) is None
    await s.close()


async def test_sharing_history_keeps_the_cloud_summary_too(tmp_path):
    from micropaw import storage

    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    await _local_then_normal(s)
    await s.compact_messages("c1", "cloud summary", keep_recent=2,
                             before_id=await s.keep_boundary("c1", 2, include_local=False), include_local=False)
    await s.close()
    storage.share_local_history(tmp_path / "data.db")
    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    assert await s.get_summary("c1", include_local=False) == "local summary\n\ncloud summary"
    await s.close()


async def test_the_agent_compacts_with_a_cloud_model_without_touching_local_talk(tmp_path):
    from micropaw.config import Config
    from micropaw.core.agent import Agent
    from micropaw.providers.base import AIResponse
    from micropaw.tools.base import ToolRegistry
    from tests.test_agent import MockProvider

    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    await _local_then_normal(s)
    await s.save_messages("c1", [Message(role="user", content=f"later{i}") for i in range(30)])
    cloud = MockProvider([AIResponse(content="cloud summary", tool_calls=[], usage={})])
    cloud.name = "claude"
    agent = Agent(Config(), cloud, ToolRegistry(), s)
    await agent._compact("c1")
    left = [m.content for m in await s.get_messages("c1", 100)]
    assert [c for c in left if c.startswith("local")] == ["local4", "local5"]  # the ones the local summary left
    assert not [c for c in left if c.startswith("normal")]  # the cloud summary took the old ones
    assert await s.get_summary("c1", include_local=False) == "cloud summary"
    assert (await s.get_summary("c1")).startswith("local summary")
    await s.close()
