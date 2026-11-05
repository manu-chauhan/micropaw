"""Delegate tool — lets the main agent spin up sub-agents for tasks."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

from micropaw.bus import MessageBus, OutboundMessage
from micropaw.core.subagent import SubAgent
from micropaw.providers.base import AIProvider
from micropaw.tools.base import Tool, ToolContext, ToolRegistry
from micropaw.utils.prices import is_local_provider

logger = logging.getLogger(__name__)

# Injected at startup
_provider: AIProvider | None = None
_bus: MessageBus | None = None
_registry: ToolRegistry | None = None
_max_iterations: int = 25
_MAX_BACKGROUND = 2  # sub-agent loops at once: more only slow every model call on a small board
_slots = asyncio.Semaphore(_MAX_BACKGROUND)
_tasks: set[asyncio.Task] = set()  # asyncio keeps only weak references to tasks


def set_delegate_deps(
    provider: AIProvider,
    bus: MessageBus | None = None,
    registry: ToolRegistry | None = None,
    max_iterations: int = 25,
) -> None:
    global _provider, _bus, _registry, _max_iterations
    _provider = provider
    _bus = bus
    _registry = registry
    _max_iterations = max_iterations


class DelegateTool(Tool):
    name = "delegate"
    description = (
        "Delegate a sub-task to an independent AI agent. The sub-agent can use all tools "
        "except delegate (no recursive spawning). Use 'inline' mode for immediate results "
        "or 'background' to run asynchronously."
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
        },
        "required": ["task"],
    }

    def __init__(self, provider: AIProvider | None = None, registry: ToolRegistry | None = None) -> None:
        self._provider = provider  # set per agent (its fast model and its tools); None = the shared ones below
        self._registry = registry

    def bound(self, provider: AIProvider, registry: ToolRegistry) -> DelegateTool:
        """A copy that uses this agent's fast model and its tool view."""
        return DelegateTool(provider, registry)

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        provider = self._provider or _provider
        registry = self._registry or _registry
        if provider is None:
            return "Error: Delegate tool not initialized."

        task = args["task"]
        mode = args.get("mode", "inline")

        if mode == "background":
            if _bus is None:
                return "Error: Background mode requires message bus (not available)."
            # Runs after this turn ends: nobody is there to approve, and it gets its own
            # browser session so the main turn closing its page doesn't break it.
            bg_context = dataclasses.replace(
                context, approve=None, conversation_id=f"{context.conversation_id}:bg",
                local_data=context.local_data and is_local_provider(provider),
            )
            agent = SubAgent(provider, registry=registry, tool_context=bg_context, max_iterations=_max_iterations)
            job = asyncio.create_task(_run_background(agent, task, context.platform, context.chat_id))
            _tasks.add(job)
            job.add_done_callback(_tasks.discard)
            return f"Task delegated in background: {task[:80]}"

        # Inline: block until done. Shares the turn's context, so approvals and the mark carry over.
        inline = dataclasses.replace(context, local_data=context.local_data and is_local_provider(provider))
        agent = SubAgent(provider, registry=registry, tool_context=inline, max_iterations=_max_iterations)
        return await agent.run(task)


async def _run_background(agent: SubAgent, task: str, channel: str, chat_id: str) -> None:
    """Run a sub-agent in the background and publish result to bus."""
    try:
        async with _slots:
            result = await agent.run(task)
        if _bus and not _bus.closed:
            await _bus.publish_outbound(OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=f"[Background task complete]\n{result}",
            ))
    except Exception as e:
        logger.error("Background sub-agent failed: %s", e)


async def cancel_background() -> None:
    """Stop sub-agents still running (the gateway is stopping)."""
    for job in list(_tasks):
        job.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
