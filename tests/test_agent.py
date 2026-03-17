"""Tests for the agent loop — handle_message, tool dispatch, caching, error classification."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from micropaw.config import Config
from micropaw.core.agent import _CACHEABLE_TOOLS, Agent, _cache_key, _classify_error
from micropaw.providers.base import AIProvider, AIResponse, Message, ToolCall, ToolDef
from micropaw.tools.base import Tool, ToolContext, ToolRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockProvider(AIProvider):
    name = "mock"

    def __init__(self, responses: list[AIResponse] | None = None) -> None:
        self._responses = list(responses or [])
        self._call_count = 0

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
            self._call_count += 1
            return resp
        return AIResponse(content="default response", tool_calls=[], usage={"input": 10, "output": 5})

    async def chat_stream(
        self, messages: list[Message], tools: list[ToolDef] | None = None,
    ) -> AsyncGenerator[str | AIResponse, None]:
        resp = await self.chat(messages, tools)
        if resp.content:
            yield resp.content
        yield resp


class EchoTool(Tool):
    name = "echo"
    description = "Echoes text"
    parameters: dict[str, Any] = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        return f"echo: {args.get('text', '')}"


class FailTool(Tool):
    name = "fail_tool"
    description = "Always fails"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        raise RuntimeError("tool exploded")


class CounterTool(Tool):
    """Tool that counts how many times it was actually executed."""
    name = "web_search"
    description = "Search the web"
    parameters: dict[str, Any] = {"type": "object", "properties": {"query": {"type": "string"}}}

    def __init__(self):
        self.call_count = 0

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        self.call_count += 1
        return f"result for: {args.get('query', '')}"


def _make_agent(provider: AIProvider, tools: list[Tool] | None = None) -> Agent:
    """Create an agent with a mock store and config."""
    config = Config()
    registry = ToolRegistry()
    for tool in (tools or []):
        registry.register(tool)

    store = MagicMock()
    store.get_or_create_conversation = AsyncMock()
    store.get_messages = AsyncMock(return_value=[])
    store.save_message = AsyncMock()
    store.get_recent_memories = AsyncMock(return_value=[])
    store.search_documents = AsyncMock(return_value=[])
    store.ingest_document = AsyncMock(return_value=0)
    store.get_summary = AsyncMock(return_value=None)
    store.compact_messages = AsyncMock()

    agent = Agent(config, provider, registry, store)
    return agent


# ---------------------------------------------------------------------------
# Tests: handle_message
# ---------------------------------------------------------------------------

class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_simple_response(self):
        provider = MockProvider([
            AIResponse(content="Hello!", tool_calls=[], usage={"input": 10, "output": 5}),
        ])
        agent = _make_agent(provider)
        result = await agent.handle_message("Hi", "user1", "terminal")
        assert result == "Hello!"

    @pytest.mark.asyncio
    async def test_accumulates_usage(self):
        provider = MockProvider([
            AIResponse(content="Hi!", tool_calls=[], usage={"input": 15, "output": 8}),
        ])
        agent = _make_agent(provider)
        await agent.handle_message("Hello", "user1", "terminal")
        assert agent.last_usage == {"input": 15, "output": 8}

    @pytest.mark.asyncio
    async def test_resets_usage_each_message(self):
        provider = MockProvider([
            AIResponse(content="A", tool_calls=[], usage={"input": 10, "output": 5}),
            AIResponse(content="B", tool_calls=[], usage={"input": 20, "output": 10}),
        ])
        agent = _make_agent(provider)
        await agent.handle_message("First", "user1", "terminal")
        assert agent.last_usage == {"input": 10, "output": 5}
        await agent.handle_message("Second", "user1", "terminal")
        assert agent.last_usage == {"input": 20, "output": 10}

    @pytest.mark.asyncio
    async def test_tool_call_then_response(self):
        provider = MockProvider([
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="echo", arguments={"text": "hello"})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(content="Got it: echo: hello", tool_calls=[], usage={"input": 20, "output": 10}),
        ])
        agent = _make_agent(provider, [EchoTool()])
        result = await agent.handle_message("Echo hello", "user1", "terminal")
        assert result == "Got it: echo: hello"
        assert agent.last_usage == {"input": 30, "output": 15}

    @pytest.mark.asyncio
    async def test_tool_error_handled(self):
        provider = MockProvider([
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="fail_tool", arguments={})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(content="Sorry, tool failed", tool_calls=[], usage={"input": 20, "output": 10}),
        ])
        agent = _make_agent(provider, [FailTool()])
        result = await agent.handle_message("Do something", "user1", "terminal")
        assert result == "Sorry, tool failed"

    @pytest.mark.asyncio
    async def test_unknown_tool_handled(self):
        provider = MockProvider([
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="nonexistent", arguments={})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(content="Hmm, I can't do that", tool_calls=[], usage={"input": 20, "output": 10}),
        ])
        agent = _make_agent(provider)
        result = await agent.handle_message("Use nonexistent tool", "user1", "terminal")
        assert result == "Hmm, I can't do that"

    @pytest.mark.asyncio
    async def test_provider_error_returns_friendly_message(self):
        import httpx
        provider = MockProvider()
        provider.chat = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        agent = _make_agent(provider)
        result = await agent.handle_message("Hi", "user1", "terminal")
        assert "connect" in result.lower()

    @pytest.mark.asyncio
    async def test_empty_content_returns_fallback(self):
        provider = MockProvider([
            AIResponse(content="", tool_calls=[], usage={"input": 10, "output": 0}),
        ])
        agent = _make_agent(provider)
        result = await agent.handle_message("Hi", "user1", "terminal")
        assert "not sure" in result.lower()

    @pytest.mark.asyncio
    async def test_max_iterations_stops(self):
        """Agent should stop after max_tool_iterations even with continuous tool calls."""
        # Create a provider that always returns a tool call
        tool_response = AIResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="echo", arguments={"text": "loop"})],
            usage={"input": 5, "output": 3},
        )
        provider = MockProvider([tool_response] * 20)  # More than max iterations
        agent = _make_agent(provider, [EchoTool()])
        agent.max_tool_iterations = 3
        await agent.handle_message("Loop forever", "user1", "terminal")
        assert provider._call_count <= 4  # max_iterations + 1 safety margin


# ---------------------------------------------------------------------------
# Tests: handle_message_stream
# ---------------------------------------------------------------------------

class TestHandleMessageStream:
    @pytest.mark.asyncio
    async def test_streams_chunks(self):
        provider = MockProvider([
            AIResponse(content="Hello world", tool_calls=[], usage={"input": 10, "output": 5}),
        ])
        agent = _make_agent(provider)

        chunks = []

        async def on_chunk(text: str) -> None:
            chunks.append(text)

        result = await agent.handle_message_stream("Hi", "user1", "terminal", on_chunk=on_chunk)
        assert result == "Hello world"
        assert "Hello world" in chunks

    @pytest.mark.asyncio
    async def test_stream_resets_usage(self):
        provider = MockProvider([
            AIResponse(content="A", tool_calls=[], usage={"input": 10, "output": 5}),
            AIResponse(content="B", tool_calls=[], usage={"input": 20, "output": 10}),
        ])
        agent = _make_agent(provider)
        await agent.handle_message_stream("First", "user1", "terminal")
        assert agent.last_usage == {"input": 10, "output": 5}
        await agent.handle_message_stream("Second", "user1", "terminal")
        assert agent.last_usage == {"input": 20, "output": 10}


# ---------------------------------------------------------------------------
# Tests: tool result caching
# ---------------------------------------------------------------------------

class TestToolCache:
    def test_cache_key_deterministic(self):
        k1 = _cache_key("web_search", {"query": "python", "limit": 5})
        k2 = _cache_key("web_search", {"limit": 5, "query": "python"})
        assert k1 == k2  # Order independent

    def test_cache_key_different_args(self):
        k1 = _cache_key("web_search", {"query": "python"})
        k2 = _cache_key("web_search", {"query": "javascript"})
        assert k1 != k2

    def test_cache_key_different_tools(self):
        k1 = _cache_key("web_search", {"query": "python"})
        k2 = _cache_key("file_read", {"query": "python"})
        assert k1 != k2

    def test_cacheable_tools_set(self):
        assert "web_search" in _CACHEABLE_TOOLS
        assert "file_read" in _CACHEABLE_TOOLS
        assert "web_browse" in _CACHEABLE_TOOLS
        assert "shell" not in _CACHEABLE_TOOLS
        assert "file_write" not in _CACHEABLE_TOOLS
        assert "memory" not in _CACHEABLE_TOOLS
        assert "delegate" not in _CACHEABLE_TOOLS

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_second_call(self):
        """Same tool call twice in one message should only execute once."""
        counter = CounterTool()

        provider = MockProvider([
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="web_search", arguments={"query": "python"})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc2", name="web_search", arguments={"query": "python"})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(content="Done", tool_calls=[], usage={"input": 10, "output": 5}),
        ])
        agent = _make_agent(provider, [counter])
        await agent.handle_message("Search python twice", "user1", "terminal")
        assert counter.call_count == 1  # Second call should be cached

    @pytest.mark.asyncio
    async def test_cache_cleared_between_messages(self):
        """Cache is cleared on each new message."""
        counter = CounterTool()

        provider = MockProvider([
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="web_search", arguments={"query": "python"})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(content="First", tool_calls=[], usage={"input": 10, "output": 5}),
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc2", name="web_search", arguments={"query": "python"})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(content="Second", tool_calls=[], usage={"input": 10, "output": 5}),
        ])
        agent = _make_agent(provider, [counter])
        await agent.handle_message("First msg", "user1", "terminal")
        await agent.handle_message("Second msg", "user1", "terminal")
        assert counter.call_count == 2  # Each message gets its own execution

    @pytest.mark.asyncio
    async def test_non_cacheable_tool_always_executes(self):
        """Non-cacheable tools should always execute."""

        class ShellTool(Tool):
            name = "shell"
            description = "Run shell"
            parameters: dict[str, Any] = {"type": "object", "properties": {"command": {"type": "string"}}}

            def __init__(self):
                self.call_count = 0

            async def execute(self, args, context):
                self.call_count += 1
                return "ok"

        shell = ShellTool()
        provider = MockProvider([
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="shell", arguments={"command": "ls"})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc2", name="shell", arguments={"command": "ls"})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(content="Done", tool_calls=[], usage={"input": 10, "output": 5}),
        ])
        agent = _make_agent(provider, [shell])
        await agent.handle_message("Run ls twice", "user1", "terminal")
        assert shell.call_count == 2  # Both calls execute


# ---------------------------------------------------------------------------
# Tests: error classification
# ---------------------------------------------------------------------------

class TestClassifyError:
    def test_connect_error(self):
        import httpx
        msg = _classify_error(httpx.ConnectError("Connection refused"))
        assert "connect" in msg.lower()

    def test_timeout_error(self):
        import httpx
        msg = _classify_error(httpx.ReadTimeout("timeout"))
        assert "timed out" in msg.lower()

    def test_401_error(self):
        import httpx
        resp = httpx.Response(status_code=401, request=httpx.Request("POST", "https://api.example.com"))
        msg = _classify_error(httpx.HTTPStatusError("auth", request=resp.request, response=resp))
        assert "api key" in msg.lower()

    def test_429_error(self):
        import httpx
        resp = httpx.Response(status_code=429, request=httpx.Request("POST", "https://api.example.com"))
        msg = _classify_error(httpx.HTTPStatusError("rate limited", request=resp.request, response=resp))
        assert "rate limited" in msg.lower()

    def test_500_error(self):
        import httpx
        resp = httpx.Response(status_code=500, request=httpx.Request("POST", "https://api.example.com"))
        msg = _classify_error(httpx.HTTPStatusError("server error", request=resp.request, response=resp))
        assert "internal error" in msg.lower()

    def test_generic_error(self):
        msg = _classify_error(RuntimeError("something weird"))
        assert "something went wrong" in msg.lower()


# ---------------------------------------------------------------------------
# Tests: system prompt
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    def test_includes_persona(self):
        provider = MockProvider()
        agent = _make_agent(provider)
        prompt = agent._build_system_prompt("terminal")
        assert "MicroPaw" in prompt
        assert "friendly" in prompt

    def test_includes_platform_hint(self):
        provider = MockProvider()
        agent = _make_agent(provider)
        prompt = agent._build_system_prompt("telegram")
        assert "Telegram" in prompt

    def test_includes_tool_names(self):
        provider = MockProvider()
        agent = _make_agent(provider, [EchoTool()])
        prompt = agent._build_system_prompt("terminal")
        assert "echo" in prompt

    def test_includes_memories(self):
        provider = MockProvider()
        agent = _make_agent(provider)
        memories = [{"key": "favorite_color", "value": "blue"}]
        prompt = agent._build_system_prompt("terminal", memories)
        assert "favorite_color" in prompt
        assert "blue" in prompt

    def test_includes_extra_prompt(self):
        provider = MockProvider()
        config = Config()
        config.persona.system_prompt_extra = "Always respond in Spanish."
        registry = ToolRegistry()
        store = MagicMock()
        store.get_recent_memories = AsyncMock(return_value=[])
        agent = Agent(config, provider, registry, store)
        prompt = agent._build_system_prompt("terminal")
        assert "Spanish" in prompt

    def test_includes_location(self):
        provider = MockProvider()
        config = Config()
        config.persona.location = "San Francisco, CA"
        registry = ToolRegistry()
        store = MagicMock()
        store.get_recent_memories = AsyncMock(return_value=[])
        agent = Agent(config, provider, registry, store)
        prompt = agent._build_system_prompt("terminal")
        assert "San Francisco" in prompt
        assert "near me" in prompt

    def test_no_location_when_empty(self):
        provider = MockProvider()
        config = Config()
        registry = ToolRegistry()
        store = MagicMock()
        store.get_recent_memories = AsyncMock(return_value=[])
        agent = Agent(config, provider, registry, store)
        prompt = agent._build_system_prompt("terminal")
        assert "User's location:" not in prompt


class TestToolResult:
    @pytest.mark.asyncio
    async def test_tool_returning_tool_result(self):
        """Agent handles ToolResult with media from a tool."""
        from micropaw.providers.base import MediaItem
        from micropaw.tools.base import ToolResult

        class ScreenshotTool(Tool):
            name = "screenshot"
            description = "Take a screenshot"
            parameters: dict[str, Any] = {"type": "object", "properties": {}}

            async def execute(self, args, context):
                return ToolResult(
                    text="Screenshot of example.com",
                    media=[MediaItem(type="image", data=b"fakepng", mime_type="image/png")],
                )

        provider = MockProvider([
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="screenshot", arguments={})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(content="I can see the page.", tool_calls=[], usage={"input": 20, "output": 10}),
        ])
        agent = _make_agent(provider, tools=[ScreenshotTool()])
        result = await agent.handle_message("take a screenshot", "user1", "terminal")
        assert result == "I can see the page."
        # Verify tool message was saved with media
        save_calls = agent.store.save_message.call_args_list
        tool_msgs = [c for c in save_calls if c[0][1].role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0][0][1].content == "Screenshot of example.com"
        assert len(tool_msgs[0][0][1].media) == 1
        assert tool_msgs[0][0][1].media[0].mime_type == "image/png"

    @pytest.mark.asyncio
    async def test_tool_returning_string_still_works(self):
        """Existing tools returning str continue to work."""
        provider = MockProvider([
            AIResponse(
                content="",
                tool_calls=[ToolCall(id="tc1", name="echo", arguments={"text": "hello"})],
                usage={"input": 10, "output": 5},
            ),
            AIResponse(content="Got it.", tool_calls=[], usage={"input": 20, "output": 10}),
        ])
        agent = _make_agent(provider, tools=[EchoTool()])
        result = await agent.handle_message("echo hello", "user1", "terminal")
        assert result == "Got it."
        save_calls = agent.store.save_message.call_args_list
        tool_msgs = [c for c in save_calls if c[0][1].role == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0][0][1].content == "echo: hello"
        assert tool_msgs[0][0][1].media == []
