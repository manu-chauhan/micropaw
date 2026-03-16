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
        self._dm_override: str | None = None

    def get_conversation_id(self, platform: str, user_id: str, channel_id: str | None = None) -> str:
        """Build a conversation ID.

        DMs (channel_id is None) share a single unified conversation across
        all channels.  Group chats keep platform-specific IDs.
        """
        # Groups: keep isolated per platform + channel
        if channel_id:
            return f"{platform}:{channel_id}"
        # DM override (/new command)
        if self._dm_override:
            return self._dm_override
        return _DM_CONV_ID

    async def load_history(self, conversation_id: str, platform: str, user_id: str) -> list[Message]:
        await self.store.get_or_create_conversation(conversation_id, platform, user_id)
        return await self.store.get_messages(conversation_id, MAX_HISTORY)

    async def save_message(self, conversation_id: str, message: Message) -> None:
        await self.store.save_message(conversation_id, message)

    def reset_dm_session(self, new_id: str) -> None:
        """Set a new conversation ID for DM sessions (/new command)."""
        self._dm_override = new_id
