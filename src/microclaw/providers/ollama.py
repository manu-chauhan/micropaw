"""Ollama provider via raw httpx — for local models."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncGenerator

import httpx

from microclaw.providers.base import AIProvider, AIResponse, Message, ToolCall, ToolDef

logger = logging.getLogger(__name__)


class OllamaProvider(AIProvider):
    name = "ollama"

    def __init__(self, host: str = "http://localhost:11434", model: str = "llama3.1", max_tokens: int = 4096) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(timeout=300.0)  # local models can be slow

    def _url(self) -> str:
        return f"{self.host}/api/chat"

    def _build_body(self, messages: list[Message], tools: list[ToolDef] | None, stream: bool = False) -> dict:
        api_messages = []
        for msg in messages:
            if msg.role == "tool":
                api_messages.append({"role": "tool", "content": msg.content})
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        body: dict = {
            "model": self.model,
            "messages": api_messages,
            "stream": stream,
            "options": {"num_predict": self.max_tokens},
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
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

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        body = self._build_body(messages, tools, stream=False)
        logger.debug("Calling Ollama %s", self.model)

        resp = await self._client.post(self._url(), json=body)
        resp.raise_for_status()
        data = resp.json()

        msg = data.get("message", {})
        return AIResponse(
            content=msg.get("content", ""),
            tool_calls=self._parse_tool_calls(msg),
            usage={
                "input": data.get("prompt_eval_count", 0),
                "output": data.get("eval_count", 0),
            },
        )

    async def chat_stream(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AsyncGenerator[str | AIResponse, None]:
        body = self._build_body(messages, tools, stream=True)
        logger.debug("Streaming Ollama %s", self.model)

        content = ""
        tool_calls: list[ToolCall] = []
        input_tokens = 0
        output_tokens = 0

        async with self._client.stream("POST", self._url(), json=body) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
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
