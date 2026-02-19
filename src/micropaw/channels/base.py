"""Abstract channel interface and shared utilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

MessageHandler = Callable[[str, str, str, str | None], Awaitable[str]]
StreamHandler = Callable[[str, str, str, str | None, Callable[[str], Awaitable[None]] | None], Awaitable[str]]


class Channel(ABC):
    """Abstract base class for messaging channels."""

    name: str

    def __init__(self) -> None:
        self._handler: MessageHandler | None = None
        self._stream_handler: StreamHandler | None = None

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_stream_handler(self, handler: StreamHandler) -> None:
        self._stream_handler = handler

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_message(self, chat_id: str, text: str) -> None: ...

    async def send_typing(self, chat_id: str) -> None:
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
