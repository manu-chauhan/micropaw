"""Tests for the tool system — registry, path security, chunk_text."""

from __future__ import annotations

from typing import Any

import pytest

from microclaw.channels.base import chunk_text
from microclaw.tools.base import Tool, ToolContext, ToolRegistry
from microclaw.tools.path_security import validate_file_path, validate_write_path


# ---------------------------------------------------------------------------
# ToolRegistry tests
# ---------------------------------------------------------------------------

class FakeTool(Tool):
    name = "test_tool"
    description = "A test tool"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        return f"result: {args.get('text', '')}"


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        tool = FakeTool()
        reg.register(tool)
        assert reg.get("test_tool") is tool
        assert reg.get("nonexistent") is None

    def test_names(self):
        reg = ToolRegistry()
        reg.register(FakeTool())
        assert "test_tool" in reg.names

    def test_all(self):
        reg = ToolRegistry()
        reg.register(FakeTool())
        assert len(reg.all()) == 1

    def test_to_tool_defs(self):
        reg = ToolRegistry()
        reg.register(FakeTool())
        defs = reg.to_tool_defs()
        assert len(defs) == 1
        assert defs[0].name == "test_tool"


class TestToolExecution:
    async def test_execute(self):
        tool = FakeTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
        result = await tool.execute({"text": "hello"}, ctx)
        assert result == "result: hello"


# ---------------------------------------------------------------------------
# Path security tests
# ---------------------------------------------------------------------------

class TestPathSecurity:
    def test_normal_path_allowed(self):
        assert validate_file_path("/tmp/test.txt") is None

    def test_proc_blocked(self):
        err = validate_file_path("/proc/self/environ")
        assert err is not None
        assert "blocked" in err.lower()

    def test_sys_blocked(self):
        err = validate_file_path("/sys/kernel/version")
        assert err is not None

    def test_etc_shadow_blocked(self):
        err = validate_file_path("/etc/shadow")
        assert err is not None

    def test_ssh_dir_blocked(self):
        err = validate_file_path("/home/user/.ssh/id_rsa")
        assert err is not None
        assert "sensitive" in err.lower()

    def test_env_file_blocked(self):
        err = validate_file_path("/app/.env")
        assert err is not None

    def test_netrc_blocked(self):
        err = validate_file_path("/home/user/.netrc")
        assert err is not None


class TestWritePathSecurity:
    def test_bashrc_blocked(self):
        err = validate_write_path("/home/user/.bashrc")
        assert err is not None
        assert "login script" in err.lower()

    def test_zshrc_blocked(self):
        err = validate_write_path("/home/user/.zshrc")
        assert err is not None

    def test_authorized_keys_blocked(self):
        err = validate_write_path("/home/user/.ssh/authorized_keys")
        assert err is not None

    def test_cron_blocked(self):
        err = validate_write_path("/etc/cron.d/malicious")
        assert err is not None

    def test_normal_write_allowed(self):
        assert validate_write_path("/tmp/output.txt") is None


# ---------------------------------------------------------------------------
# chunk_text tests
# ---------------------------------------------------------------------------

class TestChunkText:
    def test_short_text_single_chunk(self):
        assert chunk_text("hello", 100) == ["hello"]

    def test_exact_limit(self):
        text = "x" * 100
        assert chunk_text(text, 100) == [text]

    def test_splits_on_newline(self):
        text = "line1\nline2\nline3"
        chunks = chunk_text(text, 12)
        assert len(chunks) >= 2
        assert "line1" in chunks[0]

    def test_splits_without_newline(self):
        text = "a" * 10
        chunks = chunk_text(text, 3)
        assert len(chunks) >= 3
        for chunk in chunks:
            assert len(chunk) <= 3

    def test_empty_text(self):
        assert chunk_text("", 100) == [""]
