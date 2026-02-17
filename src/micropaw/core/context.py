"""Context window management — compacts long conversations by summarizing old messages."""

from __future__ import annotations

import logging

from micropaw.providers.base import AIProvider, Message

logger = logging.getLogger(__name__)

KEEP_RECENT = 10  # Always keep the last N messages intact
SUMMARY_PROMPT = (
    "Summarize the following conversation history in 2-3 sentences. "
    "Focus on key facts, decisions, and context that would be important "
    "for continuing the conversation. Be concise."
)


class ContextManager:
    """Compacts conversation context when it grows too large.

    Strategy:
    1. Keep system prompt intact
    2. Keep the last KEEP_RECENT messages intact
    3. Summarize everything in between via the LLM
    """

    def __init__(self, max_history: int = 50, compaction_threshold: int = 30) -> None:
        self.max_history = max_history
        self.compaction_threshold = compaction_threshold

    async def compact(self, messages: list[Message], provider: AIProvider) -> list[Message]:
        """Compact message history if it exceeds the threshold."""
        non_system = [m for m in messages if m.role != "system"]

        if len(non_system) <= self.compaction_threshold:
            return messages

        logger.info("Compacting context: %d messages (threshold: %d)", len(non_system), self.compaction_threshold)

        system_msgs = [m for m in messages if m.role == "system"]
        user_assistant_msgs = [m for m in messages if m.role != "system"]

        to_summarize = user_assistant_msgs[:-KEEP_RECENT]
        to_keep = user_assistant_msgs[-KEEP_RECENT:]

        # Build summary text from old messages (skip tool messages for cleaner summary)
        summary_parts = []
        for msg in to_summarize:
            if msg.role == "tool":
                continue
            prefix = "User" if msg.role == "user" else "Assistant"
            content = msg.content[:500]
            if msg.tool_calls:
                tool_names = ", ".join(tc.name for tc in msg.tool_calls)
                summary_parts.append(f"{prefix}: [used tools: {tool_names}] {content}")
            else:
                summary_parts.append(f"{prefix}: {content}")

        if not summary_parts:
            return messages

        conversation_text = "\n".join(summary_parts)

        try:
            summary_response = await provider.chat([
                Message(role="system", content=SUMMARY_PROMPT),
                Message(role="user", content=conversation_text),
            ])
            summary = summary_response.content
            if not summary or not summary.strip():
                logger.warning("Context compaction returned empty summary, falling back to truncation.")
                return system_msgs + to_keep
        except Exception as e:
            logger.warning("Context compaction failed: %s — falling back to truncation.", e)
            return system_msgs + to_keep

        logger.info("Compacted %d messages into summary (%d chars)", len(to_summarize), len(summary))

        summary_msg = Message(role="system", content=f"Previous conversation summary: {summary}")
        return system_msgs + [summary_msg] + to_keep
