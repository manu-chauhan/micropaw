"""Async message bus connecting channels to the agent core.

The bus owns one bounded asyncio.Queue for outbound traffic (agent → channel).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_SIZE = 100
_DEFAULT_TIMEOUT = 1.0
_CLASS_TIMEOUT: Any = object()  # "use the bus's own timeout"; None means wait without one


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------

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
    """Async message bus with a bounded queue and backpressure.

    Usage::

        bus = MessageBus()
        await bus.publish_outbound(reply)
        reply = await bus.consume_outbound()
        bus.close()
    """

    def __init__(
        self,
        maxsize: int = _DEFAULT_QUEUE_SIZE,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=maxsize)
        self._timeout = timeout
        self._closed = False

    async def _consume(self, queue: asyncio.Queue, timeout: float | None) -> Any:
        if self._closed:
            return None
        if timeout is _CLASS_TIMEOUT:
            timeout = self._timeout
        if timeout is None:
            return await queue.get()  # no timer: an idle consumer never wakes the loop
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    # -- Outbound (agent → channel) --

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Enqueue an outbound message. Awaits if queue is full (backpressure)."""
        self._assert_open()
        await self._outbound.put(msg)
        logger.debug("outbound enqueued channel=%s chat=%s qsize=%d", msg.channel, msg.chat_id, self._outbound.qsize())

    async def consume_outbound(self, timeout: float | None = _CLASS_TIMEOUT) -> OutboundMessage | None:
        """Dequeue next outbound message, or None on timeout/closed. timeout=None waits until one arrives."""
        return await self._consume(self._outbound, timeout)

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
