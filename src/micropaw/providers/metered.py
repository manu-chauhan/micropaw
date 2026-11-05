"""Budget wrapper for a provider: reserve before the call, settle in `finally`, record in the ledger."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from micropaw.memory.ledger import Totals, usage_tag
from micropaw.providers.base import AIProvider, AIResponse, Message, ToolDef
from micropaw.utils.prices import LOCAL_PROVIDERS, cost, is_local_provider

logger = logging.getLogger(__name__)

_RESERVE_OUTPUT_SHARE = 0.25  # of max_tokens, held for the answer


class BudgetExceeded(Exception):  # noqa: N818
    """The daily cap is reached. The message is a plain reply for the chat."""


def _request_tokens(messages: list[Message], tools: list[ToolDef] | None) -> int:
    chars = sum(len(m.content) + sum(len(json.dumps(tc.arguments)) for tc in m.tool_calls) for m in messages)
    chars += sum(len(t.name) + len(t.description) + len(json.dumps(t.parameters)) for t in tools or [])
    return chars // 4


class MeteredProvider(AIProvider):
    """Same interface as the provider it wraps; anything else is passed through."""

    def __init__(
        self, inner: AIProvider, *, agent: str, role: str, ledger: Any, budget: Any,
        agent_caps: dict[str, float] | None = None, chat: str = "",
    ) -> None:
        self.inner = inner
        self.agent = agent
        self.role = role
        self.ledger = ledger
        self.budget = budget
        self.agent_caps = agent_caps or {}
        self.chat_id = chat
        self.local = is_local_provider(inner)

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.inner.name

    @property
    def model(self) -> str:  # type: ignore[override]
        return self.inner.model

    def __getattr__(self, attr: str) -> Any:
        if attr == "inner":
            raise AttributeError(attr)
        return getattr(self.inner, attr)

    def _price(self, usage: dict[str, int]) -> float | None:
        if self.local:
            return 0.0
        return None if self.inner.name in LOCAL_PROVIDERS else cost(self.inner.name, self.inner.model, usage)

    def _caps(self) -> list[tuple[float, str]]:
        """(USD cap, agent name or "" for the global cap), only the ones that are set."""
        caps = [(float(self.budget.daily_usd or 0), ""), (float(self.agent_caps.get(self.agent) or 0), self.agent)]
        return [(cap, scope) for cap, scope in caps if cap]

    @staticmethod
    def _spent(today: dict[str, Totals], scope: str) -> float:
        return today.get(scope, Totals()).usd if scope else sum(t.usd for t in today.values())

    async def _reserve(self, messages: list[Message], tools: list[ToolDef] | None) -> tuple[int, int]:
        """Check today's spend plus calls in flight, then hold an estimate. Returns (hold, input tokens)."""
        est_in = _request_tokens(messages, tools)
        est_out = int(getattr(self.inner, "max_tokens", 4096) * _RESERVE_OUTPUT_SHARE)
        est_usd = self._price({"input": est_in, "output": est_out})
        caps = self._caps()
        async with self.ledger.lock:
            if caps:
                today = await self.ledger.today()
                if est_usd is None:
                    unpriced = sum(t.unpriced_tokens for t in today.values())
                    if unpriced + self.ledger.held()[1] + est_in + est_out > self.budget.daily_tokens:
                        raise BudgetExceeded(
                            f"Daily budget of {self.budget.daily_tokens:,} tokens reached for models without a "
                            "known price; it resets at midnight. Change budget.daily_tokens to raise it."
                        )
                elif est_usd > 0:
                    for cap, scope in caps:
                        if self._spent(today, scope) + self.ledger.held(scope or None)[0] + est_usd > cap:
                            where = f" for agent {scope}" if scope else ""
                            raise BudgetExceeded(
                                f"Daily budget of ${cap:.2f} reached{where}; it resets at midnight. "
                                f"Change {'the agent budget.daily_usd' if scope else 'budget.daily_usd'} to raise it."
                            )
            return self.ledger.hold(self.agent, est_usd or 0.0, est_in + est_out), est_in

    async def _settle(
        self, hold: int, est_in: int, usage: dict[str, int] | None, received_chars: int, call_made: bool,
    ) -> None:
        self.ledger.release(hold)
        if usage is None and not call_made:
            return  # failed before any tokens came back
        usage = dict(usage or {})
        if not usage.get("input"):
            usage["input"] = est_in
        if not usage.get("output"):
            usage["output"] = received_chars // 4
        usd = self._price(usage)
        chat, role = usage_tag.get() or (self.chat_id, self.role)
        try:
            await self.ledger.record(
                agent=self.agent, chat=chat, role=role, provider=self.inner.name,
                model=self.inner.model, input=usage["input"], cached=usage.get("cache_read", 0) or 0,
                output=usage["output"], usd=usd,
            )
            await self._warn(usd)
        except Exception as e:  # a ledger failure must not lose the reply
            logger.error("Could not record usage: %s", e)

    async def _warn(self, usd: float | None) -> None:
        line = float(self.budget.warn_at or 0)
        caps = self._caps()
        if not usd or not line or not caps:
            return
        today = await self.ledger.today()
        for cap, scope in caps:
            spent = self._spent(today, scope)
            if spent >= cap * line:
                who = f" for agent {scope}" if scope else ""
                self.ledger.warn_once(
                    scope,
                    f"Heads up: ${spent:.2f} of the ${cap:.2f} daily budget{who} is used. It resets at midnight.",
                )

    @property
    def _unbound(self) -> bool:
        return getattr(self.ledger, "target", self.ledger) is None

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        if self._unbound:  # no database yet (paw models test, setup): nothing to meter against
            return await self.inner.chat(messages, tools)
        hold, est_in = await self._reserve(messages, tools)
        response: AIResponse | None = None
        try:
            response = await self.inner.chat(messages, tools)
            return response
        finally:
            await self._settle(
                hold, est_in, response.usage if response else None,
                len(response.content) if response else 0, response is not None,
            )

    async def chat_stream(
        self, messages: list[Message], tools: list[ToolDef] | None = None,
    ) -> AsyncGenerator[str | AIResponse, None]:
        if self._unbound:
            async for item in self.inner.chat_stream(messages, tools):
                yield item
            return
        hold, est_in = await self._reserve(messages, tools)
        usage: dict[str, int] | None = None
        received = 0
        try:
            async for item in self.inner.chat_stream(messages, tools):
                if isinstance(item, AIResponse):
                    usage = item.usage
                else:
                    received += len(item)
                yield item
        finally:
            await self._settle(hold, est_in, usage, received, received > 0)

    async def close(self) -> None:
        await self.inner.close()


def meter(
    provider: AIProvider, *, agent: str, role: str, ledger: Any, budget: Any,
    agent_caps: dict[str, float] | None = None, chat: str = "",
) -> AIProvider:
    """Wrap a provider so every call is checked against the budget and written to the ledger."""
    return MeteredProvider(provider, agent=agent, role=role, ledger=ledger, budget=budget,
                           agent_caps=agent_caps, chat=chat)
