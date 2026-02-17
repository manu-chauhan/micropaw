"""Abstract channel interface and shared utilities."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Channel(ABC):
    """Abstract base class for messaging channels.

    In MicroPaw, channels are thin adapters: they receive messages from
    external platforms, publish them to the message bus, consume outbound
    replies from the bus, and deliver them back to the platform.
    """

    name: str

    @abstractmethod
    async def start(self) -> None:
        """Start the channel and begin listening for messages."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        ...

    @abstractmethod
    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a message to a conversation."""
        ...

    async def send_typing(self, chat_id: str) -> None:
        """Send a typing indicator. Override if supported."""
        pass


def chunk_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks that fit within max_len, preferring newline boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
