"""Anthropic Claude provider via raw httpx — no SDK dependency."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncGenerator

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

_DEFAULT_BASE = "https://api.anthropic.com"
_API_VERSION = "2023-06-01"


class ClaudeProvider(AIProvider):
    name = "claude"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5-5",
        max_tokens: int = 16000,
        base_url: str = _DEFAULT_BASE,
        effort: str = "",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self._api_url = f"{base_url.rstrip('/')}/v1/messages"
        self._client = httpx.AsyncClient(timeout=http_timeout(120.0))

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

    @staticmethod
    def _image_block(media) -> dict:
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media.mime_type,
                        "data": base64.b64encode(media.data).decode()},
        }

    def _build_body(self, messages: list[Message], tools: list[ToolDef] | None) -> dict:
        system_msg = next((m for m in messages if m.role == "system"), None)
        non_system = [m for m in messages if m.role != "system"]

        api_messages = []
        for msg in non_system:
            if msg.role == "assistant" and msg.tool_calls:
                # Thinking blocks go back first and unchanged (only ever present within one turn)
                blocks: list[dict] = list(msg.thinking)
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
                if msg.media:
                    result_content = [self._image_block(m) for m in msg.media]
                    if msg.content:
                        result_content.append({"type": "text", "text": msg.content})
                    tool_result = {"type": "tool_result", "tool_use_id": msg.tool_call_id, "content": result_content}
                    api_messages.append({"role": "user", "content": [tool_result]})
                else:
                    api_messages.append({
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": msg.tool_call_id, "content": msg.content}],
                    })
            elif msg.role == "user" and msg.media:
                blocks = [self._image_block(m) for m in msg.media]
                blocks.append({"type": "text", "text": msg.content})
                api_messages.append({"role": "user", "content": blocks})
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        body: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": api_messages,
            # Cache up to the newest message, so each tool round re-reads the conversation cheaply
            "cache_control": {"type": "ephemeral"},
        }
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        if system_msg:
            # Tools come before system in the prefix, so this breakpoint caches both across turns
            body["system"] = [{"type": "text", "text": system_msg.content, "cache_control": {"type": "ephemeral"}}]
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        return body

    def _drop_effort(self, body: dict, status: int, text: str) -> bool:
        """Older models reject output_config.effort; turn it off once instead of failing every call."""
        if status == 400 and "output_config" in body and "effort" in text:
            logger.warning("%s does not accept effort=%s; sending without it", self.model, self.effort)
            self.effort = ""
            body.pop("output_config")
            return True
        return False

    async def _do_chat(self, body: dict) -> dict:
        resp = await self._client.post(self._api_url, headers=self._headers(), json=body)
        if resp.is_error and self._drop_effort(body, resp.status_code, resp.text):
            resp = await self._client.post(self._api_url, headers=self._headers(), json=body)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _usage(usage: dict) -> dict[str, int]:
        cached = usage.get("cache_read_input_tokens", 0) or 0
        written = usage.get("cache_creation_input_tokens", 0) or 0
        out = {
            "input": (usage.get("input_tokens", 0) or 0) + cached + written,
            "output": usage.get("output_tokens", 0) or 0,
            "cache_read": cached,
        }
        if written:
            out["cache_write"] = written
        return out

    def _parse_response(self, data: dict) -> AIResponse:
        content = ""
        tool_calls: list[ToolCall] = []
        thinking: list[dict] = []
        for block in data.get("content", []):
            if block["type"] == "text":
                content += block["text"]
            elif block["type"] == "tool_use":
                tool_calls.append(ToolCall(id=block["id"], name=block["name"], arguments=block.get("input", {})))
            elif block["type"] in ("thinking", "redacted_thinking"):
                thinking.append(block)
        return AIResponse(
            content=content,
            tool_calls=tool_calls,
            usage=self._usage(data.get("usage", {})),
            stop_reason=data.get("stop_reason", ""),
            thinking=thinking,
        )

    async def chat_stream(
        self, messages: list[Message], tools: list[ToolDef] | None = None,
    ) -> AsyncGenerator[str | AIResponse, None]:
        body = self._build_body(messages, tools)
        body["stream"] = True
        logger.debug("Streaming Claude %s", self.model)

        content = ""
        tool_calls: list[ToolCall] = []
        thinking: list[dict] = []
        current_thinking: dict | None = None
        usage: dict = {}
        stop_reason = ""
        current_tool_id = ""
        current_tool_name = ""
        current_tool_json = ""

        async with self._client.stream("POST", self._api_url, headers=self._headers(), json=body) as resp:
            if resp.is_error:
                await resp.aread()
                if self._drop_effort(body, resp.status_code, resp.text):
                    async for item in self.chat_stream(messages, tools):
                        yield item
                    return
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                event = parse_sse(line)
                if event is None:
                    continue
                etype = event.get("type", "")

                if etype == "message_start":
                    usage.update(event.get("message", {}).get("usage", {}))

                elif etype == "content_block_start":
                    cb = event.get("content_block", {})
                    if cb.get("type") in ("thinking", "redacted_thinking"):
                        current_thinking = dict(cb)
                    elif cb.get("type") == "tool_use":
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
                    elif current_thinking is not None and delta.get("type") == "thinking_delta":
                        current_thinking["thinking"] = current_thinking.get("thinking", "") + delta.get("thinking", "")
                    elif current_thinking is not None and delta.get("type") == "signature_delta":
                        current_thinking["signature"] = delta.get("signature", "")

                elif etype == "content_block_stop":
                    if current_thinking is not None:
                        thinking.append(current_thinking)
                        current_thinking = None
                    elif current_tool_id:
                        try:
                            args = json.loads(current_tool_json) if current_tool_json else {}
                        except json.JSONDecodeError:
                            args = {}
                        tool_calls.append(ToolCall(id=current_tool_id, name=current_tool_name, arguments=args))
                        current_tool_id = ""
                        current_tool_name = ""
                        current_tool_json = ""

                elif etype == "message_delta":
                    usage.update(event.get("usage", {}))
                    stop_reason = event.get("delta", {}).get("stop_reason") or stop_reason

        yield AIResponse(
            content=content,
            tool_calls=tool_calls,
            usage=self._usage(usage),
            stop_reason=stop_reason,
            thinking=thinking,
        )
