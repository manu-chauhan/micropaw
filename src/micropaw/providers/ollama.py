"""Ollama provider via raw httpx — for local models."""

from __future__ import annotations

import base64
import functools
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass

import httpx

from micropaw.providers.base import (
    AIProvider,
    AIResponse,
    Message,
    ToolCall,
    ToolDef,
    http_timeout,
    parse_sse,
)

logger = logging.getLogger(__name__)

_GIB = 2**30


@functools.cache
def _mac_ram() -> int | None:
    """macOS has no SC_PHYS_PAGES; ask sysctl once."""
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2).stdout
        return int(out.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def ram_bytes() -> int | None:
    """Physical memory, or None when it cannot be read. No psutil."""
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemStatus()
            status.dwLength = ctypes.sizeof(_MemStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return None
            return int(status.ullTotalPhys)
        if sys.platform == "darwin":
            return _mac_ram()
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None


def default_num_ctx(ram: int | None = None) -> int:
    """8192 on a machine with 16 GB or less (or unknown), else 16384. The KV cache grows with it."""
    ram = ram_bytes() if ram is None else ram
    return 16384 if ram and ram > 17 * _GIB else 8192


@dataclass(frozen=True)
class ShowInfo:
    caps: frozenset[str] | None  # None: this Ollama is too old to list capabilities
    ctx: int | None              # the model's own maximum context

    @property
    def tools(self) -> bool:
        return self.caps is None or "tools" in self.caps

    @property
    def vision(self) -> bool:
        return self.caps is not None and "vision" in self.caps


def parse_show(data: dict) -> ShowInfo:
    """Read /api/show: `capabilities` (completion, vision, tools, thinking, embedding) and the context size."""
    caps = data.get("capabilities")
    ctx = next((v for k, v in (data.get("model_info") or {}).items()
                if k.endswith(".context_length") and isinstance(v, int)), None)
    return ShowInfo(frozenset(caps) if isinstance(caps, list) else None, ctx)


async def fetch_show(client: httpx.AsyncClient, host: str, model: str) -> ShowInfo | None:
    """None when the model is not pulled or the server cannot be reached."""
    try:
        resp = await client.post(f"{host}/api/show", json={"model": model})
        resp.raise_for_status()
        return parse_show(resp.json())
    except (httpx.HTTPError, ValueError):
        return None


async def pull_model(host: str, model: str) -> AsyncIterator[dict]:
    """Stream /api/pull progress: {"status", "total", "completed"} dicts, ending with status "success"."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0)) as client:
        async with client.stream("POST", f"{host.rstrip('/')}/api/pull", json={"model": model}) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                chunk = parse_sse(line, prefix="")
                if chunk is not None:
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])
                    yield chunk


class OllamaProvider(AIProvider):
    name = "ollama"

    def __init__(
        self, host: str = "http://localhost:11434", model: str = "llama3.1", max_tokens: int = 4096,
        num_ctx: int = 0, keep_alive: str = "30m",
    ) -> None:
        host = host.rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        self.host = host
        self.model = model
        self.max_tokens = max_tokens
        self.num_ctx = num_ctx or default_num_ctx()
        self.keep_alive = keep_alive
        self._client = httpx.AsyncClient(timeout=http_timeout(300.0))  # local models can be slow
        self._tools_supported = True  # set False when /api/show or the server says no
        self.info: ShowInfo | None = None  # from /api/show, filled by load_capabilities()
        self.missing = False  # the model is not pulled

    def _url(self) -> str:
        return f"{self.host}/api/chat"

    async def load_capabilities(self) -> ShowInfo | None:
        """Read /api/show once (cached). Never loads the model into memory."""
        if self.info is None:
            self.info = await fetch_show(self._client, self.host, self.model)
            self.missing = self.info is None
            if self.info is not None:
                self._tools_supported = self.info.tools
        return self.info

    def _ctx(self) -> int:
        ceiling = self.info.ctx if self.info and self.info.ctx else self.num_ctx
        return min(self.num_ctx, ceiling)

    def _build_body(self, messages: list[Message], tools: list[ToolDef] | None = None, stream: bool = False) -> dict:
        if not self._tools_supported:
            tools = None
        api_messages: list[dict] = []
        tool_names = {tc.id: tc.name for m in messages for tc in m.tool_calls}
        pending: list[str] = []  # images returned by tools, sent as a user message after the results
        for i, msg in enumerate(messages):
            images = [base64.b64encode(m.data).decode() for m in msg.media if m.type == "image"]
            if msg.role == "tool":
                entry: dict = {"role": "tool", "content": msg.content}
                if msg.tool_call_id in tool_names:
                    entry["tool_name"] = tool_names[msg.tool_call_id]
                api_messages.append(entry)
                pending += images
                if pending and (i + 1 == len(messages) or messages[i + 1].role != "tool"):
                    api_messages.append({"role": "user", "content": "Image returned by the tool.", "images": pending})
                    pending = []
                continue
            entry = {"role": msg.role, "content": msg.content}
            if msg.role == "assistant" and msg.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": tc.name, "arguments": tc.arguments}} for tc in msg.tool_calls
                ]
            if images:
                entry["images"] = images
            api_messages.append(entry)

        body: dict = {
            "model": self.model,
            "messages": api_messages,
            "stream": stream,
            "options": {"num_predict": self.max_tokens, "num_ctx": self._ctx()},
            # "-1" must go as the number -1: Ollama rejects it as a string without a unit
            "keep_alive": int(self.keep_alive) if str(self.keep_alive).lstrip("-").isdigit() else self.keep_alive,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        return body

    def _parse_tool_calls(self, msg: dict) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {})
            tool_calls.append(ToolCall(
                id=f"ollama_{uuid.uuid4().hex[:12]}",
                name=func.get("name", ""),
                arguments=func.get("arguments", {}),
            ))
        return tool_calls

    async def _do_chat(self, body: dict) -> dict:
        resp = await self._client.post(self._url(), json=body)
        if resp.status_code == 400 and "does not support tools" in resp.text:
            logger.warning("Model %s does not support tools — retrying without", self.model)
            self._tools_supported = False
            body.pop("tools", None)
            resp = await self._client.post(self._url(), json=body)
        if resp.status_code >= 400:
            logger.error("Ollama error %d: %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    def _parse_response(self, data: dict) -> AIResponse:
        msg = data.get("message", {})
        return AIResponse(
            content=msg.get("content", ""),
            tool_calls=self._parse_tool_calls(msg),
            usage={
                "input": data.get("prompt_eval_count", 0),
                "output": data.get("eval_count", 0),
            },
        )

    async def chat_stream(
        self, messages: list[Message], tools: list[ToolDef] | None = None,
    ) -> AsyncGenerator[str | AIResponse, None]:
        body = self._build_body(messages, tools, stream=True)
        logger.debug("Streaming Ollama %s", self.model)

        content = ""
        tool_calls: list[ToolCall] = []
        input_tokens = 0
        output_tokens = 0

        logger.debug("Ollama stream request to %s (tools=%s)", self._url(), "tools" in body)
        async with self._client.stream("POST", self._url(), json=body) as resp:
            logger.debug("Ollama stream status: %d", resp.status_code)
            if resp.status_code >= 400:
                body_text = await resp.aread()
                logger.error("Ollama stream error %d: %s", resp.status_code, body_text[:500])
                resp.raise_for_status()
            async for line in resp.aiter_lines():
                chunk = parse_sse(line, prefix="")
                if chunk is None:
                    continue
                if chunk.get("error"):
                    raise RuntimeError(f"Ollama error: {chunk['error']}")  # lets a fallback take over

                msg = chunk.get("message", {})
                if msg.get("content"):
                    content += msg["content"]
                    yield msg["content"]

                tool_calls.extend(self._parse_tool_calls(msg))

                if chunk.get("prompt_eval_count"):
                    input_tokens = chunk["prompt_eval_count"]
                if chunk.get("eval_count"):
                    output_tokens = chunk["eval_count"]

        yield AIResponse(
            content=content,
            tool_calls=tool_calls,
            usage={"input": input_tokens, "output": output_tokens},
        )
