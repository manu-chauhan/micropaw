"""MCP client: tools from Model Context Protocol servers (stdio), e.g. REA, GhidraMCP, blender-mcp.

Each server runs as a child process speaking JSON-RPC 2.0, one message per line. Its tools are
registered as mcp_<server>_<tool>. Safety: servers get an environment without MicroPaw's keys,
their output marks the conversation as outside content, and every call asks the owner unless
the tool is listed in that server's `allow`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

from micropaw.config import MCPServerConfig
from micropaw.providers.base import MediaItem
from micropaw.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)

_PROTOCOL = "2025-06-18"
_START_TIMEOUT = 60.0
_MAX_OUTPUT = 50_000


class MCPError(Exception):
    pass


class MCPServer:
    """One MCP server over stdio."""

    def __init__(self, name: str, config: MCPServerConfig) -> None:
        self.name = name
        self.config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._reader: asyncio.Task | None = None

    async def start(self) -> list[dict]:
        """Launch the server, do the handshake and return its tool definitions."""
        from micropaw.tools.shell import _child_env

        self._proc = await asyncio.create_subprocess_exec(
            self.config.command, *self.config.args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**_child_env(), **self.config.env},
            limit=16 * 1024 * 1024,  # tool results can be large single lines
        )
        self._reader = asyncio.create_task(self._read())
        asyncio.create_task(self._drain_stderr())
        try:
            await self.request("initialize", {
                "protocolVersion": _PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "micropaw", "version": "0"},
            }, timeout=_START_TIMEOUT)
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except MCPError as e:
            # Stateless servers (2026 spec) may have no handshake; tools/list decides
            logger.debug("MCP [%s] initialize failed (%s); trying without it", self.name, e)
        tools: list[dict] = []
        cursor = None
        while True:
            page = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=_START_TIMEOUT)
            tools.extend(page.get("tools", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return tools

    async def _send(self, message: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float | None = None) -> Any:
        if not self._proc or self._proc.returncode is not None:
            raise MCPError(f"server {self.name} is not running")
        self._next_id += 1
        request_id = self._next_id
        answer: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = answer
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(answer, timeout or self.config.timeout)
        except TimeoutError:
            raise MCPError(f"{method} timed out") from None
        finally:
            self._pending.pop(request_id, None)

    async def _read(self) -> None:
        assert self._proc and self._proc.stdout
        async for raw in self._proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue  # log lines some servers print on stdout
            if "id" in msg and "method" in msg:
                # The server asks us something (sampling, roots, elicitation): not offered
                await self._send({"jsonrpc": "2.0", "id": msg["id"],
                                  "error": {"code": -32601, "message": "not supported by this client"}})
            elif (answer := self._pending.get(msg.get("id"))) and not answer.done():
                if "error" in msg:
                    answer.set_exception(MCPError(msg["error"].get("message", "error")))
                else:
                    answer.set_result(msg.get("result", {}))
        for answer in self._pending.values():
            if not answer.done():
                answer.set_exception(MCPError(f"server {self.name} exited"))

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        async for line in self._proc.stderr:
            logger.debug("MCP [%s] %s", self.name, line.decode(errors="replace").rstrip())

    async def close(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 5.0)
            except TimeoutError:
                self._proc.kill()
        if self._reader:
            self._reader.cancel()


def _tool_name(server: str, tool: str) -> str:
    """Provider-safe and unique: letters, digits, _ and -, at most 64 characters."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{server}_{tool}")[:64]


class MCPTool(Tool):
    marks = True  # whatever a server returns came from outside

    def __init__(self, server: MCPServer, spec: dict) -> None:
        self.server = server
        self.remote_name = spec["name"]
        self.name = _tool_name(server.name, self.remote_name)
        self.description = f"[{server.name}] {spec.get('description', '')}".strip()
        self.parameters = spec.get("inputSchema") or {"type": "object", "properties": {}}

    def needs_approval(self, args: dict[str, Any], context: ToolContext) -> str | None:
        if self.remote_name in self.server.config.allow and not context.marked:
            return None
        shown = json.dumps(args, ensure_ascii=False)
        shown = shown if len(shown) <= 800 else shown[:800] + " ..."
        return f"Use {self.remote_name} from MCP server '{self.server.name}':\n{shown}"

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str | ToolResult:
        try:
            result = await self.server.request("tools/call", {"name": self.remote_name, "arguments": args})
        except MCPError as e:
            return f"Error: {e}"
        texts: list[str] = []
        media: list[MediaItem] = []
        for part in result.get("content", []):
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image" and part.get("data"):
                media.append(MediaItem(type="image", data=base64.b64decode(part["data"]),
                                       mime_type=part.get("mimeType", "image/png")))
            elif part.get("type") == "resource":
                texts.append(part.get("resource", {}).get("text", ""))
        if not texts and "structuredContent" in result:
            texts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        text = "\n".join(t for t in texts if t)[:_MAX_OUTPUT] or "(no output)"
        if result.get("isError"):
            text = f"Error: {text}"
        return ToolResult(text=text, media=media) if media else text


async def start_servers(configs: dict[str, MCPServerConfig]) -> tuple[list[MCPServer], list[MCPTool]]:
    """Start every enabled server; one that fails is logged and skipped."""
    servers: list[MCPServer] = []
    tools: list[MCPTool] = []
    for name, cfg in configs.items():
        if not cfg.enabled:
            continue
        server = MCPServer(name, cfg)
        try:
            specs = await server.start()
        except (OSError, MCPError) as e:
            logger.warning("MCP server '%s' did not start: %s", name, e)
            await server.close()
            continue
        servers.append(server)
        tools.extend(MCPTool(server, spec) for spec in specs if spec.get("name"))
        logger.info("MCP server '%s': %d tools", name, len(specs))
    return servers, tools
