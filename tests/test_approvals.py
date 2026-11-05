"""Tests for approvals before side effects and the outside-content mark."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from micropaw.channels.base import approval_hint
from micropaw.gateway import _Approvals
from micropaw.memory.store import MemoryStore
from micropaw.tools.base import Tool, ToolContext, run_tool


class _SideEffect(Tool):
    name = "side_effect"
    description = "Does something"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.ran = 0

    def needs_approval(self, args, context):
        return "do the thing"

    async def execute(self, args, context):
        self.ran += 1
        return "done"


class _Reader(Tool):
    name = "reader"
    description = "Reads a web page"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    marks = True

    async def execute(self, args, context):
        return "page text"


def _ctx(**kw) -> ToolContext:
    return ToolContext(conversation_id="c", user_id="u", platform="telegram", chat_id="telegram:1", **kw)


class TestRunTool:
    @pytest.mark.asyncio
    async def test_refused_when_nobody_can_be_asked(self):
        tool = _SideEffect()
        result = await run_tool(tool, {}, _ctx(approve=None))
        assert result.startswith("Not done") and tool.ran == 0

    @pytest.mark.asyncio
    async def test_declined(self):
        tool = _SideEffect()
        result = await run_tool(tool, {}, _ctx(approve=AsyncMock(return_value=False)))
        assert "declined" in result and tool.ran == 0

    @pytest.mark.asyncio
    async def test_approved_runs(self):
        tool, approve = _SideEffect(), AsyncMock(return_value=True)
        assert await run_tool(tool, {}, _ctx(approve=approve)) == "done"
        approve.assert_awaited_once_with("side_effect", "do the thing")

    @pytest.mark.asyncio
    async def test_reading_outside_content_marks(self):
        ctx = _ctx()
        await run_tool(_Reader(), {}, ctx)
        assert ctx.marked is True


class TestApprovals:
    def _approvals(self, tmp_path) -> _Approvals:
        return _Approvals(tmp_path / "approved.json")

    async def _ask_and_answer(self, approvals, reply_word: str, chat="telegram:1", marked=False):
        sent: list[str] = []

        async def send(msg: str, ask_id: str, always: bool) -> None:
            sent.append(f"{msg}\n\n{approval_hint(ask_id, always)}")

        task = asyncio.create_task(approvals.ask(send, "telegram:1", "shell", "Run: ls", marked))
        await asyncio.sleep(0)
        ask_id = sent[0].split("[", 1)[1][:4]
        reply = approvals.answer(chat, f"{reply_word} {ask_id}")
        if chat != "telegram:1":
            task.cancel()
            return None, reply, sent
        return await task, reply, sent

    @pytest.mark.asyncio
    async def test_yes_and_no(self, tmp_path):
        a = self._approvals(tmp_path)
        assert (await self._ask_and_answer(a, "yes"))[0] is True
        assert (await self._ask_and_answer(a, "no"))[0] is False

    @pytest.mark.asyncio
    async def test_answer_from_another_chat_ignored(self, tmp_path):
        _, reply, _ = await self._ask_and_answer(self._approvals(tmp_path), "yes", chat="telegram:2")
        assert "No pending approval" in reply

    def test_plain_yes_is_not_an_answer(self, tmp_path):
        assert self._approvals(tmp_path).answer("telegram:1", "yes") is None

    @pytest.mark.asyncio
    async def test_always_saves_exact_rule(self, tmp_path):
        a = self._approvals(tmp_path)
        ok, _, _ = await self._ask_and_answer(a, "always")
        assert ok is True
        sent: list[str] = []
        send = AsyncMock(side_effect=lambda *args: sent.append(args))
        assert await a.ask(send, "telegram:1", "shell", "Run: ls", False)
        assert sent == []  # remembered, no new ask
        reloaded = _Approvals(tmp_path / "approved.json")
        assert await reloaded.ask(AsyncMock(), "telegram:1", "shell", "Run: ls", False)

    @pytest.mark.asyncio
    async def test_rules_ignored_and_always_refused_after_outside_content(self, tmp_path):
        a = self._approvals(tmp_path)
        await self._ask_and_answer(a, "always")  # saved for unmarked conversations
        sent: list[str] = []

        async def send(msg: str, ask_id: str, always: bool) -> None:
            sent.append(f"{msg}\n\n{approval_hint(ask_id, always)}")

        task = asyncio.create_task(a.ask(send, "telegram:1", "shell", "Run: ls", True))
        await asyncio.sleep(0)
        assert sent, "the saved rule must not skip the ask once outside content was read"
        ask_id = sent[0].split("[", 1)[1][:4]
        assert f"always {ask_id}" not in sent[0]
        assert "isn't offered" in a.answer("telegram:1", f"always {ask_id}")
        a.answer("telegram:1", f"no {ask_id}")
        assert await task is False

    @pytest.mark.asyncio
    async def test_times_out_as_no(self, tmp_path):
        a = self._approvals(tmp_path)
        a.TIMEOUT = 0.01
        assert await a.ask(AsyncMock(), "telegram:1", "shell", "Run: ls", False) is False


class TestToolRules:
    def test_shell_asks_unless_disabled_and_unmarked(self, monkeypatch):
        from micropaw.tools import sandbox
        from micropaw.tools.sandbox import Sandbox
        from micropaw.tools.shell import ShellTool
        monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("bwrap"))  # a host without one always asks
        tool = ShellTool()
        assert "ls -la" in tool.needs_approval({"command": "ls -la"}, _ctx(allow_shell=True))
        assert tool.needs_approval({"command": "ls"}, _ctx(allow_shell=True, confirm_dangerous=False)) is None
        assert tool.needs_approval({"command": "ls"}, _ctx(allow_shell=True, confirm_dangerous=False, marked=True))
        assert "Careful" in tool.needs_approval({"command": "rm notes.txt"}, _ctx(allow_shell=True))

    def test_http_request_rules(self):
        from micropaw.tools.http_request import HttpRequestTool
        tool = HttpRequestTool()
        assert tool.needs_approval({"url": "https://example.com"}, _ctx()) is None
        assert "POST" in tool.needs_approval({"url": "https://example.com", "method": "POST"}, _ctx())
        assert tool.needs_approval({"url": "https://example.com"}, _ctx(marked=True))

    def test_file_write_free_inside_workspace_only(self, tmp_path):
        from micropaw.tools.file_write import FileWriteTool
        tool, ws = FileWriteTool(), str(tmp_path / "ws")
        ctx = _ctx(allow_file_write=True, workspace=ws)
        assert tool.needs_approval({"path": "notes.md", "content": "x"}, ctx) is None
        assert tool.needs_approval({"path": str(tmp_path / "elsewhere.md"), "content": "x"}, ctx)
        assert tool.needs_approval({"path": "Makefile", "content": "x"}, ctx)

    @pytest.mark.asyncio
    async def test_relative_write_lands_in_workspace(self, tmp_path):
        from micropaw.tools.file_write import FileWriteTool
        ws = tmp_path / "ws"
        await FileWriteTool().execute({"path": "a/notes.md", "content": "hi"},
                                      _ctx(allow_file_write=True, workspace=str(ws)))
        assert (ws / "a" / "notes.md").read_text() == "hi"

    def test_email_send_asks_with_recipient_first(self):
        from micropaw.tools.email_tool import EmailTool
        ask = EmailTool().needs_approval({"action": "send", "to": "a@b.c", "subject": "s", "body": "b"}, _ctx())
        assert ask.splitlines()[1] == "To: a@b.c"
        assert EmailTool().needs_approval({"action": "inbox"}, _ctx()) is None


class TestStoredMark:
    @pytest.mark.asyncio
    async def test_conversation_and_memory_marks(self, tmp_path):
        store = MemoryStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.get_or_create_conversation("dm:owner", "dm", "u")
            assert await store.is_marked("dm:owner") is False
            await store.set_marked("dm:owner")
            assert await store.is_marked("dm:owner") is True
            await store.save_memory("tip", "from a web page", marked=True)
            await store.save_memory("name", "Manu")
            assert await store.any_marked_memory(["tip"]) is True
            assert await store.any_marked_memory(["name"]) is False
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_background_delegate_has_nobody_to_ask(self, monkeypatch):
        import micropaw.tools.delegate as delegate
        seen: dict[str, Any] = {}

        class FakeSubAgent:
            def __init__(self, provider, registry=None, tool_context=None, max_iterations=0):
                seen["ctx"] = tool_context

            async def run(self, task):
                return "ok"

        monkeypatch.setattr(delegate, "SubAgent", FakeSubAgent)
        monkeypatch.setattr(delegate, "_provider", object())
        monkeypatch.setattr(delegate, "_bus", object())
        ctx = _ctx(approve=AsyncMock(return_value=True))
        await delegate.DelegateTool().execute({"task": "t", "mode": "background"}, ctx)
        await asyncio.sleep(0)
        assert seen["ctx"].approve is None and seen["ctx"].conversation_id.endswith(":bg")


class TestOwnerCommandsAndAudit:
    @pytest.mark.asyncio
    async def test_memory_and_forget_handled_by_code(self, tmp_path):
        from unittest.mock import MagicMock

        from micropaw.config import Config
        from micropaw.gateway import Gateway
        store = MemoryStore(str(tmp_path / "t.db"))
        await store.init()
        try:
            await store.save_memory("color", "blue")
            await store.save_memory("tip", "from a page", marked=True)
            gw = Gateway(Config(), MagicMock(), MagicMock(), [])
            gw.store = store
            listing = await gw._owner_command("/memory")
            assert "color: blue" in listing and "tip: from a page (saved after reading outside content)" in listing
            assert await gw._owner_command("/forget color") == "Forgot: color"
            assert "color" not in await gw._owner_command("/memory")
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_every_call_and_decision_is_audited(self):
        from micropaw.utils.audit import tail
        await run_tool(_SideEffect(), {"x": "y" * 1000}, _ctx(approve=AsyncMock(return_value=False)))
        await run_tool(_Reader(), {}, _ctx())
        last_two = tail(2)
        assert [e["outcome"] for e in last_two] == ["declined", "ok"]
        assert last_two[0]["approval"] == "do the thing" and len(last_two[0]["args"]["x"]) < 400
