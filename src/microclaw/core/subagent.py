"""Sub-agent — independent LLM loop with NO tool access."""

from __future__ import annotations

import logging

from microclaw.providers.base import AIProvider, Message

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 10


class SubAgent:
    """A lightweight agent that runs a task using an LLM with no tools.

    Security: tools=None is always passed to the provider, so a sub-agent
    physically cannot call tools, read files, or execute shell commands.
    """

    def __init__(self, provider: AIProvider, system_prompt: str | None = None) -> None:
        self.provider = provider
        self.system_prompt = system_prompt or "You are a helpful assistant. Complete the task concisely."

    async def run(self, task: str) -> str:
        """Run the task and return the result text."""
        messages: list[Message] = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=task),
        ]

        for i in range(_MAX_ITERATIONS):
            try:
                response = await self.provider.chat(messages, tools=None)
            except Exception as e:
                logger.error("SubAgent provider error on iteration %d: %s", i, e)
                return f"Sub-agent error: {e}"

            if response.content and response.content.strip():
                return response.content.strip()

            # Empty response — nudge the model (use non-empty content for Claude compatibility)
            logger.debug("SubAgent empty response on iteration %d, nudging", i)
            messages.append(Message(role="assistant", content="I need to think about this more."))
            messages.append(Message(role="user", content="Please provide your response."))

        return "Sub-agent reached maximum iterations without a response."
