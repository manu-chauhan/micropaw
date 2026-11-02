"""Abstract channel interface and shared utilities."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# (text, user_id, platform, channel_id, ...) -> reply. Both also take media=[MediaItem] (photos, voice).
MessageHandler = Callable[..., Awaitable[str]]
StreamHandler = Callable[..., Awaitable[str]]
# (platform, user_id) -> None if allowed, else the reply for the sender ("" = stay silent)
AccessCheck = Callable[[str, str], str | None]


class Channel(ABC):
    """Abstract base class for messaging channels."""

    name: str

    def __init__(self) -> None:
        self._handler: MessageHandler | None = None
        self._stream_handler: StreamHandler | None = None
        self._access: AccessCheck | None = None

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_stream_handler(self, handler: StreamHandler) -> None:
        self._stream_handler = handler

    def set_access(self, check: AccessCheck) -> None:
        self._access = check

    def check_access(self, user_id: str) -> str | None:
        """None if this sender may use the assistant, else the reply to send them ("" = silent)."""
        return self._access(self.name, str(user_id)) if self._access else None

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send_message(self, chat_id: str, text: str) -> None: ...

    async def send_typing(self, chat_id: str) -> None:
        pass


async def stream_response(
    handler: StreamHandler,
    text: str,
    user_id: str,
    platform: str,
    channel_id: str | None,
    edit_fn: Callable[[str], Awaitable[None]],
    send_fn: Callable[[str], Awaitable[None]],
    max_msg_len: int,
    edit_interval: float,
    media: list | None = None,
) -> None:
    """Handle streaming: accumulate chunks with throttled edits, then finalize."""
    accumulated = ""
    last_edit = 0.0

    async def on_chunk(chunk: str) -> None:
        nonlocal accumulated, last_edit
        accumulated += chunk
        now = time.monotonic()
        if now - last_edit >= edit_interval:
            last_edit = now
            try:
                await edit_fn(accumulated[:max_msg_len - 4] + " ...")
            except Exception as e:
                logger.debug("%s stream edit failed: %s", platform, e)

    response = await handler(text, user_id, platform, channel_id, on_chunk, media=media)

    for i, part in enumerate(chunk_text(response, max_msg_len)):
        try:
            if i == 0:
                await edit_fn(part)
            else:
                await send_fn(part)
        except Exception as e:
            logger.error("%s send error: %s", platform, e)


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
