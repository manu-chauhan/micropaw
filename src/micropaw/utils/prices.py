"""Model prices in USD per 1M tokens, with the date each entry was read. Prices change: check the date.

Unknown model = None (never a guess). Local servers cost 0.
"""

from __future__ import annotations

from dataclasses import dataclass

LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio", "llamacpp", "vllm", "jan", "koboldcpp", "localai", "local"})


@dataclass(frozen=True, slots=True)
class Price:
    input: float
    output: float
    cache_read: float | None = None   # None = billed as input
    cache_write: float | None = None  # None = billed as input
    read: str = "2026-11-05"          # date the price was read
    limit: int | None = None          # prompt size where the rate changes
    over: Price | None = None         # rate above `limit`; None with a limit = unknown above it


def _claude(inp: float, out: float, cache_read: float, over: Price | None = None, limit: int | None = None) -> Price:
    return Price(inp, out, cache_read, round(inp * 1.25, 6), limit=limit, over=over)


_HAIKU_OVER = _claude(0.50, 2.50, 0.05)

PRICES: dict[tuple[str, str], Price] = {
    # Anthropic model docs, read 2026-11-05. Cache writes are the 5-minute rate (1.25 x input).
    ("claude", "claude-sonnet-5-5"): _claude(2, 10, 0.20),
    ("claude", "claude-sonnet-5"): _claude(2, 10, 0.20),
    ("claude", "claude-haiku-5-5"): _claude(0.10, 0.50, 0.01, over=_HAIKU_OVER, limit=100_000),
    ("claude", "claude-opus-5-5"): _claude(4, 20, 0.20),
    ("claude", "claude-opus-5"): _claude(5, 25, 0.5),
    ("claude", "claude-fable-5-1"): _claude(10, 50, 0.25),
    ("claude", "claude-fable-5"): _claude(10, 50, 1),
    # research/11 section 2, pages read 2026-11-05. Input / cached / output.
    ("xai", "grok-4.7"): Price(2.00, 6.00, 0.50, limit=200_000),
    ("xai", "grok-4.6"): Price(2.00, 6.00, 0.50, limit=200_000),
    ("xai", "grok-4.5"): Price(2.00, 6.00, 0.30, limit=200_000),
    ("xai", "grok-4.3"): Price(1.25, 2.50, 0.20, limit=200_000),
    ("xai", "grok-4.20-0309-reasoning"): Price(1.25, 2.50, 0.20, limit=200_000),
    ("xai", "grok-4.20-0309-non-reasoning"): Price(1.25, 2.50, 0.20, limit=200_000),
    ("xai", "grok-build-0.1"): Price(1.00, 2.00, 0.20, limit=200_000),
    ("openai", "gpt-5.6-sol"): Price(4.00, 20.00, 0.40, limit=272_000),  # promo price to 30 Nov 2026
    ("openai", "gpt-5.6-terra"): Price(2.00, 12.00, 0.20, limit=272_000),
    ("openai", "gpt-5.6-luna"): Price(0.20, 1.20, 0.02, limit=272_000),
    ("openai", "gpt-5.5"): Price(5.00, 30.00, 0.50, limit=272_000),
    ("openai", "gpt-5.4"): Price(2.50, 15.00, 0.25, limit=272_000),
    ("openai", "gpt-5.4-mini"): Price(0.75, 4.50, 0.075, limit=272_000),
    ("openai", "gpt-5.4-nano"): Price(0.20, 1.25, 0.02, limit=272_000),
    ("openai", "gpt-5-mini"): Price(0.25, 2.00, 0.025, limit=272_000),
    ("openai", "gpt-5-nano"): Price(0.05, 0.40, 0.005, limit=272_000),
    # Gemini standard tier. gemini-4-argon and the 3.x flash prices rise on 1 Dec 2026 and 1 Jan 2027.
    ("gemini", "gemini-4-argon"): Price(2.00, 10.00, 0.10),
    ("gemini", "gemini-3.8-flash"): Price(0.75, 3.75, 0.075),
    ("gemini", "gemini-3.6-flash"): Price(0.75, 3.75, 0.075),
    ("gemini", "gemini-3.5-flash-lite"): Price(0.30, 2.50, 0.03),
    ("gemini", "gemini-3.1-pro-preview"): Price(
        2.00, 12.00, 0.20, limit=200_000, over=Price(4.00, 18.00, 0.40),
    ),
    ("gemini", "gemini-3.1-flash-lite"): Price(0.25, 1.50, 0.025),
    ("gemini", "gemini-3-flash-preview"): Price(0.50, 3.00, 0.05),
    # DeepSeek: the higher (peak hours) rates, so the estimate never runs low.
    ("deepseek", "deepseek-flash"): Price(0.30, 1.20, 0.006),
    ("deepseek", "deepseek-v4-pro"): Price(1.32, 3.96, 0.044),
    # Qwen intl, 0-256k tier (no cache price on the page).
    ("qwen", "qwen3.8-max"): Price(2.00, 6.00, limit=256_000),
    ("qwen", "qwen3.7-max"): Price(2.50, 7.50, limit=256_000),
    ("qwen", "qwen-max"): Price(1.60, 6.40, limit=256_000),
    ("qwen", "qwen3.7-plus"): Price(0.40, 1.60, limit=256_000),
    ("qwen", "qwen3.6-plus"): Price(0.50, 3.00, limit=256_000),
    ("qwen", "qwen3.8-flash"): Price(0.15, 0.47, limit=256_000),
    ("qwen", "qwen3.6-flash"): Price(0.25, 1.50, limit=256_000),
    ("qwen", "qwen3.5-flash"): Price(0.10, 0.40, limit=256_000),
    ("qwen", "qwen-flash"): Price(0.05, 0.40, limit=256_000),
    ("qwen", "qwen3.8-27b"): Price(0.50, 3.00, limit=256_000),
    ("qwen", "qwen3-vl-plus"): Price(0.20, 1.60, limit=256_000),
}
# Not priced on purpose (unverified in research/11): Kimi, Mistral except Large (its API id is not
# confirmed), GLM (the page shows display names, not API ids), OpenRouter and Groq (many models).


