"""Session search: the FTS index, safe quoting, agent scope, marks, the backfill and the /search command."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from micropaw.memory.store import MemoryStore
from micropaw.providers.base import Message
from micropaw.tools import sessions
from micropaw.tools.base import ToolContext, run_tool

needs_fts = pytest.mark.skipif(
    not any("FTS5" in str(r) for r in sqlite3.connect(":memory:").execute("pragma compile_options")),
    reason="this SQLite build has no FTS5")


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    sessions.set_store(s)
    yield s
    await s.close()


async def say(store, conv, *pairs):
    await store.get_or_create_conversation(conv, "dm", "u")
    await store.save_messages(conv, [Message(role=r, content=t) for r, t in pairs])


def ctx(conv="dm:owner") -> ToolContext:
    return ToolContext(conversation_id=conv, user_id="u", platform="terminal")


pytestmark = needs_fts


async def test_finds_user_and_assistant_text_but_not_tool_output(store):
    await say(store, "dm:owner", ("user", "my bicycle is red"), ("assistant", "noted, a red bike"),
              ("tool", "bicycle secret page text"))
    hits = await store.search_messages("bicycle")
    assert [h["role"] for h in hits] == ["user"]
    assert "[bicycle]" in hits[0]["snippet"]


async def test_words_are_anded(store):
    await say(store, "dm:owner", ("user", "red bicycle"), ("user", "blue bicycle"))
    assert len(await store.search_messages("red bicycle")) == 1
    assert len(await store.search_messages("bicycle")) == 2


@pytest.mark.parametrize("hostile", [
    '" OR 1 --', "NEAR(red bicycle)", 'red" OR "x', "bicycle*", "col:bicycle", "-bicycle", "AND", "OR NOT",
    '"', '"""', "(", ")", "'; DROP TABLE messages; --", "bicycle\x00",
])
async def test_hostile_input_is_plain_words_never_syntax(store, hostile):
    await say(store, "dm:owner", ("user", "red bicycle"), ("user", "OR 1"))
    await store.search_messages(hostile)  # must not raise a syntax error
    async with store.db.execute("SELECT COUNT(*) FROM messages") as cur:
        assert (await cur.fetchone())[0] == 2


async def test_quote_breakout_does_not_widen_the_search(store):
    await say(store, "dm:owner", ("user", "red bicycle"), ("user", "green tree"))
    assert await store.search_messages('red" OR "tree') == []  # one phrase-word each, ANDed
    assert store.fts_query('a" b') == '"a""" "b"'
    assert store.fts_query('" OR 1 --') == '"OR" "1"'
    assert store.fts_query("NEAR(x y)") == '"NEAR(x" "y)"'
    assert store.fts_query("   ") == "" and store.fts_query('"') == ""


async def test_cut_never_splits_an_escaped_quote(store):
    for hostile in ("a" + '"' * 70, '"' * 63 + "a" * 5, "a" * 63 + '"' * 9):
        q = store.fts_query(hostile)
        assert q.count('"') % 2 == 0
        await store.search_messages(hostile)  # no syntax error


async def test_limits_words_and_length(store):
    q = store.fts_query(" ".join(["w"] * 40) + " " + "z" * 500)
    assert q.count('"') // 2 == 12
    assert store.fts_query("z" * 500) == '"' + "z" * 64 + '"'


async def test_scope_never_crosses_agents(store):
    await say(store, "dm:owner", ("user", "shared kiwi"))
    await say(store, "agent:coder:dm:owner", ("user", "coder kiwi"))
    await say(store, "agent:family:dm:owner", ("user", "family kiwi secret"))
    await say(store, "agent:coder2:dm:owner", ("user", "coder2 kiwi"))
    main = await store.search_messages("kiwi", "")
    assert [h["conversation"] for h in main] == ["dm:owner"]
    coder = await store.search_messages("kiwi", "agent:coder:")
    assert [h["conversation"] for h in coder] == ["agent:coder:dm:owner"]


