"""Tests for the MCP client against a small stdio server written for the test."""

from __future__ import annotations

import sys
import textwrap

import pytest

from micropaw.config import MCPServerConfig
from micropaw.mcp import _tool_name, start_servers
from micropaw.tools.base import ToolContext, ToolResult

_SERVER = textwrap.dedent('''
    import json, os, sys
    PNG = "iVBORw0KGgo="
    tools = [
        {"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"t": {"type": "string"}}}},
        {"name": "env.check", "description": "Report env"},
        {"name": "shot", "description": "Image"},
        {"name": "boom", "description": "Fails"},
    ]
    def send(m): sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
    print("not json, a log line")
    sys.stdout.flush()
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m or "method" not in m:
            continue  # notifications, and our replies to the sampling request
        method, i = m["method"], m["id"]
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": i, "result": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        elif method == "tools/list":
            if m["params"].get("cursor"):
                send({"jsonrpc": "2.0", "id": i, "result": {"tools": tools[2:]}})
            else:
                send({"jsonrpc": "2.0", "id": i, "result": {"tools": tools[:2], "nextCursor": "p2"}})
        elif method == "tools/call":
            name, args = m["params"]["name"], m["params"]["arguments"]
            send({"jsonrpc": "2.0", "id": 99, "method": "sampling/createMessage", "params": {}})
            if name == "echo":
                content = [{"type": "text", "text": "echo " + args.get("t", "")}]
                send({"jsonrpc": "2.0", "id": i, "result": {"content": content}})
            elif name == "env.check":
                seen = "KEY_VISIBLE" if os.environ.get("ANTHROPIC_API_KEY") else "no key"
                send({"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": seen + " " + os.environ.get("EXTRA", "")}]}})
            elif name == "shot":
                send({"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "image", "data": PNG, "mimeType": "image/png"}]}})
            else:
                send({"jsonrpc": "2.0", "id": i, "result": {"isError": True, "content": [{"type": "text", "text": "it broke"}]}})
''')


@pytest.fixture
async def mcp(tmp_path, monkeypatch):
    script = tmp_path / "server.py"
    script.write_text(_SERVER)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    cfg = MCPServerConfig(command=sys.executable, args=["-I", str(script)], env={"EXTRA": "given"}, allow=["echo"])
    servers, tools = await start_servers({"rea": cfg})
    yield {t.remote_name: t for t in tools}
    for s in servers:
        await s.close()


def _ctx(**kw):
    return ToolContext(conversation_id="c", user_id="u", platform="terminal", **kw)


@pytest.mark.asyncio
async def test_tools_listed_across_pages_with_safe_names(mcp):
    assert set(mcp) == {"echo", "env.check", "shot", "boom"}
    assert mcp["env.check"].name == "mcp_rea_env_check"
    assert all(t.marks for t in mcp.values())


@pytest.mark.asyncio
async def test_calls_and_results(mcp):
    assert await mcp["echo"].execute({"t": "hi"}, _ctx()) == "echo hi"
    shot = await mcp["shot"].execute({}, _ctx())
    assert isinstance(shot, ToolResult) and shot.media[0].mime_type == "image/png"
    assert (await mcp["boom"].execute({}, _ctx())).startswith("Error: it broke")


@pytest.mark.asyncio
async def test_server_never_sees_micropaw_keys(mcp):
    assert await mcp["env.check"].execute({}, _ctx()) == "no key given"


@pytest.mark.asyncio
async def test_asks_unless_allowed_and_unmarked(mcp):
    assert mcp["echo"].needs_approval({"t": "x"}, _ctx()) is None
    assert mcp["echo"].needs_approval({"t": "x"}, _ctx(marked=True))
    assert "env.check" in mcp["env.check"].needs_approval({}, _ctx())


@pytest.mark.asyncio
async def test_bad_server_is_skipped():
    servers, tools = await start_servers({"gone": MCPServerConfig(command="definitely-not-a-command-xyz")})
    assert servers == [] and tools == []


def test_tool_names_fit_provider_limits():
    name = _tool_name("server with spaces", "x" * 100)
    assert len(name) <= 64 and " " not in name
