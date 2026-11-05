"""Fallback chain: try the next provider only when the error says another one could succeed."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator

import httpx

from micropaw.providers.base import NO_RETRY, AIProvider, AIResponse, Message, ToolDef
from micropaw.providers.metered import BudgetExceeded

logger = logging.getLogger(__name__)

COOLDOWN = 60.0  # seconds a failed provider is skipped (flat)
# Bad key, out of credit, model gone and similar: the next provider has its own key and model.
_FALL_THROUGH = frozenset({401, 402, 403, 404, 408, 409, 425, 429})
# A 400 or 413 is the request itself and would fail everywhere, unless the body blames the key or the account
# (Gemini answers 400 for a bad key, and some providers 400 for no credit).
_ACCOUNT_WORDS = ("api key", "credit", "billing", "quota")
_CONTEXT_WORDS = (
    "context length", "context_length", "context window", "maximum context", "context size",
    "too many tokens", "prompt is too long", "exceeds the available context",
)


def _now() -> float:
    return time.monotonic()


class AllProvidersFailedError(Exception):
    """All providers in the failover chain failed."""


class FriendlyError(Exception):
    """A provider error with a message fit for the chat. The original is kept for classification."""

    def __init__(self, message: str, original: Exception) -> None:
        super().__init__(message)
        self.original = original


def unwrap(e: Exception) -> Exception:
    while isinstance(e, FriendlyError):
        e = e.original
    return e


def classify(e: Exception) -> str | None:
    """Short reason when another provider could help ("timed out"), None when the request itself is bad."""
    e = unwrap(e)
    if isinstance(e, BudgetExceeded):
        return "has reached its daily budget"
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status in _FALL_THROUGH or status >= 500 or _account_problem(e):
            return f"returned HTTP {status}"
        return None
    if isinstance(e, httpx.TimeoutException):
        return "timed out"
    if isinstance(e, httpx.TransportError):
        return "could not be reached"
    return "failed"


def _body(e: httpx.HTTPStatusError) -> str:
    try:
        return e.response.text.lower()
    except httpx.ResponseNotRead:
        return ""


def _account_problem(e: httpx.HTTPStatusError) -> bool:
    if e.response.status_code != 400:
        return False
    text = _body(e)
    return any(w in text for w in _ACCOUNT_WORDS) and not is_context_error(e)


def is_context_error(e: Exception) -> bool:
    e = unwrap(e)
    if not isinstance(e, httpx.HTTPStatusError) or e.response.status_code != 400:
        return False
    return any(w in _body(e) for w in _CONTEXT_WORDS)


def trim_messages(messages: list[Message]) -> list[Message] | None:
    """Drop the older half of the conversation (keeping the system prompt and the newest message)."""
    from micropaw.core.conversation import sanitize_history

    system = [m for m in messages if m.role == "system"]
    rest = [m for m in messages if m.role != "system"]
    if len(rest) < 3:
        return None
    kept = sanitize_history(rest[max(1, (len(rest) - 1) // 2):])
    return [*system, *kept] if kept else None


def _label(p: AIProvider) -> str:
    model = getattr(p, "model", "")
    return f"{p.name}:{model}" if model else p.name


def _shared(p: AIProvider) -> AIProvider:
    """The provider under a metering wrapper, so every agent that uses it shares one cooldown."""
    return p.__dict__.get("inner", p)


def _cooling(p: AIProvider) -> bool:
    return getattr(_shared(p), "_down_until", 0.0) > _now()


def _mark_down(p: AIProvider, why: str) -> None:
    if why.endswith("budget"):
        return  # the cap is checked on every call, no cooldown needed
    p = _shared(p)
    p._down_until = _now() + COOLDOWN  # type: ignore[attr-defined]
    p._down_why = why  # type: ignore[attr-defined]


class FallbackProvider(AIProvider):
    """Providers in order. Each call starts at the first one that is not cooling down, so every
    new turn begins at the primary again. A provider that failed is skipped for COOLDOWN seconds."""

    name = "failover"

    def __init__(self, providers: list[AIProvider]) -> None:
        if not providers:
            raise ValueError("FailoverProvider requires at least one provider")
        self.providers = providers
        self.name = f"failover({','.join(p.name for p in providers)})"
        self.model = providers[0].model if hasattr(providers[0], "model") else ""

    def _active(self) -> list[AIProvider]:
        return [p for p in self.providers if not _cooling(p)] or list(self.providers)

    def _note(self, answered: AIProvider, failed: dict[int, str]) -> str:
        """Why the primary was not used, for the end of the reply. "" when it answered."""
        first = self.providers[0]
        if answered is first:
            return ""
        why = failed.get(id(first)) or getattr(_shared(first), "_down_why", "failed")
        return f"\n\n[answered by {_label(answered)} because {_label(first)} {why}]"

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        active = self._active()
        failed: dict[int, str] = {}
        last: Exception | None = None
        for i, provider in enumerate(active):
            token = NO_RETRY.set(i < len(active) - 1)
            try:
                response = await provider.chat(messages, tools)
            except Exception as e:
                why = classify(e)
                if why is None:
                    raise
                logger.warning("Provider %s failed: %s. Trying next.", provider.name, e)
                _mark_down(provider, why)
                failed[id(provider)] = why
                last = e
                continue
            finally:
                NO_RETRY.reset(token)
            if not response.tool_calls:
                response.note = self._note(provider, failed)
            return response
        if isinstance(last, BudgetExceeded):
            raise last  # the chat gets the plain budget message
        raise AllProvidersFailedError(f"All providers failed: {last}") from last

    async def chat_stream(
        self, messages: list[Message], tools: list[ToolDef] | None = None,
    ) -> AsyncGenerator[str | AIResponse, None]:
        active = self._active()
        failed: dict[int, str] = {}
        last: Exception | None = None
        for i, provider in enumerate(active):
            text = ""
            stream = provider.chat_stream(messages, tools)
            try:
                while True:
                    token = NO_RETRY.set(i < len(active) - 1)
                    try:
                        item = await stream.__anext__()
                    except StopAsyncIteration:
                        break
                    finally:
                        NO_RETRY.reset(token)
                    if isinstance(item, str):
                        text += item
                        yield item
                        continue
                    note = "" if item.tool_calls else self._note(provider, failed)
                    if note:
                        yield note
                        item.note = note
                    yield item
                    return
            except Exception as e:
                why = classify(e)
                if why is None:
                    raise
                logger.warning("Provider %s failed during streaming: %s", provider.name, e)
                _mark_down(provider, why)
                failed[id(provider)] = why
                last = e
                if text:  # the user has seen part of a reply: end it here, do not start another one
                    note = f"\n\n[reply cut short: {_label(provider)} {why}]"
                    yield note
                    yield AIResponse(content=text, tool_calls=[], usage={}, stop_reason="error", note=note)
                    return
            finally:
                await stream.aclose()
        if isinstance(last, BudgetExceeded):
            raise last  # the chat gets the plain budget message
        raise AllProvidersFailedError(f"All providers failed: {last}") from last

    async def close(self) -> None:
        for provider in self.providers:
            await provider.close()


FailoverProvider = FallbackProvider  # the v0.3 name
