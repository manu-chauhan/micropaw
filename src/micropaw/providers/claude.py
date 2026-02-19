"""Anthropic Claude provider via raw httpx — no SDK dependency."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncGenerator

import httpx

from micropaw.providers.base import AIProvider, AIResponse, Message, ToolCall, ToolDef, parse_sse, retry_on_transient

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"


class ClaudeProvider(AIProvider):
    name = "claude"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
        base_url: str = _DEFAULT_BASE,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self._api_url = f"{base_url.rstrip('/')}/v1/messages"
        self._client = httpx.AsyncClient(timeout=120.0)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

    def _build_body(self, messages: list[Message], tools: list[ToolDef] | None) -> dict:
        system_msg = next((m for m in messages if m.role == "system"), None)
        non_system = [m for m in messages if m.role != "system"]

        api_messages = []
        for msg in non_system:
            if msg.role == "assistant" and msg.tool_calls:
                blocks: list[dict] = []
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                api_messages.append({"role": "assistant", "content": blocks})
            elif msg.role == "tool":
                api_messages.append({
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": msg.tool_call_id, "content": msg.content}],
                })
            elif msg.role == "user" and msg.media:
                blocks = []
                for media in msg.media:
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media.mime_type,
                            "data": base64.b64encode(media.data).decode(),
                        },
                    })
                blocks.append({"type": "text", "text": msg.content})
                api_messages.append({"role": "user", "content": blocks})
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        body: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": api_messages,
        }
        if system_msg:
            body["system"] = system_msg.content
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        return body

    async def _do_chat(self, body: dict) -> dict:
        resp = await self._client.post(self._api_url, headers=self._headers(), json=body)
        resp.raise_for_status()
        return resp.json()

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        body = self._build_body(messages, tools)
        logger.debug("Calling Claude %s", self.model)

        data = await retry_on_transient(self._do_chat, body)

        content = ""
        tool_calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block["type"] == "text":
                content += block["text"]
            elif block["type"] == "tool_use":
                tool_calls.append(ToolCall(id=block["id"], name=block["name"], arguments=block.get("input", {})))

        usage = data.get("usage", {})
        return AIResponse(
            content=content,
            tool_calls=tool_calls,
            usage={"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)},
            stop_reason=data.get("stop_reason", ""),
        )

    async def chat_stream(
        self, messages: list[Message], tools: list[ToolDef] | None = None,
    ) -> AsyncGenerator[str | AIResponse, None]:
        body = self._build_body(messages, tools)
        body["stream"] = True
        logger.debug("Streaming Claude %s", self.model)

        content = ""
        tool_calls: list[ToolCall] = []
        input_tokens = 0
        output_tokens = 0
        current_tool_id = ""
        current_tool_name = ""
        current_tool_json = ""

        async with self._client.stream("POST", self._api_url, headers=self._headers(), json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                event = parse_sse(line)
                if event is None:
                    continue
                etype = event.get("type", "")

                if etype == "message_start":
                    msg = event.get("message", {})
                    input_tokens = msg.get("usage", {}).get("input_tokens", 0)

                elif etype == "content_block_start":
                    cb = event.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        current_tool_id = cb.get("id", "")
                        current_tool_name = cb.get("name", "")
                        current_tool_json = ""

                elif etype == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        content += text
                        yield text
                    elif delta.get("type") == "input_json_delta":
                        current_tool_json += delta.get("partial_json", "")

                elif etype == "content_block_stop":
                    if current_tool_id:
                        try:
                            args = json.loads(current_tool_json) if current_tool_json else {}
                        except json.JSONDecodeError:
                            args = {}
                        tool_calls.append(ToolCall(id=current_tool_id, name=current_tool_name, arguments=args))
                        current_tool_id = ""
                        current_tool_name = ""
                        current_tool_json = ""

                elif etype == "message_delta":
                    output_tokens = event.get("usage", {}).get("output_tokens", output_tokens)

        yield AIResponse(
            content=content,
            tool_calls=tool_calls,
            usage={"input": input_tokens, "output": output_tokens},
        )

    async def close(self) -> None:
        await self._client.aclose()
