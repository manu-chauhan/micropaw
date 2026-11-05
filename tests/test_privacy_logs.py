"""Logs and the audit file carry no message text, and stay bounded."""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from micropaw import config
from micropaw.channels.media import Attachment, fetch_media, max_bytes
from micropaw.config import Config
from micropaw.core.agent import Agent, _summarize_args
from micropaw.gateway import Gateway, _RateLimiter
from micropaw.providers.base import AIProvider, AIResponse, MediaItem, ToolCall
from micropaw.tools import delegate
from micropaw.tools.base import Tool, ToolContext, ToolRegistry
from micropaw.utils import audit
from micropaw.utils import logger as logmod

CANARY = "CANARY-7f3a9c"


@pytest.fixture
def logs(tmp_path, monkeypatch):
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logmod._configured, logmod._log_content)
    root.handlers, logmod._configured = [], False
    path = tmp_path / "gateway.log"
    monkeypatch.setenv("MICROPAW_LOG_FILE", str(path))
    monkeypatch.setattr(logmod, "_in_docker", lambda: False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)

    def basic_config(**kw):
        for h in kw["handlers"]:
            root.addHandler(h)
        root.setLevel(kw["level"])

    monkeypatch.setattr(logging, "basicConfig", basic_config)
    logmod.setup_logging("DEBUG")
    yield path
    for h in root.handlers:
        h.close()
    root.handlers, root.level = saved[0], saved[1]
    logmod._configured, logmod._log_content = saved[2], saved[3]


class _Say(AIProvider):
    name, model = "mock", "m"

    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, tools=None):
        return self.responses.pop(0)


class _Note(Tool):
    name = "note"
    description = "x"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args, context):
        return f"saved {args['text']}"


def _agent(provider) -> Agent:
    registry = ToolRegistry()
    registry.register(_Note())
    store = MagicMock()
    for name in ("get_or_create_conversation", "save_messages", "compact_messages", "set_marked", "ingest_document"):
        setattr(store, name, AsyncMock())
    store.get_messages = AsyncMock(return_value=[])
    store.get_recent_memories = AsyncMock(return_value=[])
    store.search_documents = AsyncMock(return_value=[])
    store.get_summary = AsyncMock(return_value=None)
    store.is_marked = AsyncMock(return_value=False)
    return Agent(Config(), provider, registry, store)


