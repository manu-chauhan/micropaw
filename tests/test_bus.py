"""Tests for the async message bus."""

from __future__ import annotations

import pytest

from micropaw.bus import MessageBus, OutboundMessage


@pytest.fixture
def bus():
    return MessageBus(maxsize=10, timeout=0.1)


class TestMessageBus:
    async def test_outbound_round_trip(self, bus: MessageBus):
        msg = OutboundMessage(channel="terminal", chat_id="chat1", content="reply")
        await bus.publish_outbound(msg)
        received = await bus.consume_outbound()
        assert received is not None
        assert received.content == "reply"

    async def test_consume_timeout_returns_none(self, bus: MessageBus):
        result = await bus.consume_outbound()
        assert result is None

    async def test_close_prevents_publish(self, bus: MessageBus):
        bus.close()
        assert bus.closed is True
        with pytest.raises(RuntimeError, match="closed"):
            await bus.publish_outbound(OutboundMessage(channel="terminal", chat_id="chat1", content="hello"))

    async def test_queue_ordering(self, bus: MessageBus):
        for i in range(3):
            await bus.publish_outbound(OutboundMessage(channel="terminal", chat_id="chat1", content=f"msg-{i}"))
        for i in range(3):
            received = await bus.consume_outbound()
            assert received is not None
            assert received.content == f"msg-{i}"
