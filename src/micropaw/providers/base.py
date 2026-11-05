"""Abstract AI provider interface and shared types."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from abc import ABC
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
CONNECT_TIMEOUT = 5.0  # an unreachable host fails fast instead of hanging for the read timeout

# Set by the fallback chain around a provider that has another one behind it: failing over beats sleeping.
NO_RETRY: contextvars.ContextVar[bool] = contextvars.ContextVar("micropaw_no_retry", default=False)


def http_timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(read, connect=CONNECT_TIMEOUT)


async def retry_on_transient(func, *args, **kwargs):
    """Retry an async function on transient HTTP errors (429, 503) with exponential backoff."""
    last_exc = None
    max_retries = 0 if NO_RETRY.get() else MAX_RETRIES
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            last_exc = e
            if e.response.status_code in (429, 503) and attempt < max_retries:
                # Check for Retry-After header
                retry_after = e.response.headers.get("retry-after")
                if retry_after:
                    try:
                        delay = min(float(retry_after), 30.0)
                    except ValueError:
                        delay = RETRY_BASE_DELAY * (2 ** attempt)
                else:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "Retrying after %s (attempt %d/%d, status %d)",
                    f"{delay:.1f}s", attempt + 1, max_retries, e.response.status_code,
                )
                await asyncio.sleep(delay)
            else:
                raise
    if last_exc:
        raise last_exc
    raise RuntimeError("retry_on_transient: all attempts exhausted with no captured exception")


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
    # Provider reasoning blocks (Claude "thinking") to send back unchanged within the same turn.
    # Never stored: earlier turns are rebuilt each time, and replaying a block after the
    # history changed is rejected.
    thinking: list[dict[str, Any]] = field(default_factory=list)


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
    thought_signature: str | None = None  # Gemini: echoed back unchanged on the next request


@dataclass(slots=True)
class AIResponse:
    content: str
    tool_calls: list[ToolCall]
    usage: dict[str, int]  # {"input": N, "output": N}
    stop_reason: str = ""  # "end_turn", "tool_use", "max_tokens", "refusal"
    thinking: list[dict[str, Any]] = field(default_factory=list)


class AIProvider(ABC):
    """Abstract base class for AI providers.

    All providers normalize to the same Message/AIResponse/ToolCall types.
    Subclasses implement _build_body, _do_chat, and _parse_response;
    the template chat() handles logging and retry.
    """

    name: str
    model: str

    def _build_body(self, messages: list[Message], tools: list[ToolDef] | None) -> dict:
        """Convert normalized messages/tools into the provider's API body format."""
        raise NotImplementedError

    async def _do_chat(self, body: dict) -> dict:
        """POST the body to the provider's API and return the raw JSON response."""
        raise NotImplementedError

    def _parse_response(self, data: dict) -> AIResponse:
        """Parse the provider's raw JSON response into an AIResponse."""
        raise NotImplementedError

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
    ) -> AIResponse:
        """Send messages and get a response, optionally with tool use."""
        body = self._build_body(messages, tools)
        logger.debug("Calling %s %s", self.name, self.model)
        data = await retry_on_transient(self._do_chat, body)
        return self._parse_response(data)

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

    async def close(self) -> None:
        """Close any HTTP clients held by this provider."""
        if hasattr(self, "_client"):
            await self._client.aclose()


def parse_sse(line: str, prefix: str = "data: ") -> dict | None:
    """Parse an SSE or NDJSON line into a dict, returning None on skip/error."""
    if prefix:
        if not line.startswith(prefix):
            return None
        raw = line[len(prefix):]
    else:
        raw = line.strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None