async def test_a_turn_with_a_canary_in_message_arguments_and_reply_logs_none_of_it(logs, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit.jsonl")
    audit.configure("names")
    try:
        call = ToolCall(id="1", name="note", arguments={"text": f"{CANARY} in a tool argument"})
        agent = _agent(_Say([AIResponse(content="", tool_calls=[call], usage={}),
                             AIResponse(content=f"reply {CANARY}", tool_calls=[], usage={})]))
        gw = Gateway(Config(), MagicMock(), MagicMock(), [])
        gw.agent = agent
        gw.agents = {"main": agent}
        out = await gw._handle_message(f"please remember {CANARY}", "terminal-user", "terminal", None)
        assert CANARY in out
    finally:
        audit.configure("full")
    log_text = logs.read_text()
    assert "[note] text" in log_text  # the tool name and the argument names are there
    assert CANARY not in log_text
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert entries and all(set(e) == {"ts", "conversation", "tool", "outcome"} for e in entries)
    assert CANARY not in json.dumps(entries)


async def test_log_content_switch_lets_the_debug_lines_through(logs):
    gw = Gateway(Config(), MagicMock(), MagicMock(), [])
    gw.agent = MagicMock()
    gw.agent.handle_message = AsyncMock(return_value="fine")
    gw.agent.conversations.get_conversation_id = lambda *a: "c"
    await gw._handle_message(f"hello {CANARY}", "u", "terminal", None)
    assert CANARY not in logs.read_text()
    logmod.allow_content(True)
    await gw._handle_message(f"hello again {CANARY}", "u", "terminal", None)
    assert CANARY in logs.read_text()


def test_tool_log_line_names_arguments_only():
    assert _summarize_args({"query": CANARY, "path": "/home/me/x"}) == "query, path"
    assert _summarize_args({}) == "(no args)"


def test_web_search_never_logs_the_query():
    tree = ast.parse((Path(__file__).parents[1] / "src/micropaw/tools/web_search.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Name) and node.func.value.id == "logger":
            names = {n.id for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Name)}
            assert "query" not in names, ast.dump(node)


def test_refused_stranger_id_is_hashed_in_the_log(logs):
    gw = Gateway(Config(), MagicMock(), MagicMock(), [])
    reply = gw._access.check("telegram", "555123456")
    assert "555123456" in reply  # the stranger is told their id, so the owner can allow it
    assert "555123456" not in logs.read_text() and logmod.short_hash("555123456") in logs.read_text()


async def test_whatsapp_recipient_is_hashed_in_the_log(logs, monkeypatch):
    import httpx

    from micropaw.channels.whatsapp import WhatsAppChannel

    async def post(self, url, **kw):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    await WhatsAppChannel(token="t", phone_id="p", app_secret="s")._send("15551234567", "hi")
    assert "15551234567" not in logs.read_text()


def test_rag_logs_a_hash_of_the_source_not_its_path(logs, tmp_path):
    async def go():
        from micropaw.memory.store import MemoryStore
        s = MemoryStore(tmp_path / "d.db")
        await s.init()
        await s.ingest_document(f"/home/me/{CANARY}.pdf", "some text to store", "u")
        await s.close()
    asyncio.run(go())
    assert CANARY not in logs.read_text()


# --- the audit file ---

@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit.jsonl")
    audit.configure("full")
    return tmp_path / "audit.jsonl"


def test_audit_cuts_arguments_and_approval_and_drops_results(audit_file):
    audit.record("shell", {"command": "x" * 1000}, "ok", "c", approval="y" * 1000)
    line = json.loads(audit_file.read_text())
    assert len(line["args"]["command"]) <= audit._MAX_VALUE + 3 and len(line["approval"]) <= audit._MAX_VALUE + 3
    assert "result" not in line


def test_audit_names_mode_keeps_only_who_what_outcome(audit_file):
    audit.configure("names")
    try:
        audit.record("shell", {"command": CANARY}, "declined", "conv", approval=f"run {CANARY}")
    finally:
        audit.configure("full")
    assert set(json.loads(audit_file.read_text())) == {"ts", "conversation", "tool", "outcome"}


def test_audit_rotates_at_5_mb_and_keeps_three_old_files(audit_file):
    audit_file.write_bytes(b'{"a": 1}\n' * (6 * 1024 * 1024 // 9))
    assert audit_file.stat().st_size > audit._MAX_BYTES
    audit.record("shell", {}, "ok", "c")
    assert audit_file.stat().st_size < 1000 and audit_file.with_name("audit.jsonl.1").exists()
    for i in range(5):
        audit_file.write_bytes(b"x" * (audit._MAX_BYTES + 1))
        audit.record("shell", {}, "ok", "c")
    names = sorted(p.name for p in audit_file.parent.glob("audit.jsonl*"))
    assert names == ["audit.jsonl", "audit.jsonl.1", "audit.jsonl.2", "audit.jsonl.3"]


def test_audit_tail_spans_a_rotation(audit_file):
    for i in range(5):
        audit.record("note", {"i": i}, "ok", "c")
    audit_file.rename(audit_file.with_name("audit.jsonl.1"))  # as if it had just rotated
    audit.record("note", {"i": 5}, "ok", "c")
    assert [e["args"]["i"] for e in audit.tail(3)] == [3, 4, 5]
    assert [e["args"]["i"] for e in audit.tail(50)] == [0, 1, 2, 3, 4, 5]


def test_audit_tail_of_a_huge_file_is_cheap(audit_file):
    with open(audit_file, "wb") as f:
        f.seek(300 * 1024 * 1024)
        f.write(b'\n{"tool": "end", "outcome": "ok", "ts": "t", "conversation": "c"}\n')
    start = os.times()
    entries = audit.tail(5)
    assert entries[-1]["tool"] == "end" and os.times().elapsed - start.elapsed < 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_audit_file_is_made_private_even_if_it_was_not(audit_file):
    audit_file.write_text("")
    audit_file.chmod(0o644)
    audit.record("shell", {}, "ok", "c")
    assert stat.S_IMODE(audit_file.stat().st_mode) == 0o600


def test_config_has_the_storage_defaults():
    c = Config()
    assert (c.storage.retention_days, c.storage.secure_delete, c.storage.log_content) == (0, True, False)
    assert c.storage.audit == "full" and c.storage.auto_ingest_max_chunks == 20


# --- slow leaks ---

def test_rate_limiter_drops_idle_keys():
    rl = _RateLimiter(20, 5)
    for i in range(300):
        rl.check(f"user{i}")
    assert len(rl._windows) <= 300
    import time
    for w in rl._windows.values():
        w[:] = [time.monotonic() - 120]
    rl.check("fresh")
    assert set(rl._windows) == {"fresh"}


def test_rate_limiter_refuses_and_remembers_after_a_burst():
    rl = _RateLimiter(100, 2)
    assert rl.check("a") and rl.check("a") and not rl.check("a")


async def test_every_webchat_connection_shares_one_limiter_key():
    gw = Gateway(Config(), MagicMock(), MagicMock(), [])
    gw.agent = MagicMock()
    gw._rate_limiter = _RateLimiter(1000, 1000)
    for i in range(50):
        assert gw._validate("hi", f"conn{i}", "webchat") is None
    assert list(gw._rate_limiter._windows) == ["webchat"]


async def test_background_sub_agents_run_two_at_a_time(monkeypatch):
    running, peak = 0, 0

    class Slow:
        def __init__(self, *a, **k): ...

        async def run(self, task):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return "done"

    monkeypatch.setattr(delegate, "SubAgent", Slow)
    delegate.set_delegate_deps(MagicMock(), delegate.MessageBus(), ToolRegistry())
    tool = delegate.DelegateTool()
    ctx = ToolContext(conversation_id="c", user_id="u", platform="terminal", chat_id="terminal:1")
    for i in range(5):
        await tool.execute({"task": f"t{i}", "mode": "background"}, ctx)
    assert len(delegate._tasks) == 5  # references kept
    await asyncio.gather(*delegate._tasks)
    assert peak == 2 and not delegate._tasks


async def test_background_sub_agents_are_cancelled_at_stop(monkeypatch):
    class Forever:
        def __init__(self, *a, **k): ...

        async def run(self, task):
            await asyncio.sleep(3600)

    monkeypatch.setattr(delegate, "SubAgent", Forever)
    delegate.set_delegate_deps(MagicMock(), delegate.MessageBus(), ToolRegistry())
    ctx = ToolContext(conversation_id="c", user_id="u", platform="terminal", chat_id="terminal:1")
    await delegate.DelegateTool().execute({"task": "t", "mode": "background"}, ctx)
    await asyncio.sleep(0)
    await asyncio.wait_for(delegate.cancel_background(), 2)
    assert not delegate._tasks


async def test_agent_keeps_auto_ingest_tasks_and_cancels_them(monkeypatch):
    agent = _agent(_Say([]))
    gate = asyncio.Event()

    async def slow(source, content, user_id, local=False):
        await gate.wait()

    monkeypatch.setattr(agent, "_auto_ingest", slow)
    from micropaw.providers.base import Message
    call = ToolCall(id="1", name="file_read", arguments={"path": "/x"})

    class Reader(Tool):
        name = "file_read"
        description = "x"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, args, context):
            return "z" * 600

    agent.registry.register(Reader())
    ctx = ToolContext(conversation_id="c", user_id="u", platform="terminal")
    await agent._execute_tools([Message(role="user", content="hi")],
                               AIResponse(content="", tool_calls=[call], usage={}), ctx, "c", {}, [])
    assert len(agent._bg) == 1
    await agent.stop_background()
    assert not agent._bg


# --- media ---

def test_media_limit_is_8_mb_under_4_gb_of_ram(monkeypatch):
    monkeypatch.setattr("micropaw.providers.ollama.ram_bytes", lambda: 2 * 1024 ** 3)
    assert max_bytes() == 8 * 1024 * 1024
    monkeypatch.setattr("micropaw.providers.ollama.ram_bytes", lambda: 16 * 1024 ** 3)
    assert max_bytes() == 20 * 1024 * 1024
    monkeypatch.setattr("micropaw.providers.ollama.ram_bytes", lambda: None)
    assert max_bytes() == 20 * 1024 * 1024


async def test_a_10_mb_photo_is_refused_on_a_small_board(monkeypatch):
    monkeypatch.setattr("micropaw.providers.ollama.ram_bytes", lambda: 2 * 1024 ** 3)
    big = Attachment("https://x.example/a.png", "image/png", 10 * 1024 * 1024, "a.png", False)
    media, notes = await fetch_media([big])
    assert media == [] and "too large" in notes[0] and "8 MB" in notes[0]


def test_base64_is_made_once_and_the_bytes_are_kept(monkeypatch):
    import base64
    item = MediaItem(type="image", data=b"abc", mime_type="image/png")
    calls = []
    real = base64.b64encode
    monkeypatch.setattr(base64, "b64encode", lambda d: calls.append(1) or real(d))
    assert item.b64() == "YWJj" and item.b64() == "YWJj"
    assert len(calls) == 1 and item.data == b"abc"


def test_providers_share_the_cached_encoding():
    from micropaw.providers.claude import ClaudeProvider
    item = MediaItem(type="image", data=b"abc", mime_type="image/png")
    from micropaw.providers.base import Message
    p = ClaudeProvider(api_key="k", model="m")
    p._build_body([Message(role="user", content="x", media=[item])], None)
    p._build_body([Message(role="user", content="x", media=[item])], None)
    assert item._b64 == "YWJj"


_ = config
