"""Tests for the MCP client against a small stdio server written for the test."""

from __future__ import annotations

import json
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from micropaw.config import MCPServerConfig
from micropaw.mcp import (
    HTTPMCPServer,
    MCPError,
    MCPServer,
    _expand_env,
    _tool_name,
    start_servers,
    start_servers_background,
)
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


# ---------------------------------------------------------------------------
# Streamable HTTP, against a fake server on 127.0.0.1
# ---------------------------------------------------------------------------


class _Fake(BaseHTTPRequestHandler):
    seen: list[dict] = []
    sse = False

    def log_message(self, *a):
        pass

    def _reply(self, body: dict, sse: bool = False, headers: dict | None = None):
        data = json.dumps(body)
        raw = (f"event: message\ndata: {data}\n\n" if sse else data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_DELETE(self):
        _Fake.seen.append({"delete": self.headers.get("Mcp-Session-Id")})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Fake.seen.append({"msg": msg, "sid": self.headers.get("Mcp-Session-Id"),
                           "accept": self.headers.get("Accept"), "auth": self.headers.get("Authorization")})
        if "id" not in msg or "method" not in msg:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        method, i = msg["method"], msg["id"]
        if method == "initialize":
            self._reply({"jsonrpc": "2.0", "id": i, "result": {"protocolVersion": "2025-06-18"}},
                        headers={"Mcp-Session-Id": "sess-1"})
        elif method == "tools/list":
            self._reply({"jsonrpc": "2.0", "id": i, "result": {"tools": [{"name": "echo"}, {"name": "fail"}]}})
        elif msg["params"]["name"] == "echo":
            # streamed: a notification first, then the answer
            body = [{"jsonrpc": "2.0", "method": "notifications/progress"},
                    {"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": "hi"}]}}]
            raw = "".join(f"data: {json.dumps(b)}\n\n" for b in body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self._reply({"jsonrpc": "2.0", "id": i, "error": {"code": -1, "message": "nope"}})


@pytest.fixture
def fake_http():
    _Fake.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/mcp"
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.asyncio
async def test_http_server_lists_and_calls(fake_http, monkeypatch):
    monkeypatch.setenv("PAW_TEST_TOKEN", "abc")
    cfg = MCPServerConfig(url=fake_http, headers={"Authorization": "Bearer ${PAW_TEST_TOKEN}"}, allow=["echo"])
    servers, tools = await start_servers({"web": cfg})
    try:
        by = {t.remote_name: t for t in tools}
        assert isinstance(servers[0], HTTPMCPServer) and set(by) == {"echo", "fail"}
        assert await by["echo"].execute({}, _ctx()) == "hi"
        assert (await by["fail"].execute({}, _ctx())).startswith("Error: nope")
        # same approval and mark rules as stdio tools
        assert by["echo"].needs_approval({}, _ctx()) is None
        assert by["echo"].needs_approval({}, _ctx(marked=True))
        assert by["fail"].needs_approval({}, _ctx()) and by["echo"].marks
    finally:
        for s in servers:
            await s.close()
    calls = [m for m in _Fake.seen if "msg" in m]
    assert all("text/event-stream" in c["accept"] and c["auth"] == "Bearer abc" for c in calls)
    assert calls[0]["sid"] is None and calls[-1]["sid"] == "sess-1"  # session id kept after initialize
    assert _Fake.seen[-1] == {"delete": "sess-1"}


def test_header_env_expansion_only_from_environment(monkeypatch):
    monkeypatch.setenv("PAW_A", "x")
    assert _expand_env("k-${PAW_A}-$PAW_A-{PAW_A}") == "k-x-$PAW_A-{PAW_A}"
    monkeypatch.delenv("PAW_MISSING", raising=False)
    with pytest.raises(MCPError, match="PAW_MISSING"):
        _expand_env("${PAW_MISSING}")


@pytest.mark.asyncio
async def test_http_non_local_url_goes_through_validate_url():
    for url in ("http://192.168.1.5/mcp", "http://169.254.169.254/mcp", "ftp://example.com/mcp"):
        servers, tools = await start_servers({"x": MCPServerConfig(url=url)})
        assert servers == [] and tools == []


@pytest.mark.asyncio
async def test_http_unset_header_variable_skips_the_server(fake_http, monkeypatch):
    monkeypatch.delenv("PAW_NOT_SET_XYZ", raising=False)
    servers, _ = await start_servers({"x": MCPServerConfig(url=fake_http, headers={"A": "${PAW_NOT_SET_XYZ}"})})
    assert servers == []


def test_config_needs_command_or_url():
    with pytest.raises(ValueError):
        MCPServerConfig()
    assert MCPServerConfig(command="x").url == ""


@pytest.mark.asyncio
async def test_missing_command_names_it():
    server = MCPServer("gone", MCPServerConfig(command="definitely-not-a-command-xyz"))
    with pytest.raises(MCPError, match="definitely-not-a-command-xyz"):
        await server.start()


@pytest.mark.asyncio
async def test_background_start_calls_back_as_each_server_is_ready(fake_http):
    got: list[tuple[str, list[str]]] = []
    cfgs = {"web": MCPServerConfig(url=fake_http), "bad": MCPServerConfig(command="no-such-cmd-xyz"),
            "off": MCPServerConfig(url=fake_http, enabled=False)}
    ready: list = []
    def on_ready(server, tools):
        ready.append(server)
        got.append((server.name, [t.remote_name for t in tools]))

    await start_servers_background(cfgs, on_ready)
    try:
        assert got == [("web", ["echo", "fail"])]
    finally:
        for s in ready:
            await s.close()
