"""Delegate tool — lets the main agent spin up sub-agents for tasks."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from microclaw.bus import MessageBus, OutboundMessage
from microclaw.core.subagent import SubAgent
from microclaw.providers.base import AIProvider
from microclaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

# Injected at startup
_provider: AIProvider | None = None
_bus: MessageBus | None = None


def set_delegate_deps(provider: AIProvider, bus: MessageBus | None = None) -> None:
    global _provider, _bus
    _provider = provider
    _bus = bus


class DelegateTool(Tool):
    name = "delegate"
    description = (
        "Delegate a sub-task to an independent AI agent. The sub-agent has no tool access "
        "and works purely with text. Use 'inline' mode for immediate results or 'background' "
        "to run asynchronously."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task to delegate to the sub-agent.",
            },
            "mode": {
                "type": "string",
                "description": "Execution mode: 'inline' (wait for result) or 'background' (fire-and-forget).",
                "enum": ["inline", "background"],
                "default": "inline",
            },
            "system_prompt": {
                "type": "string",
                "description": "Optional custom system prompt for the sub-agent.",
            },
        },
        "required": ["task"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if _provider is None:
            return "Error: Delegate tool not initialized."

        task = args["task"]
        mode = args.get("mode", "inline")
        system_prompt = args.get("system_prompt")

        agent = SubAgent(_provider, system_prompt=system_prompt)

        if mode == "background":
            if _bus is None:
                return "Error: Background mode requires message bus (not available)."
            chat_id = f"{context.platform}:{context.conversation_id}"
            asyncio.create_task(_run_background(agent, task, chat_id))
            return f"Task delegated in background: {task[:80]}"

        # Inline: block until done
        result = await agent.run(task)
        return result


async def _run_background(agent: SubAgent, task: str, chat_id: str) -> None:
    """Run a sub-agent in the background and publish result to bus."""
    try:
        result = await agent.run(task)
        if _bus and not _bus.closed:
            await _bus.publish_outbound(OutboundMessage(
                channel=chat_id.split(":")[0],
                chat_id=chat_id,
                content=f"[Background task complete]\n{result}",
            ))
    except Exception as e:
        logger.error("Background sub-agent failed: %s", e)
