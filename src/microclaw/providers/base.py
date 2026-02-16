"""Abstract AI provider interface and shared types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MediaItem:
    """An image or other media attached to a message."""
    type: str  # "image"
    data: bytes
    mime_type: str  # "image/png", "image/jpeg", etc.


@dataclass(slots=True)
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    media: list[MediaItem] = field(default_factory=list)


@dataclass(slots=True)
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class AIResponse:
    content: str
    tool_calls: list[ToolCall]
    usage: dict[str, int]  # {"input": N, "output": N}
    stop_reason: str = ""  # "end_turn", "tool_use", "max_tokens"


class AIProvider(ABC):
    """Abstract base class for AI providers.

    All providers normalize to the same Message/AIResponse/ToolCall types.
    """

    name: str

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> AIResponse:
        """Send messages and get a response, optionally with tool use."""
        ...

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> AsyncGenerator[str | AIResponse, None]:
        """Stream text deltas, then yield a final AIResponse.

        Default: falls back to non-streaming chat().
        Providers override for true streaming.
        """
        response = await self.chat(messages, tools)
        if response.content:
            yield response.content
        yield response
