"""Conversation/thread manager."""

from __future__ import annotations

from microclaw.memory.store import MemoryStore
from microclaw.providers.base import Message

MAX_HISTORY = 40


class ConversationManager:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def get_conversation_id(self, platform: str, user_id: str, channel_id: str | None = None) -> str:
        """Build a conversation ID. Use channel for groups, user for DMs."""
        scope = channel_id or user_id
        return f"{platform}:{scope}"

    async def load_history(self, conversation_id: str, platform: str, user_id: str) -> list[Message]:
        await self.store.get_or_create_conversation(conversation_id, platform, user_id)
        return await self.store.get_messages(conversation_id, MAX_HISTORY)

    async def save_message(self, conversation_id: str, message: Message) -> None:
        await self.store.save_message(conversation_id, message)
