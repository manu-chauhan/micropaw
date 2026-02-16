"""Async message bus connecting channels to the agent core.

The bus owns two bounded asyncio.Queue instances — one for inbound traffic
(channel → agent) and one for outbound traffic (agent → channel).
Channels publish inbound, consume outbound; the agent does the reverse.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_SIZE = 100
_DEFAULT_TIMEOUT = 1.0


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class InboundMessage:
    """A message from an external channel destined for the agent."""
    channel: str
    sender_id: str
    chat_id: str
    content: str
    session_key: str  # typically "<channel>:<chat_id>"
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutboundMessage:
    """A message from the agent to be delivered via a channel."""
    channel: str
    chat_id: str
    content: str


# ---------------------------------------------------------------------------
# Bus
# ---------------------------------------------------------------------------

class MessageBus:
    """Async message bus with bounded queues and backpressure.

    Usage::

        bus = MessageBus()

        # Channel publishes user message:
        await bus.publish_inbound(msg)

        # Agent consumes it:
        msg = await bus.consume_inbound()

        # Agent publishes reply:
        await bus.publish_outbound(reply)

        # Channel picks it up:
        reply = await bus.consume_outbound()

        bus.close()
    """

    def __init__(
        self,
        maxsize: int = _DEFAULT_QUEUE_SIZE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=maxsize)
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=maxsize)
        self._timeout = timeout
        self._closed = False

    # -- Inbound (channel → agent) --

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Enqueue an inbound message. Awaits if queue is full (backpressure)."""
        self._assert_open()
        await self._inbound.put(msg)
        logger.debug("inbound enqueued channel=%s chat=%s qsize=%d", msg.channel, msg.chat_id, self._inbound.qsize())

    async def consume_inbound(self) -> InboundMessage | None:
        """Dequeue next inbound message, or None on timeout."""
        try:
            return await asyncio.wait_for(self._inbound.get(), timeout=self._timeout)
        except asyncio.TimeoutError:
            return None

    # -- Outbound (agent → channel) --

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Enqueue an outbound message. Awaits if queue is full (backpressure)."""
        self._assert_open()
        await self._outbound.put(msg)
        logger.debug("outbound enqueued channel=%s chat=%s qsize=%d", msg.channel, msg.chat_id, self._outbound.qsize())

    async def consume_outbound(self) -> OutboundMessage | None:
        """Dequeue next outbound message, or None on timeout."""
        try:
            return await asyncio.wait_for(self._outbound.get(), timeout=self._timeout)
        except asyncio.TimeoutError:
            return None

    # -- Lifecycle --

    def close(self) -> None:
        """Mark the bus as closed. Further publishes will raise RuntimeError."""
        self._closed = True
        logger.debug("message bus closed")

    @property
    def closed(self) -> bool:
        return self._closed

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("cannot publish on a closed MessageBus")
