"""Tests for provider types and failover logic."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from micropaw.providers.base import AIProvider, AIResponse, Message, ToolCall, ToolDef, retry_on_transient
from micropaw.providers.failover import AllProvidersFailedError, FailoverProvider

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


class TestRetryOnTransient:
    async def test_succeeds_on_first_try(self):
        call_count = 0

        async def good_func():
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await retry_on_transient(good_func)
        assert result == "ok"
        assert call_count == 1

    async def test_non_retryable_error_raises_immediately(self):
        import httpx

        async def bad_func():
            resp = httpx.Response(status_code=401, request=httpx.Request("POST", "https://example.com"))
            raise httpx.HTTPStatusError("auth failed", request=resp.request, response=resp)

        with pytest.raises(httpx.HTTPStatusError):
            await retry_on_transient(bad_func)

    async def test_retries_on_429(self, monkeypatch):
        import httpx

        import micropaw.providers.base as base_mod

        # Speed up test by zeroing the delay
        monkeypatch.setattr(base_mod, "RETRY_BASE_DELAY", 0.0)

        call_count = 0

        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                resp = httpx.Response(status_code=429, request=httpx.Request("POST", "https://example.com"))
                raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
            return "recovered"

        result = await retry_on_transient(flaky_func)
        assert result == "recovered"
        assert call_count == 3


class TestContextCompaction:
    """Tests for context manager — history must never be deleted on failure."""

    async def test_compaction_short_history_returns_unchanged(self):
        """When non-system messages < KEEP_RECENT, nothing to summarize — returns unchanged."""
        from micropaw.core.context import ContextManager

        failing_provider = MockProvider(fail=True)
        ctx = ContextManager(max_history=5, compaction_threshold=3)

        messages = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="msg1"),
            Message(role="assistant", content="reply1"),
            Message(role="user", content="msg2"),
            Message(role="assistant", content="reply2"),
            Message(role="user", content="msg3"),
            Message(role="assistant", content="reply3"),
            Message(role="user", content="msg4"),
        ]

        result, summary = await ctx.compact(messages, failing_provider)
        # 7 non-system < KEEP_RECENT(10), so to_summarize is empty → returns unchanged
        assert len(result) == len(messages)
        assert result == messages
        assert summary is None

    async def test_compaction_fallback_truncation_on_provider_failure(self):
        """If the summarization provider fails, fall back to truncation (system + recent)."""
        from micropaw.core.context import KEEP_RECENT, ContextManager

        failing_provider = MockProvider(fail=True)
        ctx = ContextManager(max_history=50, compaction_threshold=3)

        messages = [Message(role="system", content="You are helpful.")]
        for i in range(20):
            messages.append(Message(role="user", content=f"msg{i}"))
            messages.append(Message(role="assistant", content=f"reply{i}"))

        result, summary = await ctx.compact(messages, failing_provider)
        # Fallback: system_msgs + to_keep (last KEEP_RECENT)
        assert len(result) == 1 + KEEP_RECENT  # 1 system + 10 recent
        assert result[0].content == "You are helpful."
        assert result[-1].content == "reply19"
        assert summary is None

    async def test_compaction_fallback_truncation_on_empty_summary(self):
        """If the LLM returns an empty summary, fall back to truncation."""
        from micropaw.core.context import KEEP_RECENT, ContextManager

        empty_provider = MockProvider(response="")
        ctx = ContextManager(max_history=50, compaction_threshold=3)

        messages = [Message(role="system", content="You are helpful.")]
        for i in range(20):
            messages.append(Message(role="user", content=f"msg{i}"))
            messages.append(Message(role="assistant", content=f"reply{i}"))

        result, summary = await ctx.compact(messages, empty_provider)
        assert len(result) == 1 + KEEP_RECENT
        assert result[-1].content == "reply19"
        assert summary is None

    async def test_compaction_works_when_provider_succeeds(self):
        """When summarization succeeds, messages should be compacted."""
        from micropaw.core.context import ContextManager

        good_provider = MockProvider(response="Summary of previous conversation.")
        ctx = ContextManager(max_history=50, compaction_threshold=3)

        messages = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="msg1"),
            Message(role="assistant", content="reply1"),
            Message(role="user", content="msg2"),
            Message(role="assistant", content="reply2"),
            Message(role="user", content="msg3"),
            Message(role="assistant", content="reply3"),
            Message(role="user", content="msg4"),
        ]

        result, _ = await ctx.compact(messages, good_provider)
        assert len(result) <= len(messages)

    async def test_compaction_below_threshold_returns_unchanged(self):
        """Messages below threshold should pass through unchanged."""
        from micropaw.core.context import ContextManager

        good_provider = MockProvider(response="Summary")
        ctx = ContextManager(max_history=50, compaction_threshold=100)

        messages = [
            Message(role="system", content="System"),
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        ]

        result, summary = await ctx.compact(messages, good_provider)
        assert result == messages
        assert summary is None


class TestGeminiStreamingUsage:
    """Test that Gemini streaming extracts usageMetadata from final SSE chunk."""

    async def test_gemini_build_body_system_message(self):
        from micropaw.providers.gemini import GeminiProvider
        p = GeminiProvider(api_key="test-key")
        messages = [
            Message(role="system", content="You are helpful"),
            Message(role="user", content="Hi"),
        ]
        body = p._build_body(messages, None)
        assert "systemInstruction" in body
        assert body["systemInstruction"]["parts"][0]["text"] == "You are helpful"
        assert len(body["contents"]) == 1
        assert body["contents"][0]["role"] == "user"

    async def test_gemini_build_body_tool_calls(self):
        from micropaw.providers.gemini import GeminiProvider
        p = GeminiProvider(api_key="test-key")
        messages = [
            Message(role="user", content="Search python"),
            Message(role="assistant", content="", tool_calls=[
                ToolCall(id="tc1", name="web_search", arguments={"query": "python"}),
            ]),
            Message(role="tool", content="Results here", tool_call_id="tc1"),
        ]
        body = p._build_body(messages, None)
        # Assistant message should have functionCall part
        model_msg = body["contents"][1]
        assert model_msg["role"] == "model"
        assert any("functionCall" in p for p in model_msg["parts"])
        # Tool response should be user role with functionResponse
        tool_msg = body["contents"][2]
        assert tool_msg["role"] == "user"
        assert any("functionResponse" in p for p in tool_msg["parts"])

    async def test_gemini_parse_candidates_text(self):
        from micropaw.providers.gemini import GeminiProvider
        p = GeminiProvider(api_key="test-key")
        data = {
            "candidates": [{
                "content": {"parts": [{"text": "Hello world"}]},
            }]
        }
        content, tool_calls = p._parse_candidates(data)
        assert content == "Hello world"
        assert tool_calls == []

    async def test_gemini_parse_candidates_function_call(self):
        from micropaw.providers.gemini import GeminiProvider
        p = GeminiProvider(api_key="test-key")
        data = {
            "candidates": [{
                "content": {"parts": [{"functionCall": {"name": "web_search", "args": {"query": "test"}}}]},
            }]
        }
        content, tool_calls = p._parse_candidates(data)
        assert content == ""
        assert len(tool_calls) == 1
        assert tool_calls[0].name == "web_search"

    async def test_gemini_non_streaming_usage(self):
        """Non-streaming should extract usageMetadata."""
        from unittest.mock import AsyncMock, MagicMock

        from micropaw.providers.gemini import GeminiProvider

        p = GeminiProvider(api_key="test-key")
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "Hello"}]}}],
            "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 10},
        }
        mock_response.raise_for_status = MagicMock()
        p._client = MagicMock()
        p._client.post = AsyncMock(return_value=mock_response)

        resp = await p.chat([Message(role="user", content="Hi")])
        assert resp.usage == {"input": 42, "output": 10}


class TestClaudeMessageBuilding:
    """Test Claude provider message format building."""

    def test_system_message_extracted(self):
        from micropaw.providers.claude import ClaudeProvider
        p = ClaudeProvider(api_key="test-key")
        messages = [
            Message(role="system", content="Be helpful"),
            Message(role="user", content="Hi"),
        ]
        body = p._build_body(messages, None)
        assert body["system"] == "Be helpful"
        assert len(body["messages"]) == 1  # system extracted

    def test_tool_call_format(self):
        from micropaw.providers.claude import ClaudeProvider
        p = ClaudeProvider(api_key="test-key")
        messages = [
            Message(role="user", content="Search"),
            Message(role="assistant", content="Searching...", tool_calls=[
                ToolCall(id="tc1", name="web_search", arguments={"query": "python"}),
            ]),
            Message(role="tool", content="Results", tool_call_id="tc1"),
        ]
        body = p._build_body(messages, None)
        # Assistant message should have tool_use block
        assistant = body["messages"][1]
        assert assistant["role"] == "assistant"
        assert any(b["type"] == "tool_use" for b in assistant["content"])
        # Tool result
        tool_msg = body["messages"][2]
        assert tool_msg["role"] == "user"
        assert tool_msg["content"][0]["type"] == "tool_result"

    def test_tools_format(self):
        from micropaw.providers.claude import ClaudeProvider
        p = ClaudeProvider(api_key="test-key")
        tools = [ToolDef(name="search", description="Search web", parameters={"type": "object"})]
        body = p._build_body([Message(role="user", content="Hi")], tools)
        assert "tools" in body
        assert body["tools"][0]["name"] == "search"
        assert "input_schema" in body["tools"][0]


class TestOpenAIMessageBuilding:
    """Test OpenAI provider message format building."""

    def test_system_message_included(self):
        from micropaw.providers.openai import OpenAIProvider
        p = OpenAIProvider(api_key="test-key")
        messages = [
            Message(role="system", content="Be helpful"),
            Message(role="user", content="Hi"),
        ]
        body = p._build_body(messages, None)
        assert body["messages"][0] == {"role": "system", "content": "Be helpful"}

    def test_tool_call_format(self):
        from micropaw.providers.openai import OpenAIProvider
        p = OpenAIProvider(api_key="test-key")
        messages = [
            Message(role="user", content="Search"),
            Message(role="assistant", content="Searching...", tool_calls=[
                ToolCall(id="tc1", name="web_search", arguments={"query": "python"}),
            ]),
            Message(role="tool", content="Results", tool_call_id="tc1"),
        ]
        body = p._build_body(messages, None)
        assistant = body["messages"][1]
        assert assistant["role"] == "assistant"
        assert assistant["tool_calls"][0]["function"]["name"] == "web_search"
        tool_msg = body["messages"][2]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "tc1"

    def test_tools_format(self):
        from micropaw.providers.openai import OpenAIProvider
        p = OpenAIProvider(api_key="test-key")
        tools = [ToolDef(name="search", description="Search web", parameters={"type": "object"})]
        body = p._build_body([Message(role="user", content="Hi")], tools)
        assert "tools" in body
        assert body["tools"][0]["function"]["name"] == "search"


class TestRateLimiter:
    def test_allows_normal_traffic(self):
        from micropaw.gateway import _RateLimiter
        rl = _RateLimiter(messages_per_minute=20, burst_limit=5)
        for _ in range(5):
            assert rl.check("user1") is True

    def test_blocks_burst(self):
        from micropaw.gateway import _RateLimiter
        rl = _RateLimiter(messages_per_minute=20, burst_limit=3)
        for _ in range(3):
            assert rl.check("user1") is True
        assert rl.check("user1") is False  # burst exceeded

    def test_separate_users(self):
        from micropaw.gateway import _RateLimiter
        rl = _RateLimiter(messages_per_minute=20, burst_limit=3)
        for _ in range(3):
            rl.check("user1")
        assert rl.check("user1") is False
        assert rl.check("user2") is True  # different user, not limited
