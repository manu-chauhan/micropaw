"""OpenAI provider via raw httpx — no SDK dependency."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncGenerator

import httpx

from micropaw.providers.base import AIProvider, AIResponse, Message, ToolCall, ToolDef, retry_on_transient

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.openai.com"


class OpenAIProvider(AIProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o", max_tokens: int = 4096, base_url: str = _DEFAULT_BASE) -> None:
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self._api_url = f"{base_url.rstrip('/')}/v1/chat/completions"
        self._client = httpx.AsyncClient(timeout=120.0)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_body(self, messages: list[Message], tools: list[ToolDef] | None) -> dict:
        api_messages = []
        for msg in messages:
            if msg.role == "system":
                api_messages.append({"role": "system", "content": msg.content})
            elif msg.role == "assistant" and msg.tool_calls:
                api_messages.append({
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
                })
            elif msg.role == "tool":
                api_messages.append({"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content})
            elif msg.role == "user" and msg.media:
                parts: list[dict] = []
                for media in msg.media:
                    b64 = base64.b64encode(media.data).decode()
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{media.mime_type};base64,{b64}"}})
                parts.append({"type": "text", "text": msg.content})
                api_messages.append({"role": "user", "content": parts})
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        body: dict = {"model": self.model, "messages": api_messages, "max_tokens": self.max_tokens}
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        return body

    async def _do_chat(self, body: dict) -> dict:
        resp = await self._client.post(self._api_url, headers=self._headers(), json=body)
        resp.raise_for_status()
        return resp.json()

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        body = self._build_body(messages, tools)
        logger.debug("Calling OpenAI %s", self.model)

        data = await retry_on_transient(self._do_chat, body)

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
        )

    async def chat_stream(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AsyncGenerator[str | AIResponse, None]:
        body = self._build_body(messages, tools)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        logger.debug("Streaming OpenAI %s", self.model)

        content = ""
        tool_calls_map: dict[int, dict] = {}
        usage = {"input": 0, "output": 0}

        async with self._client.stream("POST", self._api_url, headers=self._headers(), json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    break

                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage["input"] = u.get("prompt_tokens", 0)
                    usage["output"] = u.get("completion_tokens", 0)

                if not chunk.get("choices"):
                    continue

                delta = chunk["choices"][0].get("delta", {})

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

        yield AIResponse(content=content, tool_calls=final_tool_calls, usage=usage)

    async def close(self) -> None:
        await self._client.aclose()