def is_local(where: str) -> bool:
    """A local server name, or a host or URL on this machine or a private network (the registry's rule)."""
    from micropaw.providers.registry import is_local_url

    where = where.strip().lower()
    if where in LOCAL_PROVIDERS:
        return True
    if where != "localhost" and not any(c in where for c in "./:"):
        return False  # a bare provider name such as "claude", not a host
    return is_local_url(where)


def is_local_provider(provider: object) -> bool:
    """True when this provider object talks to a local server (by name or by its base URL).
    A failover chain is local only when every provider in it is."""
    from micropaw.providers.failover import FallbackProvider
    from micropaw.providers.metered import MeteredProvider

    if isinstance(provider, MeteredProvider):
        return is_local_provider(provider.inner)
    if isinstance(provider, FallbackProvider):
        return all(is_local_provider(p) for p in provider.providers)
    url = getattr(provider, "host", "") or getattr(provider, "_api_url", "") or getattr(provider, "base_url", "")
    if url:  # the address decides: a custom server named "vllm" that points at a cloud URL is not free
        return is_local(str(url))
    return str(getattr(provider, "name", "")) in LOCAL_PROVIDERS


def cost(provider: str, model: str, usage: dict[str, int]) -> float | None:
    """USD for one call, or None when the price is unknown. `provider` may be a local name or host."""
    if is_local(provider):
        return 0.0
    price = PRICES.get((provider.lower(), model))
    if price is None:
        return None
    total_in = usage.get("input", 0) or 0
    if price.limit is not None and total_in > price.limit:
        if price.over is None:
            return None
        price = price.over
    cached = min(usage.get("cache_read", 0) or 0, total_in)
    written = min(usage.get("cache_write", 0) or 0, total_in - cached)
    fresh = total_in - cached - written
    usd = (
        fresh * price.input
        + cached * (price.cache_read if price.cache_read is not None else price.input)
        + written * (price.cache_write if price.cache_write is not None else price.input)
        + (usage.get("output", 0) or 0) * price.output
    )
    return usd / 1_000_000
