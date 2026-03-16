"""Conversation/thread manager."""

from __future__ import annotations

from micropaw.memory.store import MemoryStore
from micropaw.providers.base import Message

MAX_HISTORY = 40

# Unified conversation key for all DMs across channels.
# Group chats keep their platform-specific keys.
_DM_CONV_ID = "dm:owner"


class ConversationManager:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self._global_override: str | None = None
        self._channel_overrides: dict[str, str] = {}  # platform → conv_id

    def get_conversation_id(self, platform: str, user_id: str, channel_id: str | None = None) -> str:
        """Build a conversation ID.

        DMs (channel_id is None) share a single unified conversation across
        all channels.  Group chats keep platform-specific IDs.

        Priority: group → per-channel override → global override → default.
        """
        if channel_id:
            return f"{platform}:{channel_id}"
        if platform in self._channel_overrides:
            return self._channel_overrides[platform]
        if self._global_override:
            return self._global_override
        return _DM_CONV_ID

    async def load_history(self, conversation_id: str, platform: str, user_id: str) -> list[Message]:
        await self.store.get_or_create_conversation(conversation_id, platform, user_id)
        return await self.store.get_messages(conversation_id, MAX_HISTORY)

    async def save_message(self, conversation_id: str, message: Message) -> None:
        await self.store.save_message(conversation_id, message)

    def new_channel_session(self, platform: str, new_id: str) -> None:
        """Branch this channel into a fresh conversation (/new)."""
        self._channel_overrides[platform] = new_id

    def rejoin_channel(self, platform: str) -> None:
        """Return this channel to the shared conversation (/rejoin)."""
        self._channel_overrides.pop(platform, None)

    def new_all_sessions(self, new_id: str) -> None:
        """Start a fresh conversation for all channels (/newall)."""
        self._channel_overrides.clear()
        self._global_override = new_id
