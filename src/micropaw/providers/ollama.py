"""Ollama provider via raw httpx — for local models."""

from __future__ import annotations

import base64
import logging
import uuid
from collections.abc import AsyncGenerator

import httpx

from micropaw.providers.base import AIProvider, AIResponse, Message, ToolCall, ToolDef, parse_sse

logger = logging.getLogger(__name__)


class OllamaProvider(AIProvider):
    name = "ollama"

    def __init__(self, host: str = "http://localhost:11434", model: str = "llama3.1", max_tokens: int = 4096) -> None:
        host = host.rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        self.host = host
        self.model = model
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=300.0)  # local models can be slow
        self._tools_supported = True  # set False if model rejects tools

    def _url(self) -> str:
        return f"{self.host}/api/chat"

    async def check_tool_support(self) -> bool:
        """Probe whether the model supports tool calling. Call once at startup."""
        try:
            dummy_tool = {
                "type": "function",
                "function": {"name": "test", "description": "test", "parameters": {"type": "object", "properties": {}}},
            }
            resp = await self._client.post(self._url(), json={
                "model": self.model, "stream": False,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [dummy_tool], "options": {"num_predict": 1},
            })
            if resp.status_code == 400 and "does not support tools" in resp.text:
                logger.warning("Model %s does not support tools — tool use disabled", self.model)
                self._tools_supported = False
                return False
        except Exception:
            pass  # network error — assume tools supported, will fail at runtime
        return True

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
            "options": {"num_predict": self.max_tokens},
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