async def test_sessions_tool_search_is_scoped_and_marks(store):
    await say(store, "dm:owner", ("user", "kiwi in main"))
    await say(store, "agent:family:dm:owner", ("user", "kiwi in family"))
    tool = sessions.SessionsTool()
    assert tool.marks is True
    c = ctx("agent:family:dm:owner")
    out = await run_tool(tool, {"action": "search", "query": "kiwi"}, c)
    assert "kiwi in family" not in out or "[kiwi]" in out  # snippet marks the hit
    assert "agent:family:dm:owner" in out and "dm:owner]" not in out.replace("agent:family:dm:owner", "")
    assert "main" not in out
    assert c.marked is True
    c2 = ctx("dm:owner")
    out = await run_tool(tool, {"action": "search", "query": "kiwi"}, c2)
    assert "agent:family" not in out and "may come from web pages or email" in out


async def test_search_with_no_words_or_no_match(store):
    tool = sessions.SessionsTool()
    assert "at least one word" in await run_tool(tool, {"action": "search", "query": '" --'}, ctx())
    assert await run_tool(tool, {"action": "search", "query": "nothing"}, ctx()) == "No matches."


async def test_triggers_follow_inserts_and_deletes(store):
    await say(store, "dm:owner", ("user", "alpha one"), ("assistant", "alpha two"))
    assert len(await store.search_messages("alpha")) == 2
    await store.compact_messages("dm:owner", "summary", 1)  # deletes the older message
    assert len(await store.search_messages("alpha")) == 1
    await store.db.execute("DELETE FROM messages")
    await store.db.commit()
    assert await store.search_messages("alpha") == []


async def test_backfill_indexes_an_old_database_in_the_background(tmp_path):
    path = tmp_path / "old.db"
    s = MemoryStore(path)
    await s.init()
    await s.get_or_create_conversation("dm:owner", "dm", "u")
    rows = [("dm:owner", "user", f"old message {i} walrus", None, None) for i in range(25)]
    await s.db.executemany(
        "INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id) VALUES (?, ?, ?, ?, ?)", rows)
    await s.db.executescript("DROP TRIGGER messages_fts_ai; DROP TRIGGER messages_fts_ad; DROP TABLE messages_fts;")
    await s.db.execute("DELETE FROM kv_meta WHERE key LIKE 'fts_%'")
    await s.db.commit()
    await s.close()

    s = MemoryStore(path)
    await s.init()  # the table is new again: init returns at once, the backfill runs as a task
    assert s._fts_task is not None
    await s._backfill_fts(batch=10)  # same work, in small batches (idempotent with the running task)
    await s._fts_task
    assert len(await s.search_messages("walrus", limit=50)) == 25
    # a message saved while the backfill was still going is indexed by the trigger, and not twice
    await s.save_messages("dm:owner", [Message(role="user", content="fresh walrus")])
    assert len(await s.search_messages("walrus", limit=50)) == 26
    await s.close()


async def test_second_start_does_not_backfill_again(store, tmp_path):
    await say(store, "dm:owner", ("user", "once"))
    path = store.db_path
    await store.close()
    s = MemoryStore(path)
    await s.init()
    assert await s._get_meta("fts_backfill_at") == await s._get_meta("fts_backfill_to")
    assert len(await s.search_messages("once")) == 1
    await s.close()


async def test_search_works_when_fts_is_missing(store):
    store.has_fts = False
    assert await store.search_messages("anything") == []
    assert "not available" in await sessions.search_text("anything", "")


# --- the owner command ---

async def test_search_command_uses_the_routed_agents_scope(store):
    from micropaw.gateway import Gateway
    await say(store, "dm:owner", ("user", "kiwi main"))
    await say(store, "agent:coder:dm:owner", ("user", "kiwi coder"))
    gw = Gateway.__new__(Gateway)
    gw.store = store
    gw.goals = None
    coder = SimpleNamespace(conversations=SimpleNamespace(prefix="agent:coder:"), name="coder")
    gw._route = lambda platform, user_id, channel_id: coder
    out = await gw._owner_command("/search kiwi", "terminal", "u", None)
    assert "kiwi coder" in out or "[kiwi] coder" in out
    assert "main" not in out
    await asyncio.sleep(0)
