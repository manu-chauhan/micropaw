"""Abstract channel interface and shared utilities."""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# (text, user_id, platform, channel_id, ...) -> reply. Both also take media=[MediaItem] (photos, voice).
MessageHandler = Callable[..., Awaitable[str]]
StreamHandler = Callable[..., Awaitable[str]]
# (platform, user_id) -> None if allowed, else the reply for the sender ("" = stay silent)
AccessCheck = Callable[[str, str], str | None]
# (text, user_id, channel_id) -> "Approved.", "Declined." or a refusal; None if the sender is not the owner
ApprovalAnswer = Callable[[str, str, str | None], str | None]
# (user_id, channel_id) -> True when this chat's replies to a voice note go out as voice
VoiceOn = Callable[[str, str | None], bool]
# (text, user_id, channel_id) -> (audio bytes, mime type), or None when synthesis failed
VoiceSay = Callable[[str, str, str | None], Awaitable["tuple[bytes, str] | None"]]

_BUTTON = re.compile(r"^ap:([0-9a-f]{4}):(yes|no|always)$")


def approval_hint(ask_id: str, allow_always: bool) -> str:
    always = f", or 'always {ask_id}' to allow this exact action from now on" if allow_always else ""
    return f"Reply 'yes {ask_id}' or 'no {ask_id}'{always}. This expires in 10 minutes."


def approval_buttons(ask_id: str, allow_always: bool) -> list[tuple[str, str]]:
    """(label, callback id) for each button. Always only when the ask allows a saved rule."""
    words = ("yes", "no", "always") if allow_always else ("yes", "no")
    return [(w.capitalize(), f"ap:{ask_id}:{w}") for w in words]


def settled_note(reply: str) -> str | None:
    """The line to put under an approval message once a button was used, None to leave the buttons."""
    return None if reply.startswith("'always'") else reply


class Channel(ABC):
    """Abstract base class for messaging channels."""

    name: str

    def __init__(self) -> None:
        self._handler: MessageHandler | None = None
        self._stream_handler: StreamHandler | None = None
        self._access: AccessCheck | None = None
        self._approval_answer: ApprovalAnswer | None = None
        self._voice_on: VoiceOn | None = None
        self._voice_say: VoiceSay | None = None

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_stream_handler(self, handler: StreamHandler) -> None:
        self._stream_handler = handler

    def set_access(self, check: AccessCheck) -> None:
        self._access = check

    def press_approval(self, data: str, user_id: str, channel_id: str | None) -> str | None:
        """Handle a button press. The reply from the approvals, or None if the pressing user is not
        the owner or the button is not ours. This never waits on the per-chat lock."""
        m = _BUTTON.match(data or "")
        if not m or not self._approval_answer:
            return None
        return self._approval_answer(f"{m.group(2)} {m.group(1)}", str(user_id), channel_id)

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

    async def send_approval(self, chat_id: str, text: str, ask_id: str, allow_always: bool) -> None:
        """Ask the owner. Channels with buttons override this; the owner can always type the reply."""
        await self.send_message(chat_id, f"{text}\n\n{approval_hint(ask_id, allow_always)}")


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
                await edit_fn(strip_images(accumulated)[:max_msg_len - 4] + " ...")
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


_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")


def strip_images(text: str) -> str:
    """Markdown images become their alt text. A chat app or a viewer that loads the image would send the URL
    (and anything the model put in it) to whoever runs that server."""
    return _MD_IMAGE.sub(lambda m: m.group(1) or "image", text)


def chunk_text(text: str, max_len: int) -> list[str]:
    """Split a reply into chunks that fit within max_len, preferring newline boundaries.
    Every outgoing reply passes through here, so images are stripped here."""
    text = strip_images(text)
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
