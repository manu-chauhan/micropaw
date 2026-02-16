"""Failover provider — tries multiple providers in sequence until one succeeds."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from microclaw.providers.base import AIProvider, AIResponse, Message, ToolDef

logger = logging.getLogger(__name__)


class AllProvidersFailedError(Exception):
    """All providers in the failover chain failed."""


class FailoverProvider(AIProvider):
    name = "failover"

    def __init__(self, providers: list[AIProvider]) -> None:
        if not providers:
            raise ValueError("FailoverProvider requires at least one provider")
        self.providers = providers
        self.name = f"failover({','.join(p.name for p in providers)})"

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        errors: list[str] = []
        for provider in self.providers:
            try:
                return await provider.chat(messages, tools)
            except Exception as e:
                logger.warning("Provider %s failed: %s", provider.name, e)
                errors.append(f"{provider.name}: {e}")

        raise AllProvidersFailedError(f"All providers failed: {'; '.join(errors)}")

    async def chat_stream(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AsyncGenerator[str | AIResponse, None]:
        errors: list[str] = []
        for i, provider in enumerate(self.providers):
            try:
                if i == 0:
                    # Primary: stream directly for best latency
                    async for item in provider.chat_stream(messages, tools):
                        yield item
                    return
                else:
                    # Fallback: non-streaming to avoid partial content leaks
                    logger.info("Falling back to %s (non-streaming)", provider.name)
                    response = await provider.chat(messages, tools)
                    if response.content:
                        yield response.content
                    yield response
                    return
            except Exception as e:
                logger.warning("Provider %s failed: %s", provider.name, e)
                errors.append(f"{provider.name}: {e}")

        raise AllProvidersFailedError(f"All providers failed: {'; '.join(errors)}")
