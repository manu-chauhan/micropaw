"""MCP client: tools from Model Context Protocol servers, e.g. REA, GhidraMCP, blender-mcp.

A server is either a child process speaking JSON-RPC 2.0, one message per line (`command`), or a
streamable HTTP endpoint (`url`). Its tools are registered as mcp_<server>_<tool>. Safety: servers
get an environment without MicroPaw's keys, their output marks the conversation as outside content,
and every call asks the owner unless the tool is listed in that server's `allow`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from micropaw import net, privacy
from micropaw.config import MCPServerConfig, pin_mcp_tools
from micropaw.providers.base import MediaItem
from micropaw.tools.base import Tool, ToolContext, ToolResult
from micropaw.tools.http_request import _BlockedError, _check_peer

logger = logging.getLogger(__name__)

_PROTOCOL = "2025-06-18"
_START_TIMEOUT = 60.0
_BACKGROUND_START_TIMEOUT = 180.0  # first runs of npx -y or uvx download the package
_MAX_OUTPUT = 50_000
_MAX_TOOLS = 128           # per server
_MAX_DESCRIPTION = 1000    # characters, per tool
_MAX_ARGS = 4000           # characters of JSON; the owner sees all of it in the approval, so there is a limit


# `paw mcp add <preset>` entries: {"command": ..., "args": [...pinned version...]}.
# Empty on purpose: a preset needs a package name and a version confirmed on its primary page,
# and research/05 confirms no version for REA, pyghidra-mcp or jadx-ai-mcp. Add them here when known.
PRESETS: dict[str, dict] = {}


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
        self._dead = ""  # why the reader stopped; every later call fails at once with this
        self.digest = ""  # hash of the tool list, set by start_server

    async def _launch(self) -> None:
        from micropaw.tools.shell import _child_env

        # which() honors PATHEXT, so "npx" finds npx.cmd on Windows
        command = shutil.which(self.config.command)
        if not command:
            raise MCPError(f"command not found: {self.config.command}")
        env = {**_child_env(), **self.config.env}
        argv, env = [command, *self.config.args], env
        if privacy.local_only():
            argv, env = await asyncio.to_thread(self._isolate, command, env)
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=16 * 1024 * 1024,  # tool results can be large single lines
        )
        self._reader = asyncio.create_task(self._read())
        asyncio.create_task(self._drain_stderr())

    def _isolate(self, command: str, env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        """In local and airgapped mode the child runs in bubblewrap (same layout as the shell sandbox), with no
        network unless the server asks and the mode is local. Airgapped refuses where that is not possible."""
        from micropaw.tools import sandbox
        if why := privacy.mcp_refusal(self.name, self.config):
            raise MCPError(why)
        if sandbox.isolation_ok():
            wrapped = sandbox.mcp_wrap(command, self.config.args, self.config, env)
            return wrapped.argv, wrapped.env or env
        if privacy.airgapped():
            raise MCPError(sandbox.isolation_error())
        logger.warning("MCP server '%s' runs without network isolation: this system cannot provide it.", self.name)
        return [command, *self.config.args], env

    async def start(self, timeout: float = _START_TIMEOUT) -> list[dict]:
        """Launch the server, do the handshake and return its tool definitions."""
        await self._launch()
        try:
            await self.request("initialize", {
                "protocolVersion": _PROTOCOL, "capabilities": {},
                "clientInfo": {"name": "micropaw", "version": "0"},
            }, timeout=timeout)
            await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except MCPError as e:
            # Stateless servers (2026 spec) may have no handshake; tools/list decides
            logger.debug("MCP [%s] initialize failed (%s); trying without it", self.name, e)
        tools: list[dict] = []
        cursor = None
        while True:
            page = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout)
            if not isinstance(page, dict) or not isinstance(page.get("tools", []), list) or not all(
                isinstance(t, dict) and isinstance(t.get("name"), str) for t in page.get("tools", [])
            ):
                raise MCPError("its tool list is malformed")
            tools.extend(page.get("tools", []))
            cursor = page.get("nextCursor")
            if not cursor or len(tools) >= _MAX_TOOLS:
                return tools[:_MAX_TOOLS]

    async def _send(self, message: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        await self._proc.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float | None = None) -> Any:
        if self._dead:
            raise MCPError(self._dead)
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
        reason = f"server {self.name} exited"
        try:
            async for raw in self._proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue  # log lines some servers print on stdout
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and "method" in msg:
                    # The server asks us something (sampling, roots, elicitation): not offered
                    await self._send({"jsonrpc": "2.0", "id": msg["id"],
                                      "error": {"code": -32601, "message": "not supported by this client"}})
                elif (answer := self._pending.get(msg.get("id"))) and not answer.done():
                    if "error" in msg:
                        err = msg["error"]
                        text = err.get("message", "error") if isinstance(err, dict) else err
                        answer.set_exception(MCPError(str(text)))
                    else:
                        answer.set_result(msg.get("result", {}))
        except Exception as e:  # e.g. a line over the size limit: stop now instead of letting calls time out
            reason = f"server {self.name} sent output this client cannot read ({type(e).__name__})"
            logger.error("MCP [%s] %s; stopping it", self.name, reason)
            if self._proc.returncode is None:
                self._proc.kill()
        self._dead = reason
        for answer in self._pending.values():
            if not answer.done():
                answer.set_exception(MCPError(reason))

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


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_MAX_BODY = 5 * 1024 * 1024    # one HTTP reply
_MAX_EVENT = 1024 * 1024       # one server-sent event


def _expand_env(value: str) -> str:
    """Replace ${NAME} with the environment variable NAME. Nothing else is expanded."""
    def one(m: re.Match) -> str:
        if m.group(1) not in os.environ:
            raise MCPError(f"environment variable {m.group(1)} is not set")
        return os.environ[m.group(1)]
    return _ENV_REF.sub(one, value)


async def _sse_data(resp: httpx.Response) -> AsyncIterator[str]:
    """The data of each server-sent event in the reply, with size limits."""
    buf, total, data, size = b"", 0, [], 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        buf += chunk.replace(b"\r\n", b"\n")
        if total > _MAX_BODY or len(buf) > _MAX_EVENT:
            raise MCPError("reply too large")
        *lines, buf = buf.split(b"\n")
        for raw in lines:
            if raw.startswith(b"data:"):
                data.append(raw[5:].removeprefix(b" ").decode("utf-8", errors="replace"))
                size += len(raw)
                if size > _MAX_EVENT:
                    raise MCPError("event too large")
            elif not raw and data:
                yield "\n".join(data)
                data, size = [], 0
    if data:
        yield "\n".join(data)


class HTTPMCPServer(MCPServer):
    """One MCP server over streamable HTTP: each message is a POST, the reply is JSON or an event stream."""

    def __init__(self, name: str, config: MCPServerConfig) -> None:
        super().__init__(name, config)
        self._client: httpx.AsyncClient | None = None
        self._session = ""
        self._headers: dict[str, str] = {}
        self._local = True

    async def _launch(self) -> None:
        from urllib.parse import urlparse

        from micropaw.security import validate_url

        url = urlparse(self.config.url)
        self._local = (url.hostname or "").lower() in _LOCAL_HOSTS
        if why := privacy.mcp_refusal(self.name, self.config):
            raise MCPError(why)
        if not self._local:
            if url.scheme != "https":
                raise MCPError("a server that is not on this computer must use https")
            if error := await validate_url(self.config.url):
                raise MCPError(error)
        self._headers = {k: _expand_env(v) for k, v in self.config.headers.items()}
        self._client = net.client(
            self.config.url, follow_redirects=False, timeout=httpx.Timeout(self.config.timeout, connect=15.0),
        )

    async def _send(self, message: dict) -> None:
        try:
            await self._post(message, None)
        except httpx.HTTPError as e:
            raise MCPError(f"send failed: {type(e).__name__}") from None

    async def request(self, method: str, params: dict, timeout: float | None = None) -> Any:
        if not self._client:
            raise MCPError(f"server {self.name} is not running")
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        try:
            return await asyncio.wait_for(self._post(message, self._next_id), timeout or self.config.timeout)
        except TimeoutError:
            raise MCPError(f"{method} timed out") from None
        except httpx.HTTPError as e:
            raise MCPError(f"{method} failed: {type(e).__name__}") from None

    async def _post(self, message: dict, request_id: int | None) -> Any:
        assert self._client
        headers = {**self._headers, "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": _PROTOCOL}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        async with self._client.stream("POST", self.config.url, json=message, headers=headers) as resp:
            if not self._local:
                try:  # the address really connected to, not only the name that was checked
                    _check_peer(resp, self.config.url)
                except _BlockedError as e:
                    raise MCPError(str(e)) from None
            self._session = resp.headers.get("mcp-session-id", self._session)
            if resp.status_code >= 300:  # redirects are never followed: headers must not leave the origin
                raise MCPError(f"HTTP {resp.status_code}")
            if request_id is None or resp.status_code == 202:
                return {}
            if resp.headers.get("content-type", "").startswith("text/event-stream"):
                chunks = _sse_data(resp)
            else:
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > _MAX_BODY:
                        raise MCPError("reply too large")
                chunks = _one(body.decode("utf-8", errors="replace"))
            async for data in chunks:
                try:
                    parsed = json.loads(data)
                except ValueError:
                    continue
                for msg in parsed if isinstance(parsed, list) else [parsed]:
                    if not isinstance(msg, dict):
                        continue
                    if "method" in msg:
                        if "id" in msg:  # the server asks us something: not offered
                            error = {"code": -32601, "message": "not supported by this client"}
                            await self._post({"jsonrpc": "2.0", "id": msg["id"], "error": error}, None)
                    elif msg.get("id") == request_id:
                        if "error" in msg:
                            raise MCPError(msg["error"].get("message", "error"))
                        return msg.get("result", {})
        raise MCPError("no answer in the reply")

    async def close(self) -> None:
        if self._client:
            if self._session:
                with contextlib.suppress(httpx.HTTPError):
                    await self._client.delete(
                        self.config.url, headers={**self._headers, "Mcp-Session-Id": self._session},
                    )
            await self._client.aclose()
            self._client = None


async def _one(text: str) -> AsyncIterator[str]:
    yield text


def _tool_name(server: str, tool: str) -> str:
    """Provider-safe and unique: letters, digits, _ and -, at most 64 characters."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{server}_{tool}")[:64]


