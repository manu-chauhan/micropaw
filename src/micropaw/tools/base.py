"""Abstract tool interface and tool registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from micropaw.providers.base import MediaItem, ToolDef


@dataclass(slots=True)
class ToolResult:
    """Structured result from a tool execution — text + optional media."""
    text: str
    media: list[MediaItem] = field(default_factory=list)


@dataclass(slots=True)
class ToolContext:
    """Context passed to every tool execution."""
    conversation_id: str
    user_id: str
    platform: str
    chat_id: str = ""  # transport-routable address (e.g. "telegram:12345")
    allow_shell: bool = False
    allow_file_write: bool = False
    allow_web_browse: bool = True
    confirm_dangerous: bool = True
    workspace: str = ""  # folder where writes need no approval; relative paths resolve here
    # True once the conversation has read outside content (web, email, files, shell output).
    # Shared with sub-agents, so a page read by a helper still counts.
    marked: bool = False
    memory_user: str = "_global"  # memory and document scope; an agent with its own history gets its own
    agent: str = "main"  # the agent this chat talks to; jobs it creates run on it later
    # False when the model answering is not local: what was saved in local or airgapped mode stays hidden from it
    local_data: bool = True
    # Asks the owner on the chat this request came from. None = nobody to ask (cron, heartbeat,
    # background tasks), so anything that needs approval is refused there.
    approve: Callable[[str, str], Awaitable[bool]] | None = None


class Tool(ABC):
    """Abstract base class for tools the agent can invoke."""

    name: str
    description: str
    parameters: dict[str, Any]
    # Output comes from outside (web, email, files, commands) and may carry instructions
    marks = False

    def needs_approval(self, args: dict[str, Any], context: ToolContext) -> str | None:
        """What to show the owner if this call needs a yes first, else None.

        Built by code from the real arguments, never from the model's own words.
        """
        return None

    @abstractmethod
    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        """Execute the tool and return a string result."""
        ...

    def to_tool_def(self) -> ToolDef:
        return ToolDef(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )


class ToolRegistry:
    """Registry of available tools, keyed by name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def to_tool_defs(self) -> list[ToolDef]:
        return [t.to_tool_def() for t in self._tools.values()]

    @property
    def names(self) -> list[str]:
        return list(self._tools.keys())


async def run_tool(tool: Tool, args: dict[str, Any], context: ToolContext) -> str | ToolResult:
    """Run one tool call with the approval gate. Every caller (agent, sub-agents) goes through here."""
    from micropaw.utils.audit import record
    from micropaw.utils.redact import redact

    ask = tool.needs_approval(args, context) or ""
    if ask:
        if context.approve is None:
            record(tool.name, args, "refused", context.conversation_id, ask)
            return f"Not done: {tool.name} needs the owner's approval and there is nobody to ask here."
        if not await context.approve(tool.name, ask):
            record(tool.name, args, "declined", context.conversation_id, ask)
            return f"Not done: the owner declined this {tool.name} call."
    outcome, text = "error", ""
    try:
        result = await tool.execute(args, context)
        text = redact(result.text if isinstance(result, ToolResult) else result)
        outcome = "error" if text.startswith(("Error", "Tool error")) else "ok"
        if isinstance(result, ToolResult):
            result.text = text
            return result
        return text
    finally:
        if tool.marks:
            context.marked = True
        record(tool.name, args, outcome, context.conversation_id, ask, text)
