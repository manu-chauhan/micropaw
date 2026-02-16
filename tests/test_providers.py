"""Tests for provider types and failover logic."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from microclaw.providers.base import AIProvider, AIResponse, Message, ToolCall, ToolDef
from microclaw.providers.failover import AllProvidersFailedError, FailoverProvider


# ---------------------------------------------------------------------------
# Mock providers
# ---------------------------------------------------------------------------

class MockProvider(AIProvider):
    name = "mock"

    def __init__(self, response: str = "ok", fail: bool = False) -> None:
        self._response = response
        self._fail = fail

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        if self._fail:
            raise RuntimeError("provider down")
        return AIResponse(
            content=self._response,
            tool_calls=[],
            usage={"input": 10, "output": 5},
            stop_reason="end_turn",
        )

    async def chat_stream(
        self, messages: list[Message], tools: list[ToolDef] | None = None
    ) -> AsyncGenerator[str | AIResponse, None]:
        if self._fail:
            raise RuntimeError("provider down")
        yield self._response
        yield AIResponse(
            content=self._response,
            tool_calls=[],
            usage={"input": 10, "output": 5},
            stop_reason="end_turn",
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestProviderTypes:
    def test_message_creation(self):
        msg = Message(role="user", content="hello")
        assert msg.role == "user"
        assert msg.tool_calls == []

    def test_tool_call_creation(self):
        tc = ToolCall(id="tc1", name="web_search", arguments={"query": "test"})
        assert tc.name == "web_search"

    def test_ai_response_creation(self):
        resp = AIResponse(content="hi", tool_calls=[], usage={"input": 1, "output": 1})
        assert resp.content == "hi"
        assert resp.stop_reason == ""


class TestMockProvider:
    async def test_chat(self):
        p = MockProvider("hello world")
        msgs = [Message(role="user", content="hi")]
        resp = await p.chat(msgs)
        assert resp.content == "hello world"

    async def test_chat_stream(self):
        p = MockProvider("streamed")
        msgs = [Message(role="user", content="hi")]
        chunks = []
        async for chunk in p.chat_stream(msgs):
            chunks.append(chunk)
        assert len(chunks) == 2
        assert chunks[0] == "streamed"
        assert isinstance(chunks[1], AIResponse)

    async def test_default_stream_fallback(self):
        """Base class chat_stream falls back to chat() when not overridden."""

        class MinimalProvider(AIProvider):
            name = "minimal"

            async def chat(self, messages, tools=None):
                return AIResponse(content="fallback", tool_calls=[], usage={"input": 1, "output": 1})

        p = MinimalProvider()
        chunks = []
        async for chunk in p.chat_stream([Message(role="user", content="hi")]):
            chunks.append(chunk)
        assert chunks[0] == "fallback"
        assert isinstance(chunks[1], AIResponse)


class TestFailoverProvider:
    async def test_uses_primary(self):
        primary = MockProvider("primary-response")
        fallback = MockProvider("fallback-response")
        fo = FailoverProvider([primary, fallback])
        resp = await fo.chat([Message(role="user", content="hi")])
        assert resp.content == "primary-response"

    async def test_failover_on_error(self):
        primary = MockProvider(fail=True)
        fallback = MockProvider("fallback-response")
        fo = FailoverProvider([primary, fallback])
        resp = await fo.chat([Message(role="user", content="hi")])
        assert resp.content == "fallback-response"

    async def test_all_fail_raises(self):
        p1 = MockProvider(fail=True)
        p2 = MockProvider(fail=True)
        fo = FailoverProvider([p1, p2])
        with pytest.raises(AllProvidersFailedError):
            await fo.chat([Message(role="user", content="hi")])

    async def test_stream_uses_primary(self):
        primary = MockProvider("streamed-primary")
        fallback = MockProvider("streamed-fallback")
        fo = FailoverProvider([primary, fallback])
        chunks = []
        async for chunk in fo.chat_stream([Message(role="user", content="hi")]):
            chunks.append(chunk)
        assert "streamed-primary" in str(chunks[0])
