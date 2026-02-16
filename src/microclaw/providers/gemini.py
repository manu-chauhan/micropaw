"""Google Gemini provider via raw httpx — no SDK dependency."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncGenerator

import httpx

from microclaw.providers.base import AIProvider, AIResponse, Message, ToolCall, ToolDef

logger = logging.getLogger(__name__)

_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(AIProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", max_tokens: int = 4096) -> None:
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=120.0)

    def _url(self, streaming: bool = False) -> str:
        action = "streamGenerateContent?alt=sse" if streaming else "generateContent"
        return f"{_API_BASE}/{self.model}:{action}&key={self.api_key}" if streaming else f"{_API_BASE}/{self.model}:{action}?key={self.api_key}"

    def _build_body(self, messages: list[Message], tools: list[ToolDef] | None) -> dict:
        system_text = None
        contents = []

        for msg in messages:
            if msg.role == "system":
                system_text = msg.content
            elif msg.role == "user":
                contents.append({"role": "user", "parts": [{"text": msg.content}]})
            elif msg.role == "assistant":
                parts: list[dict] = []
                if msg.content:
                    parts.append({"text": msg.content})
                for tc in msg.tool_calls:
                    parts.append({"functionCall": {"name": tc.name, "args": tc.arguments}})
                if parts:
                    contents.append({"role": "model", "parts": parts})
            elif msg.role == "tool":
                contents.append({
                    "role": "user",
                    "parts": [{"functionResponse": {"name": msg.tool_call_id or "unknown", "response": {"result": msg.content}}}],
                })

        body: dict = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
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
                    ))
        return content, tool_calls

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        body = self._build_body(messages, tools)
        logger.debug("Calling Gemini %s", self.model)

        resp = await self._client.post(self._url(), headers={"Content-Type": "application/json"}, json=body)
        resp.raise_for_status()
        data = resp.json()

        content, tool_calls = self._parse_candidates(data)
        meta = data.get("usageMetadata", {})
        return AIResponse(
            content=content,
            tool_calls=tool_calls,
            usage={"input": meta.get("promptTokenCount", 0), "output": meta.get("candidatesTokenCount", 0)},
        )

    async def chat_stream(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AsyncGenerator[str | AIResponse, None]:
        body = self._build_body(messages, tools)
        logger.debug("Streaming Gemini %s", self.model)

        content = ""
        tool_calls: list[ToolCall] = []

        async with self._client.stream("POST", self._url(streaming=True), headers={"Content-Type": "application/json"}, json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                chunk = json.loads(line[6:])
                chunk_content, chunk_tools = self._parse_candidates(chunk)
                if chunk_content:
                    content += chunk_content
                    yield chunk_content
                tool_calls.extend(chunk_tools)

        yield AIResponse(content=content, tool_calls=tool_calls, usage={})
