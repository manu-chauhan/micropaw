"""Sub-agent — independent LLM loop that can use tools (except delegate)."""

from __future__ import annotations

import logging
from typing import Any

from micropaw.providers.base import AIProvider, Message
from micropaw.tools.base import Tool, ToolContext, ToolDef, ToolRegistry

logger = logging.getLogger(__name__)

# Tools a sub-agent must never have (prevents recursive delegation)
_EXCLUDED_TOOLS = frozenset({"delegate"})

_DEFAULT_MAX_ITERATIONS = 25


class SubAgent:
    """A lightweight agent that runs a task with tools but no delegation.

    Sub-agents inherit the same tool registry and permissions as the main agent,
    except the ``delegate`` tool is always excluded to prevent recursive spawning.
    """

    def __init__(
        self,
        provider: AIProvider,
        system_prompt: str | None = None,
        registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    ) -> None:
        self.provider = provider
        self.max_iterations = max_iterations

        # Build tool defs from registry, excluding delegate
        self._tools: dict[str, Tool] = {}
        self._tool_defs: list[ToolDef] = []
        if registry:
            for tool in registry.all():
                if tool.name not in _EXCLUDED_TOOLS:
                    self._tools[tool.name] = tool
                    self._tool_defs.append(tool.to_tool_def())

        self._tool_context = tool_context

        # Build system prompt — include available tools so the sub-agent knows
        if system_prompt:
            self.system_prompt = system_prompt
        else:
            parts = ["You are a focused AI sub-agent. Complete the task concisely and thoroughly."]
            if self._tools:
                tool_list = ", ".join(sorted(self._tools.keys()))
                parts.append(f"You have access to these tools: {tool_list}")
                parts.append("Use tools when they help. Return your final answer as text.")
            self.system_prompt = "\n".join(parts)

    async def run(self, task: str) -> str:
        """Run the task and return the result text."""
        messages: list[Message] = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=task),
        ]

        tools = self._tool_defs or None

        for i in range(self.max_iterations):
            try:
                response = await self.provider.chat(messages, tools=tools)
            except Exception as e:
                logger.error("SubAgent provider error on iteration %d: %s", i, e)
                return f"Sub-agent error: {e}"

            # Tool calls — execute them and continue the loop
            if response.tool_calls and self._tools and self._tool_context:
                messages.append(Message(
                    role="assistant", content=response.content,
                    tool_calls=response.tool_calls,
                ))
                for tc in response.tool_calls:
                    tool = self._tools.get(tc.name)
                    if tool:
                        logger.info("[subagent][%s] %s", tc.name, _short_args(tc.arguments))
                        try:
                            result = await tool.execute(tc.arguments, self._tool_context)
                        except Exception as e:
                            result = f"Tool error: {e}"
                    else:
                        result = f"Unknown tool: {tc.name}"
                    messages.append(Message(role="tool", content=result, tool_call_id=tc.id))
                continue

            if response.content and response.content.strip():
                return response.content.strip()

            # Empty response — nudge the model (use non-empty content for Claude compatibility)
            logger.debug("SubAgent empty response on iteration %d, nudging", i)
            messages.append(Message(role="assistant", content="I need to think about this more."))
            messages.append(Message(role="user", content="Please provide your response."))

        return "Sub-agent reached maximum iterations without a response."


def _short_args(args: dict[str, Any]) -> str:
    from micropaw.core.agent import _summarize_args
    return _summarize_args(args)