class MCPTool(Tool):
    marks = True  # whatever a server returns came from outside

    def __init__(self, server: MCPServer, spec: dict) -> None:
        self.server = server
        self.remote_name = spec["name"]
        self.name = _tool_name(server.name, self.remote_name)
        self.description = f"[{server.name}] {str(spec.get('description', ''))[:_MAX_DESCRIPTION]}".strip()
        self.parameters = spec.get("inputSchema") or {"type": "object", "properties": {}}

    def needs_approval(self, args: dict[str, Any], context: ToolContext) -> str | None:
        if self.remote_name in self.server.config.allow and not context.marked:
            return None
        shown = json.dumps(args, ensure_ascii=False)
        if len(shown) > _MAX_ARGS:
            return None  # execute() refuses
        return f"Use {self.remote_name} from MCP server '{self.server.name}':\n{shown}"

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str | ToolResult:
        if len(json.dumps(args, ensure_ascii=False)) > _MAX_ARGS:
            return f"Error: The arguments are too long to show you in full (max {_MAX_ARGS:,} characters). Not run."
        try:
            result = await self.server.request("tools/call", {"name": self.remote_name, "arguments": args})
        except MCPError as e:
            return f"Error: {e}"
        if not isinstance(result, dict):
            return "Error: The server sent an invalid result."
        texts: list[str] = []
        media: list[MediaItem] = []
        for part in result.get("content", []):
            if not isinstance(part, dict):
                continue
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


