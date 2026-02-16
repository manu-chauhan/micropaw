"""Abstract tool interface and tool registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from microclaw.providers.base import ToolDef


@dataclass(slots=True)
class ToolContext:
    """Context passed to every tool execution."""
    conversation_id: str
    user_id: str
    platform: str
    allow_shell: bool = False
    allow_file_write: bool = False
    allow_web_browse: bool = True
    confirm_dangerous: bool = True


class Tool(ABC):
    """Abstract base class for tools the agent can invoke."""

    name: str
    description: str
    parameters: dict[str, Any]

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
