"""Google Gemini provider via raw httpx — no SDK dependency."""

from __future__ import annotations

import logging
import uuid
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

_DEFAULT_BASE = "https://generativelanguage.googleapis.com"


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        max_tokens: int = 4096,
        base_url: str = _DEFAULT_BASE,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self._api_base = f"{base_url.rstrip('/')}/v1beta/models"
        self._client = httpx.AsyncClient(
            timeout=http_timeout(120.0),
            headers={"x-goog-api-key": api_key},
        )

    def _url(self, streaming: bool = False) -> str:
        action = "streamGenerateContent?alt=sse" if streaming else "generateContent"
        return f"{self._api_base}/{self.model}:{action}"

    def _build_body(self, messages: list[Message], tools: list[ToolDef] | None) -> dict:
        system_parts: list[str] = []
        contents: list[dict] = []

        # Build mapping from tool_call_id -> function name for tool responses
        tool_id_to_name: dict[str, str] = {}
        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_id_to_name[tc.id] = tc.name

        def inline(media) -> dict:
            return {"inline_data": {"mime_type": media.mime_type, "data": media.b64()}}

        responses: list[dict] = []  # parallel function responses, flushed as ONE user turn
        images: list[dict] = []  # tool-result images, placed after the responses in that turn

        def flush() -> None:
            nonlocal responses, images
            if responses:
                contents.append({"role": "user", "parts": responses + images})
            responses, images = [], []

        for msg in messages:
            if msg.role != "tool":
                flush()
            if msg.role == "system":
                system_parts.append(msg.content)
            elif msg.role == "user":
                parts: list[dict] = [inline(m) for m in msg.media if m.type == "image"]
                parts.append({"text": msg.content})
                contents.append({"role": "user", "parts": parts})
            elif msg.role == "assistant":
                parts = []
                if msg.content:
                    parts.append({"text": msg.content})
                for tc in msg.tool_calls:
                    part: dict = {"functionCall": {"name": tc.name, "args": tc.arguments}}
                    if tc.thought_signature:
                        part["thoughtSignature"] = tc.thought_signature
                    parts.append(part)
                if parts:
                    contents.append({"role": "model", "parts": parts})
            elif msg.role == "tool":
                func_name = tool_id_to_name.get(msg.tool_call_id or "", msg.tool_call_id or "unknown")
                responses.append({"functionResponse": {"name": func_name, "response": {"result": msg.content}}})
                images += [inline(m) for m in msg.media if m.type == "image"]
        flush()

        body: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        if system_parts:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if tools:
            func_decls = []
            for t in tools:
                params = dict(t.parameters) if t.parameters else {}
                params.pop("additionalProperties", None)
                func_decls.append({"name": t.name, "description": t.description, "parameters": params})
            body["tools"] = [{"functionDeclarations": func_decls}]
        return body

    def _parse_candidates(self, data: dict) -> tuple[str, list[ToolCall]]:
        content = ""
        tool_calls: list[ToolCall] = []
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if "text" in part:
                    content += part["text"]
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    tool_calls.append(ToolCall(
                        id=f"gemini_{uuid.uuid4().hex[:12]}",
                        name=fc.get("name", ""),
                        arguments=fc.get("args", {}),
                        thought_signature=part.get("thoughtSignature"),
                    ))
        return content, tool_calls

    async def _do_chat(self, body: dict) -> dict:
        resp = await self._client.post(self._url(), headers={"Content-Type": "application/json"}, json=body)
        resp.raise_for_status()
        return resp.json()

    def _parse_response(self, data: dict) -> AIResponse:
        content, tool_calls = self._parse_candidates(data)
        meta = data.get("usageMetadata", {})
        return AIResponse(
            content=content,
            tool_calls=tool_calls,
            usage={"input": meta.get("promptTokenCount", 0), "output": meta.get("candidatesTokenCount", 0)},
        )

    async def chat_stream(
        self, messages: list[Message], tools: list[ToolDef] | None = None,
    ) -> AsyncGenerator[str | AIResponse, None]:
        body = self._build_body(messages, tools)
        logger.debug("Streaming Gemini %s", self.model)

        content = ""
        tool_calls: list[ToolCall] = []
        usage_meta: dict = {}

        url = self._url(streaming=True)
        async with self._client.stream(
            "POST", url, headers={"Content-Type": "application/json"}, json=body,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                chunk = parse_sse(line)
                if chunk is None:
                    continue
                chunk_content, chunk_tools = self._parse_candidates(chunk)
                if chunk_content:
                    content += chunk_content
                    yield chunk_content
                tool_calls.extend(chunk_tools)
                if "usageMetadata" in chunk:
                    usage_meta = chunk["usageMetadata"]

        yield AIResponse(
            content=content,
            tool_calls=tool_calls,
            usage={"input": usage_meta.get("promptTokenCount", 0), "output": usage_meta.get("candidatesTokenCount", 0)},
        )