def tools_digest(specs: list[dict]) -> str:
    """sha256 of the tool list (names, descriptions, schemas), to notice a server that changed."""
    return hashlib.sha256(json.dumps(specs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def start_server(
    name: str, cfg: MCPServerConfig, timeout: float = _START_TIMEOUT, pin: bool = True,
) -> tuple[MCPServer, list[MCPTool]]:
    """Start one server and check its tool list against the pinned hash. Raises MCPError (or OSError)."""
    server = HTTPMCPServer(name, cfg) if cfg.url else MCPServer(name, cfg)
    try:
        specs = [s for s in await server.start(timeout) if s.get("name")]
        digest = server.digest = tools_digest(specs)
        if cfg.tools_hash and cfg.tools_hash != digest:
            raise MCPError(f"its tool list changed since you approved it; check it, then run: paw mcp trust {name}")
        if not cfg.tools_hash and pin:
            cfg.tools_hash = digest
            with contextlib.suppress(OSError, ValueError):
                pin_mcp_tools(name, digest)
    except (OSError, MCPError, httpx.HTTPError) as e:
        await server.close()
        raise (e if isinstance(e, (OSError, MCPError)) else MCPError(type(e).__name__)) from None
    except asyncio.CancelledError:  # shutdown while starting: don't leave the child process running
        await server.close()
        raise
    logger.info("MCP server '%s': %d tools", name, len(specs))
    return server, [MCPTool(server, spec) for spec in specs]


async def _start_one(name: str, cfg: MCPServerConfig, timeout: float) -> tuple[MCPServer, list[MCPTool]] | None:
    try:
        return await start_server(name, cfg, timeout)
    except (OSError, MCPError) as e:
        logger.warning("MCP server '%s' did not start: %s", name, e)
        return None


async def start_servers(
    configs: dict[str, MCPServerConfig], timeout: float = _START_TIMEOUT,
) -> tuple[list[MCPServer], list[MCPTool]]:
    """Start every enabled server at once; one that fails is logged and skipped."""
    started = await asyncio.gather(*(_start_one(n, c, timeout) for n, c in configs.items() if c.enabled))
    servers = [r[0] for r in started if r]
    return servers, [t for r in started if r for t in r[1]]


def start_servers_background(
    configs: dict[str, MCPServerConfig], on_ready: Callable[[MCPServer, list[MCPTool]], None],
    timeout: float = _BACKGROUND_START_TIMEOUT,
) -> asyncio.Task:
    """Start servers without waiting: on_ready(server, tools) is called as each one comes up.

    The caller keeps the returned task and closes the servers it was given at shutdown.
    """
    async def one(name: str, cfg: MCPServerConfig) -> None:
        if result := await _start_one(name, cfg, timeout):
            on_ready(*result)

    async def run() -> None:
        await asyncio.gather(*(one(n, c) for n, c in configs.items() if c.enabled))

    return asyncio.create_task(run())
