"""Tests for the gateway — message routing, rate limiting, message size validation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from micropaw.config import Config
from micropaw.gateway import Gateway, _RateLimiter

# ---------------------------------------------------------------------------
# Rate limiter (additional tests beyond test_providers.py)
# ---------------------------------------------------------------------------

class TestRateLimiterEdgeCases:
    def test_different_users_independent(self):
        rl = _RateLimiter(messages_per_minute=2, burst_limit=5)
        assert rl.check("user1") is True
        assert rl.check("user1") is True
        assert rl.check("user1") is False  # minute limit hit
        assert rl.check("user2") is True  # different user still allowed

    def test_burst_limit_under_minute_limit(self):
        rl = _RateLimiter(messages_per_minute=100, burst_limit=2)
        assert rl.check("user1") is True
        assert rl.check("user1") is True
        assert rl.check("user1") is False  # burst hit, even though minute has room


# ---------------------------------------------------------------------------
# Gateway message handling
# ---------------------------------------------------------------------------

class TestGatewayHandleMessage:
    def _make_gateway(self) -> Gateway:
        config = Config()
        config.rate_limit.messages_per_minute = 20
        config.rate_limit.burst_limit = 5
        config.context.max_message_size = 1000

        provider = MagicMock()
        registry = MagicMock()
        channels = []

        gw = Gateway(config, provider, registry, channels)
        gw.agent = MagicMock()
        gw.agent.handle_message = AsyncMock(return_value="AI response")
        gw.agent.handle_message_stream = AsyncMock(return_value="Streamed response")
        gw.agent.last_usage = {"input": 10, "output": 5}
        return gw

    @pytest.mark.asyncio
    async def test_handle_message_routes_to_agent(self):
        gw = self._make_gateway()
        result = await gw._handle_message("Hello", "user1", "terminal", None)
        assert result == "AI response"
        gw.agent.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_message_no_agent(self):
        gw = self._make_gateway()
        gw.agent = None
        result = await gw._handle_message("Hello", "user1", "terminal", None)
        assert "not initialized" in result.lower()

    @pytest.mark.asyncio
    async def test_handle_message_rate_limited(self):
        gw = self._make_gateway()
        gw._rate_limiter = _RateLimiter(messages_per_minute=1, burst_limit=1)
        await gw._handle_message("msg1", "user1", "terminal", None)
        result = await gw._handle_message("msg2", "user1", "terminal", None)
        assert "too fast" in result.lower()

    @pytest.mark.asyncio
    async def test_handle_message_too_long(self):
        gw = self._make_gateway()
        long_msg = "x" * 2000  # Over 1000 limit
        result = await gw._handle_message(long_msg, "user1", "terminal", None)
        assert "too long" in result.lower()

    @pytest.mark.asyncio
    async def test_handle_message_stream_routes_to_agent(self):
        gw = self._make_gateway()
        result = await gw._handle_message_stream("Hello", "user1", "terminal", None)
        assert result == "Streamed response"

    @pytest.mark.asyncio
    async def test_handle_message_stream_rate_limited(self):
        gw = self._make_gateway()
        gw._rate_limiter = _RateLimiter(messages_per_minute=1, burst_limit=1)
        await gw._handle_message_stream("msg1", "user1", "terminal", None)
        result = await gw._handle_message_stream("msg2", "user1", "terminal", None)
        assert "too fast" in result.lower()

    @pytest.mark.asyncio
    async def test_handle_message_stream_too_long(self):
        gw = self._make_gateway()
        long_msg = "x" * 2000
        result = await gw._handle_message_stream(long_msg, "user1", "terminal", None)
        assert "too long" in result.lower()


class TestGatewayTerminalConversation:
    def _make_gw(self):
        config = Config()
        gw = Gateway(config, MagicMock(), MagicMock(), [])
        gw.agent = MagicMock()
        gw.agent.conversations = MagicMock()
        gw.agent.conversations.new_channel_session = MagicMock()
        gw.agent.conversations.rejoin_channel = MagicMock()
        gw.agent.conversations.new_all_sessions = MagicMock()
        return gw

    @pytest.mark.asyncio
    async def test_on_new_branches_terminal(self):
        gw = self._make_gw()
        await gw._on_new()
        gw.agent.conversations.new_channel_session.assert_called_once()
        args = gw.agent.conversations.new_channel_session.call_args[0]
        assert args[0] == "terminal"
        assert args[1].startswith("dm:terminal:")

    @pytest.mark.asyncio
    async def test_on_rejoin_clears_terminal(self):
        gw = self._make_gw()
        await gw._on_rejoin()
        gw.agent.conversations.rejoin_channel.assert_called_once_with("terminal")

    @pytest.mark.asyncio
    async def test_on_newall_resets_all(self):
        gw = self._make_gw()
        await gw._on_newall()
        gw.agent.conversations.new_all_sessions.assert_called_once()
        args = gw.agent.conversations.new_all_sessions.call_args[0]
        assert args[0].startswith("dm:owner:")
