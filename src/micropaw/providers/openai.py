"""OpenAI provider via raw httpx — no SDK dependency."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncGenerator
from urllib.parse import urlparse

from micropaw import net
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

_DEFAULT_BASE = "https://api.openai.com"


class OpenAIProvider(AIProvider):
    """Any OpenAI-style chat server. quirks (from the preset): "reasoning_content" is sent back
    on tool-call turns (DeepSeek), "no_stream_with_tools" answers tool turns without streaming (Qwen)."""

    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
        max_tokens: int = 4096,
        base_url: str = _DEFAULT_BASE,
        quirks: frozenset[str] = frozenset(),
        read_timeout: float = 120.0,
        name: str = "openai",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.name = name
        self.quirks = quirks
        base = base_url.rstrip("/")
        # A base URL that already ends in a version ("/v1", "/v4") only needs the endpoint
        versioned = re.search(r"/v\d+$", urlparse(base).path)
        self._api_url = f"{base}{'' if versioned else '/v1'}/chat/completions"
        self._is_openai = urlparse(base_url).hostname == "api.openai.com"
        self._client = net.client(base_url, timeout=http_timeout(read_timeout), limits=net.LIMITS)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_body(self, messages: list[Message], tools: list[ToolDef] | None) -> dict:
        api_messages = []
        pending: list[dict] = []  # tool-result images; tool messages can't carry them
        for i, msg in enumerate(messages):
            if msg.role == "system":
                api_messages.append({"role": "system", "content": msg.content})
            elif msg.role == "assistant" and msg.tool_calls:
                entry: dict = {
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in msg.tool_calls
                    ],
                }
                if "reasoning_content" in self.quirks:
                    reasoning = next((t["reasoning_content"] for t in msg.thinking if "reasoning_content" in t), None)
                    if reasoning:
                        entry["reasoning_content"] = reasoning
                api_messages.append(entry)
            elif msg.role == "tool":
                api_messages.append({"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content})
                for media in msg.media:
                    b64 = media.b64()
                    pending.append({"type": "image_url", "image_url": {"url": f"data:{media.mime_type};base64,{b64}"}})
                if pending and (i + 1 == len(messages) or messages[i + 1].role != "tool"):
                    pending.append({"type": "text", "text": "Image returned by the tool."})
                    api_messages.append({"role": "user", "content": pending})
                    pending = []
            elif msg.role == "user" and msg.media:
                parts: list[dict] = []
                for media in msg.media:
                    b64 = media.b64()
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{media.mime_type};base64,{b64}"}})
                parts.append({"type": "text", "text": msg.content})
                api_messages.append({"role": "user", "content": parts})
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        # Newer OpenAI models reject max_tokens; every other compatible server still wants it.
        limit_key = "max_completion_tokens" if self._is_openai else "max_tokens"
        body: dict = {"model": self.model, "messages": api_messages, limit_key: self.max_tokens}
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

    async def _do_chat(self, body: dict) -> dict:
        resp = await self._client.post(self._api_url, headers=self._headers(), json=body)
        resp.raise_for_status()
        return resp.json()

    def _parse_response(self, data: dict) -> AIResponse:
        choices = data.get("choices") or []
        if not choices:
            return AIResponse(content="", tool_calls=[], usage={}, stop_reason="error")
        choice = choices[0]
        msg = choice.get("message", {})
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            tool_calls.append(ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=args))
        usage = data.get("usage", {})
        return AIResponse(
            content=msg.get("content") or "",
            tool_calls=tool_calls,
            usage={"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)},
            stop_reason=choice.get("finish_reason", ""),
            thinking=self._reasoning(msg.get("reasoning_content")),
        )

    def _reasoning(self, text: str | None) -> list[dict]:
        return [{"reasoning_content": text}] if text and "reasoning_content" in self.quirks else []

    async def chat_stream(
        self, messages: list[Message], tools: list[ToolDef] | None = None,
    ) -> AsyncGenerator[str | AIResponse, None]:
        if tools and "no_stream_with_tools" in self.quirks:
            async for item in super().chat_stream(messages, tools):
                yield item
            return
        body = self._build_body(messages, tools)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        logger.debug("Streaming OpenAI %s", self.model)

        content = ""
        reasoning = ""
        tool_calls_map: dict[int, dict] = {}
        usage = {"input": 0, "output": 0}

        async with self._client.stream("POST", self._api_url, headers=self._headers(), json=body) as resp:
            if resp.status_code >= 400:
                await resp.aread()  # so the error text can be read (context-length detection)
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                chunk = parse_sse(line)
                if chunk is None:
                    continue

                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage["input"] = u.get("prompt_tokens", 0)
                    usage["output"] = u.get("completion_tokens", 0)

                if not chunk.get("choices"):
                    continue

                delta = chunk["choices"][0].get("delta", {})

                reasoning += delta.get("reasoning_content") or ""
                if delta.get("content"):
                    content += delta["content"]
                    yield delta["content"]

                for tc_chunk in delta.get("tool_calls") or []:
                    idx = tc_chunk["index"]
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {"id": tc_chunk.get("id", ""), "name": "", "args_json": ""}
                    entry = tool_calls_map[idx]
                    if tc_chunk.get("id"):
                        entry["id"] = tc_chunk["id"]
                    func = tc_chunk.get("function", {})
                    if func.get("name"):
                        entry["name"] = func["name"]
                    if func.get("arguments"):
                        entry["args_json"] += func["arguments"]

        final_tool_calls: list[ToolCall] = []
        for idx in sorted(tool_calls_map):
            entry = tool_calls_map[idx]
            try:
                args = json.loads(entry["args_json"]) if entry["args_json"] else {}
            except json.JSONDecodeError:
                args = {}
            final_tool_calls.append(ToolCall(id=entry["id"], name=entry["name"], arguments=args))

        yield AIResponse(
            content=content, tool_calls=final_tool_calls, usage=usage, thinking=self._reasoning(reasoning),
        )
