"""Tests for conversation manager and context compaction."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from micropaw.core.context import KEEP_RECENT, ContextManager
from micropaw.core.conversation import MAX_HISTORY, _DM_CONV_ID, ConversationManager
from micropaw.providers.base import AIResponse, Message, ToolCall

# ---------------------------------------------------------------------------
# ConversationManager
# ---------------------------------------------------------------------------

class TestConversationManager:
    def test_dm_unified_across_channels(self):
        """All DMs (channel_id=None) share a single conversation."""
        store = MagicMock()
        mgr = ConversationManager(store)
        assert mgr.get_conversation_id("telegram", "user123") == _DM_CONV_ID
        assert mgr.get_conversation_id("terminal", "terminal-user") == _DM_CONV_ID
        assert mgr.get_conversation_id("discord", "user789") == _DM_CONV_ID

    def test_get_conversation_id_group(self):
        store = MagicMock()
        mgr = ConversationManager(store)
        cid = mgr.get_conversation_id("discord", "user123", channel_id="channel456")
        assert cid == "discord:channel456"

    def test_group_isolated_from_dm(self):
        store = MagicMock()
        mgr = ConversationManager(store)
        group_cid = mgr.get_conversation_id("telegram", "user123", channel_id="-100123")
        dm_cid = mgr.get_conversation_id("telegram", "user123")
        assert group_cid != dm_cid
        assert group_cid == "telegram:-100123"

    def test_dm_override(self):
        store = MagicMock()
        mgr = ConversationManager(store)
        mgr.reset_dm_session("dm:owner:abc123")
        cid = mgr.get_conversation_id("terminal", "terminal-user")
        assert cid == "dm:owner:abc123"

    def test_dm_override_applies_across_channels(self):
        """After /new, the override applies to all DM channels."""
        store = MagicMock()
        mgr = ConversationManager(store)
        mgr.reset_dm_session("dm:owner:fresh")
        assert mgr.get_conversation_id("terminal", "terminal-user") == "dm:owner:fresh"
        assert mgr.get_conversation_id("telegram", "user123") == "dm:owner:fresh"

    def test_dm_override_doesnt_affect_groups(self):
        store = MagicMock()
        mgr = ConversationManager(store)
        mgr.reset_dm_session("dm:owner:override")
        cid = mgr.get_conversation_id("telegram", "user123", channel_id="-100456")
        assert cid == "telegram:-100456"

    @pytest.mark.asyncio
    async def test_load_history(self):
        store = MagicMock()
        store.get_or_create_conversation = AsyncMock()
        store.get_messages = AsyncMock(return_value=[
            Message(role="user", content="Hi"),
            Message(role="assistant", content="Hello"),
        ])
        mgr = ConversationManager(store)
        history = await mgr.load_history("conv1", "terminal", "user1")
        assert len(history) == 2
        store.get_messages.assert_called_once_with("conv1", MAX_HISTORY)

    @pytest.mark.asyncio
    async def test_save_message(self):
        store = MagicMock()
        store.save_message = AsyncMock()
        mgr = ConversationManager(store)
        msg = Message(role="user", content="Test")
        await mgr.save_message("conv1", msg)
        store.save_message.assert_called_once_with("conv1", msg)


# ---------------------------------------------------------------------------
# Context compaction
# ---------------------------------------------------------------------------

class _MockSummaryProvider:
    """Provider that returns a predictable summary."""
    name = "mock-summarizer"

    async def chat(self, messages, tools=None):
        return AIResponse(
            content="Summary: user discussed Python and testing.",
            tool_calls=[],
            usage={"input": 50, "output": 15},
        )


class TestContextCompaction:
    def test_keep_recent_constant(self):
        assert KEEP_RECENT == 10

    @pytest.mark.asyncio
    async def test_below_threshold_unchanged(self):
        ctx = ContextManager(compaction_threshold=100)
        messages = [
            Message(role="system", content="System"),
            Message(role="user", content="Hi"),
            Message(role="assistant", content="Hello"),
        ]
        result = await ctx.compact(messages, _MockSummaryProvider())
        assert result == messages

    @pytest.mark.asyncio
    async def test_compaction_creates_summary_message(self):
        ctx = ContextManager(compaction_threshold=3)
        messages = [
            Message(role="system", content="System prompt"),
        ]
        # Add enough messages to trigger compaction (> threshold + KEEP_RECENT)
        for i in range(20):
            messages.append(Message(role="user", content=f"User message {i}"))
            messages.append(Message(role="assistant", content=f"Assistant reply {i}"))

        result = await ctx.compact(messages, _MockSummaryProvider())
        # Should have: system + summary_system + last KEEP_RECENT
        assert len(result) < len(messages)
        # Check summary message exists
        summary_msgs = [m for m in result if "Previous conversation summary" in m.content]
        assert len(summary_msgs) == 1
        assert "Summary:" in summary_msgs[0].content

    @pytest.mark.asyncio
    async def test_compaction_keeps_system_messages(self):
        ctx = ContextManager(compaction_threshold=3)
        messages = [Message(role="system", content="System prompt")]
        for i in range(20):
            messages.append(Message(role="user", content=f"msg{i}"))
            messages.append(Message(role="assistant", content=f"reply{i}"))

        result = await ctx.compact(messages, _MockSummaryProvider())
        system_msgs = [m for m in result if m.role == "system"]
        assert any("System prompt" in m.content for m in system_msgs)

    @pytest.mark.asyncio
    async def test_compaction_preserves_recent(self):
        ctx = ContextManager(compaction_threshold=3)
        messages = [Message(role="system", content="System")]
        for i in range(20):
            messages.append(Message(role="user", content=f"msg{i}"))
            messages.append(Message(role="assistant", content=f"reply{i}"))

        result = await ctx.compact(messages, _MockSummaryProvider())
        # Last message should be the most recent one
        non_system = [m for m in result if m.role != "system"]
        assert non_system[-1].content == "reply19"

    @pytest.mark.asyncio
    async def test_compaction_skips_tool_messages_in_summary(self):
        """Tool messages should not be included in the summary text."""
        ctx = ContextManager(compaction_threshold=2)
        messages = [
            Message(role="system", content="System"),
            Message(role="user", content="msg1"),
            Message(role="assistant", content="thinking", tool_calls=[
                ToolCall(id="tc1", name="web_search", arguments={"query": "test"}),
            ]),
            Message(role="tool", content="search results here", tool_call_id="tc1"),
            Message(role="assistant", content="reply1"),
        ]
        # Add enough to trigger compaction
        for i in range(20):
            messages.append(Message(role="user", content=f"msg{i+2}"))
            messages.append(Message(role="assistant", content=f"reply{i+2}"))

        result = await ctx.compact(messages, _MockSummaryProvider())
        assert len(result) < len(messages)
