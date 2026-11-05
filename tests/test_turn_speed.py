"""Turn speed: gathered reads, compaction after the reply, parallel read-only tools, one commit, big outputs, locks."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from micropaw import config as micropaw_config
from micropaw.config import Config
from micropaw.core.agent import Agent
from micropaw.core.context import ContextManager
from micropaw.memory.ledger import Ledger
from micropaw.memory.store import MemoryStore
from micropaw.providers.base import AIProvider, AIResponse, Message, ToolCall
from micropaw.providers.metered import BudgetExceeded
from micropaw.tools import tool_output
from micropaw.tools.base import Tool, ToolContext, ToolRegistry


class Scripted(AIProvider):
    name = "mock"
    model = "m"

    def __init__(self, responses=None, log=None):
        self.responses = list(responses or [])
        self.log = log if log is not None else []

    async def chat(self, messages, tools=None):
        self.log.append("main")
        if self.responses:
            return self.responses.pop(0)
        return AIResponse(content="done", tool_calls=[], usage={"input": 5, "output": 2})


def tool_turn(*calls: ToolCall) -> list[AIResponse]:
    return [AIResponse(content="", tool_calls=list(calls), usage={"input": 1, "output": 1}),
            AIResponse(content="all done", tool_calls=[], usage={"input": 2, "output": 2})]


class Timed(Tool):
    """Records (name, start, end) in a shared list. Sleeps `delays[arg]` seconds."""

    def __init__(self, name, events, delay=0.2, ask=None):
        self.name = name
        self.description = name
        self.parameters = {"type": "object", "properties": {}}
        self.events = events
        self.delay = delay
        self.ask = ask or (lambda args: None)

    def needs_approval(self, args, context):
        return self.ask(args)

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        start = time.monotonic()
        await asyncio.sleep(args.get("delay", self.delay))
        self.events.append((args.get("id", self.name), start, time.monotonic()))
        return f"out {args.get('id', self.name)}"


@pytest.fixture
async def store(tmp_path):
    st = MemoryStore(tmp_path / "data.db")
    await st.init()
    yield st
    await st.close()


def agent_with(store, provider, tools=(), **cfg) -> Agent:
    config = Config()
    for k, v in cfg.items():
        setattr(config.context, k, v)
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    return Agent(config, provider, registry, store)


async def settle(agent: Agent) -> None:
    while agent._compact_tasks:
        await asyncio.gather(*agent._compact_tasks.values())
        await asyncio.sleep(0)


class TestPrepareGather:
    async def test_independent_reads_overlap(self):
        def slow(value):
            async def f(*a, **k):
                await asyncio.sleep(0.2)
                return value
            return AsyncMock(side_effect=f)

        store = MagicMock()
        store.get_or_create_conversation = AsyncMock()
        store.get_messages = slow([])
        store.is_marked = slow(False)
        store.get_recent_memories = slow([])
        store.search_documents = slow([])
        store.get_summary = slow("earlier talk")
        agent = agent_with(store, Scripted())
        t0 = time.monotonic()
        conv, messages, marked = await agent._prepare("hi", "u", "terminal")
        assert time.monotonic() - t0 < 0.5  # five 0.2 s reads one after another would take 1.0 s
        assert conv == "dm:owner" and not marked
        assert "earlier talk" in messages[-1].content and messages[-1].content.endswith("hi")

    async def test_group_chat_skips_memories_and_documents(self):
        store = MagicMock()
        store.get_or_create_conversation = AsyncMock()
        store.get_messages = AsyncMock(return_value=[])
        store.is_marked = AsyncMock(return_value=False)
        store.get_recent_memories = AsyncMock(return_value=[{"key": "k", "value": "v", "marked": False}])
        store.search_documents = AsyncMock(return_value=[])
        store.get_summary = AsyncMock(return_value=None)
        await agent_with(store, Scripted())._prepare("hi", "u", "discord", "chan1")
        store.get_recent_memories.assert_not_called()
        store.search_documents.assert_not_called()


class TestCompaction:
    async def _seed(self, store, n=40, conv="dm:owner"):
        await store.get_or_create_conversation(conv, "dm", "u")
        for i in range(n):
            await store.save_message(conv, Message(role="user" if i % 2 == 0 else "assistant", content=f"m{i}"))

    async def test_runs_after_the_reply_not_before(self, store):
        await self._seed(store)
        log: list[str] = []

        class Summarizer(AIProvider):
            name = "fast"
            model = "f"

            async def chat(self, messages, tools=None):
                log.append("summary-start")
                await asyncio.sleep(0.3)
                log.append("summary-end")
                return AIResponse(content="short summary", tool_calls=[], usage={})

        agent = agent_with(store, Scripted(log=log))
        agent.fast_provider = Summarizer()
        t0 = time.monotonic()
        assert await agent.handle_message("hello", "u", "terminal") == "done"
        assert time.monotonic() - t0 < 0.25
        assert "summary-end" not in log and log[0] == "main"
        await settle(agent)
        assert log == ["main", "summary-start", "summary-end"]
        assert await store.get_summary("dm:owner") == "short summary"
        assert len(await store.get_messages("dm:owner", 100)) < 20

    async def test_previous_summary_is_folded_in(self, store):
        await self._seed(store)
        await store._set_meta("summary:dm:owner", "user likes tea")
        seen: list[str] = []

        class Summarizer(AIProvider):
            name = "fast"
            model = "f"

            async def chat(self, messages, tools=None):
                seen.append(messages[0].content + "\n" + messages[1].content)
                return AIResponse(content="tea and more", tool_calls=[], usage={})

        agent = agent_with(store, Scripted())
        agent.fast_provider = Summarizer()
        await agent.handle_message("hello", "u", "terminal")
        await settle(agent)
        assert "fold it into the new summary" in seen[0]
        assert "Earlier summary: user likes tea" in seen[0]
        assert await store.get_summary("dm:owner") == "tea and more"

    async def test_messages_saved_during_compaction_survive(self, store):
        await self._seed(store)
        gate = asyncio.Event()

        class Summarizer(AIProvider):
            name = "fast"
            model = "f"

            async def chat(self, messages, tools=None):
                await gate.wait()
                return AIResponse(content="s", tool_calls=[], usage={})

        agent = agent_with(store, Scripted())
        agent.fast_provider = Summarizer()
        await agent.handle_message("one", "u", "terminal")
        await store.save_message("dm:owner", Message(role="user", content="arrived meanwhile"))
        gate.set()
        await settle(agent)
        contents = [m.content for m in await store.get_messages("dm:owner", 100)]
        assert "arrived meanwhile" in contents and "one" not in contents[:1]

    async def test_summary_of_marked_chat_keeps_the_mark(self, store):
        await self._seed(store)
        await store.set_marked("dm:owner")
        await store.compact_messages("dm:owner", "summary", 10, marked=await store.is_marked("dm:owner"))
        await store.db.execute("UPDATE conversations SET marked = 0")
        assert await store.is_marked("dm:owner")
        await self._seed(store, 2, "clean")
        await store.compact_messages("clean", "summary", 1)
        assert not await store.is_marked("clean")

    def test_token_trigger_follows_the_window(self):
        cm = ContextManager(compaction_threshold=30)
        few_big = [Message(role="system", content="s" * 400)] + [
            Message(role="user" if i % 2 == 0 else "assistant", content="x" * 4000) for i in range(6)]
        assert cm.needs_compaction(few_big, window_tokens=8192)       # about 6100 tokens, over 60% of 8192
        assert not cm.needs_compaction(few_big, window_tokens=100_000)
        assert not cm.needs_compaction(few_big, window_tokens=0)
        many_small = [Message(role="user", content="hi")] * 31
        assert cm.needs_compaction(many_small)

    def test_keep_count_shrinks_for_big_tails(self):
        big = [Message(role="user", content="x" * 4000)] * 12
        assert ContextManager.keep_count(big, 0) == 10
        assert ContextManager.keep_count(big, 8192) < 10

    def test_window_default_local_vs_cloud(self):
        local = MagicMock()
        local.name = "ollama"
        local.host = "http://localhost:11434"
        local.num_ctx = 8192
        cloud = MagicMock()
        cloud.name = "claude"
        cloud.host = ""
        cloud._api_url = ""
        cloud.base_url = ""
        assert agent_with(MagicMock(), local)._window_tokens() == 8192
        assert agent_with(MagicMock(), cloud)._window_tokens() == 100_000
        assert agent_with(MagicMock(), cloud, window_tokens=32_000)._window_tokens() == 32_000
        lm = MagicMock()
        lm.name = "openai"
        lm.host = ""
        lm._api_url = "http://192.168.1.9:1234/v1/chat/completions"
        assert agent_with(MagicMock(), lm)._window_tokens() == 8192

    async def test_big_history_triggers_background_compaction_on_local_model(self, store):
        await store.get_or_create_conversation("dm:owner", "dm", "u")
        for i in range(8):
            await store.save_message("dm:owner", Message(role="user" if i % 2 == 0 else "assistant",
                                                         content="x" * 4000))
        local = Scripted()
        local.name = "ollama"
        agent = agent_with(store, local)
        calls: list[int] = []

        class Summarizer(AIProvider):
            name = "fast"
            model = "f"

            async def chat(self, messages, tools=None):
                calls.append(1)
                return AIResponse(content="s", tool_calls=[], usage={})

        agent.fast_provider = Summarizer()
        await agent.handle_message("hello", "u", "terminal")
        await settle(agent)
        assert calls == [1]


class TestParallelTools:
    def _run(self, store, calls, tools, approver=None):
        agent = agent_with(store, Scripted(tool_turn(*calls)), tools)
        agent.approver = approver
        return agent

    async def test_read_only_calls_overlap_and_keep_order(self, store):
        events: list = []
        calls = [ToolCall(id=c, name="web_search", arguments={"id": c, "delay": d})
                 for c, d in (("a", 0.3), ("b", 0.2), ("c", 0.1))]
        agent = self._run(store, calls, [Timed("web_search", events)])
        t0 = time.monotonic()
        assert await agent.handle_message("go", "u", "terminal") == "all done"
        assert time.monotonic() - t0 < 0.5  # serial would be 0.6 s
        assert [e[0] for e in events] == ["c", "b", "a"]  # finished in reverse order
        saved = await store.get_messages("dm:owner", 50)
        tool_msgs = [m for m in saved if m.role == "tool"]
        assert [(m.tool_call_id, m.content) for m in tool_msgs] == [("a", "out a"), ("b", "out b"), ("c", "out c")]

    async def test_identical_lookups_run_once(self, store):
        events: list = []
        calls = [ToolCall(id=str(i), name="web_search", arguments={"id": "same"}) for i in range(3)]
        await self._run(store, calls, [Timed("web_search", events, delay=0.05)]).handle_message("go", "u", "terminal")
        assert len(events) == 1

    async def test_writes_and_asks_run_alone_in_order(self, store):
        events: list = []
        asked: list = []

        async def approver(platform, chat_id, tool, text, marked):
            start = time.monotonic()
            await asyncio.sleep(0.1)
            asked.append(("ask", start, time.monotonic()))
            return True

        def ask_secret(args):
            return "read a secret" if args.get("id") == "ask" else None

        reader = Timed("file_read", events, delay=0.1, ask=ask_secret)
        writer = Timed("file_write", events, delay=0.1)
        order = ["r1", "r2", "ask", "w", "r3", "r4"]
        calls = [ToolCall(id=i, name="file_write" if i == "w" else "file_read", arguments={"id": i}) for i in order]
        agent = self._run(store, calls, [reader, writer], approver)
        await agent.handle_message("go", "u", "terminal")
        spans = sorted(events + asked, key=lambda e: e[1])
        # the asking read and the write never overlap anything
        for name in ("ask", "w"):
            mine = next(e for e in spans if e[0] == name)
            assert all(o is mine or o[2] <= mine[1] + 1e-3 or o[1] >= mine[2] - 1e-3 for o in spans)
        # r1/r2 overlap each other, r3/r4 overlap each other
        r = {e[0]: e for e in events}
        assert r["r2"][1] < r["r1"][2] and r["r4"][1] < r["r3"][2]
        # a write after reads: the reads before it have finished, the reads after start later
        assert max(r["r1"][2], r["r2"][2]) <= r["w"][1] + 1e-3 and min(r["r3"][1], r["r4"][1]) >= r["w"][2] - 1e-3
        assert len(asked) == 1

    async def test_memory_reads_parallel_but_save_serial(self, store):
        events: list = []

        class Mem(Timed):
            pass

        mem = Mem("memory", events, delay=0.15)
        calls = [
            ToolCall(id="s1", name="memory", arguments={"action": "search", "id": "s1"}),
            ToolCall(id="s2", name="memory", arguments={"action": "list", "id": "s2"}),
            ToolCall(id="w1", name="memory", arguments={"action": "save", "id": "w1"}),
            ToolCall(id="w2", name="memory", arguments={"action": "delete", "id": "w2"}),
        ]
        await self._run(store, calls, [mem]).handle_message("go", "u", "terminal")
        e = {x[0]: x for x in events}
        assert e["s2"][1] < e["s1"][2]            # reads overlapped
        assert e["w1"][1] >= e["s1"][2] - 1e-3    # the save waited for them
        assert e["w2"][1] >= e["w1"][2] - 1e-3    # and writes ran one after another

    async def test_unknown_tool_still_answered(self, store):
        calls = [ToolCall(id="x", name="nope", arguments={}),
                 ToolCall(id="y", name="web_search", arguments={"id": "y"})]
        agent = self._run(store, calls, [Timed("web_search", [], delay=0)])
        await agent.handle_message("go", "u", "terminal")
        tool_msgs = [m for m in await store.get_messages("dm:owner", 50) if m.role == "tool"]
        assert [m.content for m in tool_msgs] == ["Unknown tool: nope", "out y"]


class TestOneCommitPerTurn:
    async def _count(self, store, agent, text="hi"):
        commits = 0
        real = store.db.commit

        async def counting():
            nonlocal commits
            commits += 1
            await real()

        store.db.commit = counting
        try:
            await agent.handle_message(text, "u", "terminal")
        finally:
            store.db.commit = real
        return commits

    async def test_plain_turn_one_commit_new_and_existing_chat(self, store):
        agent = agent_with(store, Scripted())
        assert await self._count(store, agent) == 2  # the first message also creates the conversation row
        assert await self._count(store, agent) == 1  # after that: one commit per turn
        async with store.db.execute("SELECT COUNT(*) FROM conversations") as cur:
            assert (await cur.fetchone())[0] == 1
        assert [m.role for m in await store.get_messages("dm:owner", 10)] == ["user", "assistant"] * 2

    async def test_no_write_transaction_open_while_the_model_thinks(self, store):
        await store.get_or_create_conversation("dm:owner", "dm", "u")
        seen: list[bool] = []

        class Watching(Scripted):
            async def chat(self, messages, tools=None):
                seen.append(store.db.in_transaction)
                return await super().chat(messages, tools)

        await agent_with(store, Watching()).handle_message("hi", "u", "terminal")
        assert seen == [False]

    async def test_turn_with_tools_still_one_commit(self, store):
        calls = [ToolCall(id="a", name="web_search", arguments={"id": "a"}),
                 ToolCall(id="b", name="web_search", arguments={"id": "b"})]
        agent = agent_with(store, Scripted(tool_turn(*calls)), [Timed("web_search", [], delay=0)])
        await store.get_or_create_conversation("dm:owner", "dm", "u")
        assert await self._count(store, agent) == 1
        roles = [m.role for m in await store.get_messages("dm:owner", 10)]
        assert roles == ["user", "assistant", "tool", "tool", "assistant"]

    async def test_pragmas_and_indexes(self, store):
        async with store.db.execute("PRAGMA synchronous") as cur:
            assert (await cur.fetchone())[0] == 1  # NORMAL
        async with store.db.execute("PRAGMA busy_timeout") as cur:
            assert (await cur.fetchone())[0] == 5000
        queries = {
            "SELECT role FROM messages WHERE conversation_id = 'c' ORDER BY id DESC LIMIT 40": "idx_messages",
            "SELECT key FROM memories WHERE user_id = '_global' ORDER BY updated_at DESC LIMIT 10":
                "idx_memories_user_updated",
            "SELECT content FROM document_chunks WHERE user_id = '_global' AND LOWER(content) LIKE 'x' "
            "ORDER BY created_at DESC LIMIT 5": "idx_chunks_user_created",
        }
        for sql, index in queries.items():
            async with store.db.execute("EXPLAIN QUERY PLAN " + sql) as cur:
                plan = " ".join(str(r[3]) for r in await cur.fetchall())
            assert index in plan, (sql, plan)
            assert "TEMP B-TREE" not in plan, (sql, plan)


@pytest.fixture
def outputs_home(tmp_path, monkeypatch):
    monkeypatch.setattr(micropaw_config, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr(tool_output, "_last_purge", 0.0)
    return tmp_path / "home" / "outputs"


class TestLargeOutputs:
    class Big(Tool):
        name = "file_read"
        description = "big"
        parameters: dict[str, Any] = {"type": "object", "properties": {}}

        async def execute(self, args, context):
            return "HEAD" + "a" * 5996 + "m" * 10000 + "b" * 1996 + "TAIL"

    async def test_saved_outside_workspace_with_mode_0600(self, store, outputs_home, tmp_path):
        agent = agent_with(store, Scripted(tool_turn(ToolCall(id="a", name="file_read", arguments={}))), [self.Big()])
        agent.config.permissions.workspace = str(tmp_path / "workspace")
        await agent.handle_message("go", "u", "terminal")
        tool_msg = next(m for m in await store.get_messages("dm:owner", 10) if m.role == "tool")
        assert len(tool_msg.content) < 8600
        assert tool_msg.content.startswith("HEAD") and tool_msg.content.endswith("TAIL")
        assert "output 1" in tool_msg.content and "tool_output" in tool_msg.content
        files = list(outputs_home.glob("*/1.txt"))
        assert len(files) == 1 and len(files[0].read_text()) == 18000
        assert tmp_path / "workspace" not in files[0].parents
        if os.name != "nt":
            assert files[0].stat().st_mode & 0o777 == 0o600

    async def test_short_output_untouched(self, outputs_home):
        assert await tool_output.shorten("c", "x" * 12_000) == "x" * 12_000
        assert not outputs_home.exists()

    async def test_tool_output_pages_and_marks(self, outputs_home):
        text = "".join(chr(65 + i % 26) for i in range(15_000))
        await tool_output.shorten("dm:owner", text)
        tool = tool_output.ToolOutputTool()
        assert tool.marks is True
        ctx = ToolContext(conversation_id="dm:owner", user_id="u", platform="terminal")
        page2 = await tool.execute({"n": 1, "page": 2}, ctx)
        assert page2.startswith("Output 1, page 2 of 3:\n") and page2.endswith(text[6000:12000])
        assert "page 1 of 3" in await tool.execute({}, ctx)
        assert "has 3 pages" in await tool.execute({"page": 9}, ctx)
        assert "No saved output 7" in await tool.execute({"n": 7}, ctx)
        other = ToolContext(conversation_id="someone-else", user_id="u", platform="terminal")
        assert "No saved outputs" in await tool.execute({"n": 1}, other)
        assert "must be whole numbers" in await tool.execute({"n": "../../etc/passwd"}, ctx)

    async def test_old_files_deleted_at_first_save(self, outputs_home):
        old = outputs_home / "gone"
        old.mkdir(parents=True)
        (old / "1.txt").write_text("x")
        os.utime(old / "1.txt", (time.time() - 8 * 86400,) * 2)
        fresh = outputs_home / "kept"
        fresh.mkdir()
        (fresh / "1.txt").write_text("x")
        await tool_output.shorten("c", "y" * 13_000)
        assert not old.exists() and (fresh / "1.txt").exists()

    def test_registered_in_the_default_registry(self):
        from micropaw.cli import _create_registry
        assert "tool_output" in _create_registry(Config()).names


class TestBudgetInTheAgent:
    async def test_budget_exceeded_becomes_a_plain_reply(self, store):
        class Refuses(Scripted):
            async def chat(self, messages, tools=None):
                raise BudgetExceeded("Daily budget of $2.00 reached; it resets at midnight. "
                                     "Change budget.daily_usd to raise it.")

        reply = await agent_with(store, Refuses()).handle_message("hi", "u", "terminal")
        assert reply.startswith("Daily budget of $2.00 reached; it resets at midnight.")

    async def test_warning_note_added_once(self, store):
        import aiosqlite
        db = await aiosqlite.connect(":memory:")
        try:
            ledger = Ledger(db)
            await ledger.init()
            agent = agent_with(store, Scripted())
            agent.ledger = ledger
            ledger.warn_once("", "Heads up: $1.70 of the $2.00 daily budget is used. It resets at midnight.")
            first = await agent.handle_message("hi", "u", "terminal")
            assert first == "done\n\nHeads up: $1.70 of the $2.00 daily budget is used. It resets at midnight."
            assert await agent.handle_message("again", "u", "terminal") == "done"
            assert "Heads up" not in [m.content for m in await store.get_messages("dm:owner", 10)][1]
        finally:
            await db.close()

    async def test_usage_is_per_turn_and_per_conversation(self, store):
        agent = agent_with(store, Scripted([
            AIResponse(content="a", tool_calls=[], usage={"input": 10, "output": 5}),
            AIResponse(content="b", tool_calls=[], usage={"input": 20, "output": 7}),
        ]))
        await agent.handle_message("one", "u", "terminal")
        await agent.handle_message("two", "u", "discord", "chan")
        assert agent.turn_usage["dm:owner"] == {"input": 10, "output": 5}
        assert agent.turn_usage["discord:chan"] == {"input": 20, "output": 7}
        assert agent.session_usage == {"input": 30, "output": 12}


class TestChatLock:
    def _gateway(self):
        from micropaw.gateway import Gateway
        gw = Gateway(Config(), MagicMock(), MagicMock(), [])
        gw.agent = MagicMock()
        gw.agent.conversations.get_conversation_id = lambda platform, user, channel: f"{platform}:{channel or 'dm'}"
        return gw

    async def test_same_chat_serial_different_chats_concurrent(self):
        gw = self._gateway()
        running = {"now": 0, "max": 0}
        per_chat: dict[str, int] = {}

        async def slow(text, user_id, platform, channel_id, media=None):
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            per_chat[channel_id] = per_chat.get(channel_id, 0) + 1
            assert per_chat[channel_id] == 1, "two turns in one chat at once"
            await asyncio.sleep(0.15)
            per_chat[channel_id] -= 1
            running["now"] -= 1
            return "ok"

        gw.agent.handle_message = slow
        t0 = time.monotonic()
        await asyncio.gather(*(gw._handle_message(f"m{i}", "u", "terminal", "A") for i in range(3)))
        assert time.monotonic() - t0 >= 0.45
        assert running["max"] == 1
        gw._rate_limiter = type(gw._rate_limiter)(100, 100)
        t0 = time.monotonic()
        await asyncio.gather(*(gw._handle_message("m", "u", "terminal", c) for c in "BCD"))
        assert time.monotonic() - t0 < 0.3
        assert running["max"] == 3

    async def test_stream_path_takes_the_same_lock(self):
        gw = self._gateway()
        order: list[str] = []

        async def slow(text, user_id, platform, channel_id, on_chunk=None, media=None):
            order.append(f"start {text}")
            await asyncio.sleep(0.05)
            order.append(f"end {text}")
            return "ok"

        gw.agent.handle_message_stream = slow
        await asyncio.gather(gw._handle_message_stream("1", "u", "terminal", "A"),
                             gw._handle_message_stream("2", "u", "terminal", "A"))
        assert order == ["start 1", "end 1", "start 2", "end 2"]

    async def test_approval_answer_is_not_blocked_by_the_lock(self):
        gw = self._gateway()
        release = asyncio.Event()

        async def waiting_for_owner(text, user_id, platform, channel_id, media=None):
            future = asyncio.get_running_loop().create_future()
            gw._approvals._pending["ab12"] = (f"{platform}:{channel_id}", future, "")
            await release.wait()
            return f"approved={future.result()}"

        gw.agent.handle_message = waiting_for_owner
        turn = asyncio.create_task(gw._handle_message("do it", "u", "terminal", "A"))
        await asyncio.sleep(0.05)
        answer = await asyncio.wait_for(gw._handle_message("yes ab12", "u", "terminal", "A"), timeout=0.5)
        assert answer == "Approved."
        release.set()
        assert await turn == "approved=True"


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits are not meaningful on Windows")
def test_output_dir_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(micropaw_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(tool_output, "_last_purge", 0.0)
    tool_output._save("c", "x")
    assert (tmp_path / "outputs" / "c").stat().st_mode & 0o777 == 0o700
