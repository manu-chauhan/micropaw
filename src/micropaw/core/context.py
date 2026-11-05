"""Context window management — compacts long conversations by summarizing old messages."""

from __future__ import annotations

import json
import logging

from micropaw.providers.base import AIProvider, Message

logger = logging.getLogger(__name__)

KEEP_RECENT = 10  # Always keep the last N messages intact
COMPACT_AT = 0.6  # compact when history plus system prompt passes this share of the context window
KEEP_TAIL_SHARE = 0.4  # the kept recent messages should fit in this share of the window
SUMMARY_PROMPT = (
    "Summarize the following conversation history in at most 5 sentences. "
    "Focus on key facts, decisions, and context that would be important "
    "for continuing the conversation. Be concise. "
    "If an earlier summary is given, fold it into the new summary so nothing important is lost."
)


def estimate_tokens(messages: list[Message]) -> int:
    """chars / 4 of the text and tool-call arguments."""
    chars = sum(len(m.content) + sum(len(json.dumps(tc.arguments)) for tc in m.tool_calls) for m in messages)
    return chars // 4


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

    def needs_compaction(self, messages: list[Message], window_tokens: int = 0) -> bool:
        """Too many messages, or (given the model's window) history plus system prompt over 60% of it."""
        if sum(1 for m in messages if m.role != "system") > self.compaction_threshold:
            return True
        return bool(window_tokens) and estimate_tokens(messages) > COMPACT_AT * window_tokens

    @staticmethod
    def keep_count(messages: list[Message], window_tokens: int = 0) -> int:
        """How many recent messages to keep: KEEP_RECENT, fewer when they alone would crowd the window."""
        keep = KEEP_RECENT
        while window_tokens and keep > 2 and estimate_tokens(messages[-keep:]) > KEEP_TAIL_SHARE * window_tokens:
            keep -= 1
        return keep

    async def compact(
        self, messages: list[Message], provider: AIProvider, previous_summary: str | None = None,
        window_tokens: int = 0, force: bool = False,
    ) -> tuple[list[Message], str | None]:
        """Compact message history if it is too long (or `force`). The previous summary is folded in.

        Returns (compacted_messages, summary_text_or_None).
        """
        non_system = [m for m in messages if m.role != "system"]

        if not force and not self.needs_compaction(messages, window_tokens):
            return messages, None

        logger.info("Compacting context: %d messages (threshold: %d)", len(non_system), self.compaction_threshold)

        system_msgs = [m for m in messages if m.role == "system"]
        keep = self.keep_count(non_system, window_tokens)
        to_summarize = non_system[:-keep]
        to_keep = non_system[-keep:]
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
            return messages, None

        conversation_text = "\n".join(summary_parts)
        if previous_summary:
            conversation_text = f"Earlier summary: {previous_summary}\n\nNew messages:\n{conversation_text}"

        try:
            summary_response = await provider.chat([
                Message(role="system", content=SUMMARY_PROMPT),
                Message(role="user", content=conversation_text),
            ])
            summary = summary_response.content
            if not summary or not summary.strip():
                logger.warning("Context compaction returned empty summary, falling back to truncation.")
                return system_msgs + to_keep, None
        except Exception as e:
            logger.warning("Context compaction failed: %s — falling back to truncation.", e)
            return system_msgs + to_keep, None

        logger.info("Compacted %d messages into summary (%d chars)", len(to_summarize), len(summary))

        summary_msg = Message(role="system", content=f"Previous conversation summary: {summary}")
        return system_msgs + [summary_msg] + to_keep, summary
