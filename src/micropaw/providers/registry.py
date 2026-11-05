"""Model registry: one "provider:model" spec everywhere, presets, custom servers, one shared provider per spec."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from micropaw.providers.base import AIProvider, AIResponse, Message, ToolDef
from micropaw.providers.failover import (
    FallbackProvider,
    FriendlyError,
    is_context_error,
    trim_messages,
    unwrap,
)

if TYPE_CHECKING:
    from micropaw.config import Config

logger = logging.getLogger(__name__)

BUILTIN = ("claude", "openai", "gemini", "ollama")
LOCAL_CONCURRENCY = 1  # one call at a time per local server: a second one only queues inside it
CLOUD_CONCURRENCY = 8


@dataclass(frozen=True)
class Preset:
    base_url: str  # with the version path when the server has one ("/v1", "/v4")
    key_env: str = ""  # "" = no key needed
    quirks: frozenset[str] = frozenset()


# Base URLs and quirks from research/11 (read 5 Nov 2026). Whether a preset is local comes from its URL host.
# Not shipped (no verified URL or key header yet): minimax, nvidia, perplexity, localai, gemini-openai.
PRESETS: dict[str, Preset] = {
    "openrouter": Preset("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "groq": Preset("https://api.groq.com/openai/v1", "GROQ_API_KEY", frozenset({"n1"})),
    "deepseek": Preset("https://api.deepseek.com", "DEEPSEEK_API_KEY", frozenset({"reasoning_content"})),
    "mistral": Preset("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "together": Preset("https://api.together.ai/v1", "TOGETHER_API_KEY"),
    "xai": Preset("https://api.x.ai/v1", "XAI_API_KEY"),
    "cerebras": Preset("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", frozenset({"n1"})),
    "fireworks": Preset("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
    "moonshot": Preset("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY"),
    "zai": Preset("https://api.z.ai/api/paas/v4", "ZAI_API_KEY"),
    # The intl endpoint came from a search; the docs page shows a per-workspace host. Check before relying on it.
    "dashscope": Preset("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
                        frozenset({"no_stream_with_tools"})),
    "hf": Preset("https://router.huggingface.co/v1", "HF_TOKEN"),
    "ollama-cloud": Preset("https://ollama.com/v1", "OLLAMA_API_KEY"),
    "lmstudio": Preset("http://localhost:1234/v1"),
    "llamacpp": Preset("http://localhost:8080/v1"),  # the README says 9931, older builds 8080: unresolved
    "vllm": Preset("http://localhost:8000/v1"),
    "jan": Preset("http://127.0.0.1:1337/v1"),
    "koboldcpp": Preset("http://localhost:5001/v1"),
}

# Default main and fast model per lab, only IDs that appear on the lab's own pricing or model page (research/11).
# TODO: mistral has no API model ID confirmed yet, so it has no entry.
CLOUD_DEFAULTS: dict[str, tuple[str, str]] = {
    "claude": ("claude-sonnet-5-5", "claude-haiku-5-5"),
    "openai": ("gpt-5.4", "gpt-5.4-mini"),
    "gemini": ("gemini-3.8-flash", "gemini-3.5-flash-lite"),
    "xai": ("grok-4.3", "grok-4.20-0309-non-reasoning"),
    "deepseek": ("deepseek-v4-pro", "deepseek-flash"),
}

_DOWN = {
    "lmstudio": "LM Studio server is not running; start it in the Developer tab.",
    "ollama": "Ollama is not running; start the Ollama app or run `ollama serve`.",
}


# ---------------------------------------------------------------------------
# Specs and local detection
# ---------------------------------------------------------------------------

def parse_spec(spec: str) -> tuple[str, str]:
    """"ollama:qwen3.6:27b" -> ("ollama", "qwen3.6:27b"). A bare "claude" has an empty model."""
    name, _, model = spec.partition(":")
    return name.strip().lower(), model.strip()


# The last one is Tailscale's CGNAT range.
_PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                 ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "100.64.0.0/10")]


@lru_cache(maxsize=128)
def _resolves_private(host: str) -> bool:
    try:
        return all(_private_ip(ipaddress.ip_address(a[4][0])) for a in socket.getaddrinfo(host, None))
    except (OSError, ValueError):
        return False


def _private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_loopback or any(ip in net for net in _PRIVATE_NETS)


def is_local_url(url: str) -> bool:
    """Loopback or a private network address. Derived from the host, never declared.

    A plain-http host name is looked up once (a LAN box like "gpu-box"). https names count as cloud.
    """
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return _private_ip(ipaddress.ip_address(host))
    except ValueError:
        return parsed.scheme == "http" and _resolves_private(host)


def legacy_compat() -> dict[str, tuple[str, str]]:
    """The v0.3 OPENAI_COMPATIBLE shape (base URL without /v1, key env), for code that still reads it."""
    out = {}
    for name, p in PRESETS.items():
        base = p.base_url[:-3] if p.base_url.endswith("/v1") else p.base_url
        if "/v" not in urlparse(base).path:
            out[name] = (base, p.key_env)
    return out


# ---------------------------------------------------------------------------
# Wrapper: concurrency limit, context-length retry, plain errors for local servers
# ---------------------------------------------------------------------------

class _Gate(AIProvider):
    """Thin wrapper around one provider. `local` is the preset name for a local server, else None."""

    def __init__(self, inner: AIProvider, sem: asyncio.Semaphore, local: str | None) -> None:
        self._inner = inner
        self._sem = sem
        self._local = local
        self.name = inner.name
        self.model = getattr(inner, "model", "")

    @property
    def busy(self) -> bool:
        """A local server is already answering someone."""
        return self._local is not None and self._sem.locked()

    def __getattr__(self, item: str):
        if item == "_inner":
            raise AttributeError(item)
        return getattr(self._inner, item)

    def _friendly(self, e: Exception) -> Exception:
        if self._local is None:
            return e
        root = unwrap(e)
        if isinstance(root, (httpx.ConnectError, httpx.ConnectTimeout)):
            host = getattr(self._inner, "host", "") or urlparse(getattr(self._inner, "_api_url", "")).netloc
            generic = f"The {self._local} server at {host} is not running. Start it and try again."
            return FriendlyError(_DOWN.get(self._local, generic), e)
        if isinstance(root, httpx.TimeoutException):
            return FriendlyError(
                "The local model did not answer within 5 minutes. It may still be loading. "
                "Try again, or use a smaller model.", e)
        return e

    def _raise(self, e: Exception):
        friendly = self._friendly(e)
        if friendly is e:
            raise e
        raise friendly from e

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        async with self._sem:
            try:
                try:
                    return await self._inner.chat(messages, tools)
                except Exception as e:
                    trimmed = trim_messages(messages) if is_context_error(e) else None
                    if trimmed is None:
                        raise
                    logger.warning("%s: context too long, retrying once with older messages dropped", self.name)
                    return await self._inner.chat(trimmed, tools)
            except Exception as e:
                self._raise(e)

    async def chat_stream(
        self, messages: list[Message], tools: list[ToolDef] | None = None,
    ) -> AsyncGenerator[str | AIResponse, None]:
        async with self._sem:
            for attempt in (0, 1):
                started = False
                try:
                    async for item in self._inner.chat_stream(messages, tools):
                        started = True
                        yield item
                    return
                except Exception as e:
                    trimmed = trim_messages(messages) if attempt == 0 and not started and is_context_error(e) else None
                    if trimmed is None:
                        self._raise(e)
                    logger.warning("%s: context too long, retrying once with older messages dropped", self.name)
                    messages = trimmed

    async def close(self) -> None:
        await self._inner.close()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    name: str
    model: str
    base_url: str
    key_env: str = ""
    quirks: frozenset[str] = frozenset()
    kind: str = "compat"  # claude | openai | gemini | ollama | compat
    local: bool = field(init=False)

    def __post_init__(self) -> None:
        self.local = is_local_url(self.base_url)


class Registry:
    """Builds each provider once per spec, so every agent and role using it shares one connection pool."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.problems: list[str] = []  # why a spec could not be built, for the caller to show
        self._cache: dict[str, AIProvider] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        from micropaw.memory.ledger import LateLedger
        self.ledger = LateLedger()  # bound by the gateway once the database is open
        self.presets = {**PRESETS, **self._custom()}

    def _custom(self) -> dict[str, Preset]:
        out = {}
        for name, cfg in self.config.providers.items():
            name = name.lower()
            if name in BUILTIN:
                self.problems.append(f"Provider name '{name}' is built in; pick another name for your server.")
                continue
            parsed = urlparse(cfg.base_url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                self.problems.append(f"Provider '{name}': base_url must start with http:// or https://.")
            elif parsed.scheme == "http" and not is_local_url(cfg.base_url):
                self.problems.append(
                    f"Provider '{name}' refused: {cfg.base_url} is plain http to a host outside your own network, "
                    "which would send the key in clear text. Use https.")
            else:
                out[name] = Preset(cfg.base_url, cfg.api_key_env)
        return out

    def resolve(self, spec: str) -> Endpoint | None:
        """Where a spec points, with the provider's configured default model for a bare name."""
        name, model = parse_spec(spec)
        pc = self.config.provider_config
        if name in ("claude", "openai", "gemini"):
            cfg = getattr(pc, name)
            return Endpoint(name, model or cfg.model, cfg.base_url, kind=name)
        if name == "ollama":
            return Endpoint(name, model or pc.ollama.model, pc.ollama.host, kind=name)
        preset = self.presets.get(name)
        if preset is None or not model:
            return None
        return Endpoint(name, model, preset.base_url, preset.key_env, preset.quirks)

    def is_local(self, spec: str) -> bool:
        ep = self.resolve(spec)
        return ep is not None and ep.local

    def _sem(self, ep: Endpoint) -> asyncio.Semaphore:
        key = urlparse(ep.base_url if "://" in ep.base_url else f"http://{ep.base_url}").netloc if ep.local else ep.name
        if key not in self._sems:
            self._sems[key] = asyncio.Semaphore(LOCAL_CONCURRENCY if ep.local else CLOUD_CONCURRENCY)
        return self._sems[key]

    def _build(self, ep: Endpoint) -> AIProvider | None:
        pc = self.config.provider_config
        if ep.kind == "claude":
            from micropaw.providers.claude import ClaudeProvider
            c = pc.claude
            return ClaudeProvider(c.api_key, ep.model, c.max_tokens, c.base_url, effort=c.effort) if c.api_key else None
        if ep.kind == "gemini":
            from micropaw.providers.gemini import GeminiProvider
            c = pc.gemini
            return GeminiProvider(c.api_key, ep.model, c.max_tokens, c.base_url) if c.api_key else None
        if ep.kind == "ollama":
            from micropaw.providers.ollama import OllamaProvider
            c = pc.ollama
            return OllamaProvider(c.host, ep.model, c.max_tokens, c.num_ctx, c.keep_alive)
        from micropaw.providers.openai import OpenAIProvider
        timeout = 300.0 if ep.local else 120.0  # local servers load the model on the first request
        if ep.kind == "openai":
            c = pc.openai
            return OpenAIProvider(c.api_key, ep.model, c.max_tokens, c.base_url, read_timeout=timeout) \
                if c.api_key else None
        key = os.environ.get(ep.key_env, "") if ep.key_env else "local"
        if not key:
            return None
        return OpenAIProvider(key, ep.model, pc.openai.max_tokens, ep.base_url, ep.quirks, timeout, ep.name)

    def get(self, spec: str) -> AIProvider | None:
        """The provider for "provider:model", built once. None when the name is unknown or its key is missing."""
        ep = self.resolve(spec)
        if ep is None:
            self.problems.append(f"Model '{spec}' is not usable: unknown provider, or no model after the colon.")
            return None
        key = f"{ep.name}:{ep.model}"
        if key not in self._cache:
            inner = self._build(ep)
            if inner is None:
                need = ep.key_env or f"the {ep.name} API key"
                self.problems.append(f"Model '{spec}' is not usable: {need} is not set.")
                return None
            self._cache[key] = _Gate(inner, self._sem(ep), ep.name if ep.local else None)
        return self._cache[key]

    def _built(self, specs: list[str]) -> list[AIProvider]:
        """The providers that build, in order, without duplicates. Specs that cannot build are skipped."""
        built: list[AIProvider] = []
        for spec in specs:
            p = self.get(spec)
            if p is not None and p not in built:
                built.append(p)
        return built

    def chain(self, specs: list[str]) -> AIProvider | None:
        """One provider, or a FallbackProvider when several build. Not metered: see `role`."""
        built = self._built(specs)
        if not built:
            return None
        return built[0] if len(built) == 1 else FallbackProvider(built)

    def role(self, agent: str, role: str, specs: list[str]) -> AIProvider | None:
        """Like `chain`, but every provider is metered on its own, so the ledger records the one that answered."""
        from micropaw.providers.metered import meter
        metered = [meter(p, agent=agent, role=role, ledger=self.ledger, budget=self.config.budget)
                   for p in self._built(specs)]
        if not metered:
            return None
        return metered[0] if len(metered) == 1 else FallbackProvider(metered)

    async def close(self) -> None:
        for provider in self._cache.values():
            try:
                await asyncio.wait_for(provider.close(), timeout=5.0)
            except Exception as e:
                logger.error("Error closing provider: %s", e)


# ---------------------------------------------------------------------------
# Local servers: scan, size-aware defaults
# ---------------------------------------------------------------------------

# name -> (kind, URL). "ollama" lists /api/tags, the others list /v1/models.
LOCAL_SERVERS: dict[str, tuple[str, str]] = {
    "ollama": ("ollama", "http://127.0.0.1:11434"),
    "lmstudio": ("openai", "http://127.0.0.1:1234"),
    "llamacpp": ("openai", "http://127.0.0.1:8080"),
    "vllm": ("openai", "http://127.0.0.1:8000"),
    "jan": ("openai", "http://127.0.0.1:1337"),
    "koboldcpp": ("openai", "http://127.0.0.1:5001"),
}


@dataclass
class LocalModel:
    name: str
    size: int = 0  # bytes, 0 = unknown
    tools: bool = True
    vision: bool = False
    embedding: bool = False


@dataclass
class LocalServer:
    name: str
    url: str
    models: list[LocalModel]


async def _probe(client: httpx.AsyncClient, name: str, kind: str, url: str) -> LocalServer | None:
    from micropaw.providers.ollama import fetch_show
    try:
        if kind == "ollama":
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            raw = resp.json().get("models") or []
            infos = await asyncio.gather(*(fetch_show(client, url, m.get("name", "")) for m in raw))
            models = []
            for m, info in zip(raw, infos):
                caps = info.caps if info else None
                models.append(LocalModel(
                    m.get("name", ""), int(m.get("size") or 0),
                    tools=info.tools if info else True,
                    vision=info.vision if info else False,
                    embedding=("embedding" in caps) if caps else "embed" in m.get("name", ""),
                ))
        else:
            resp = await client.get(f"{url}/v1/models")
            resp.raise_for_status()
            models = [LocalModel(m["id"], embedding="embed" in m["id"]) for m in resp.json().get("data") or []
                      if m.get("id")]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return LocalServer(name, url, models)


async def scan_local(targets: dict[str, tuple[str, str]] | None = None) -> list[LocalServer]:
    """Probe the usual local ports in parallel (300 ms to connect). Returns the servers that answered."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=0.3)) as client:
        found = await asyncio.gather(*(_probe(client, n, k, u) for n, (k, u) in (targets or LOCAL_SERVERS).items()))
    return [s for s in found if s]


# (tag, download size in GB) from research/11 section 4. Sizes are of the weights; KV cache comes on top.
PULL_CHOICES: list[tuple[str, float]] = [
    ("granite4.1:3b", 2.1), ("qwen3.5:4b", 4.0), ("qwen3.5:9b", 7.6), ("qwen3.8:27b", 18.0), ("qwen3.6:35b", 23.0),
]
RAM_FRACTION = 0.6  # a model should fit in 60% of RAM


def suggest_pull(ram: int | None) -> str:
    """The largest model on the list that fits; the smallest when RAM is unknown or tiny."""
    if not ram:
        return "qwen3.5:4b"
    fits = [tag for tag, gb in PULL_CHOICES if gb * 1e9 <= ram * RAM_FRACTION]
    return fits[-1] if fits else PULL_CHOICES[0][0]


@dataclass
class LocalPick:
    main: LocalModel | None = None
    fast: LocalModel | None = None    # the smallest with tools (hybrid use)
    vision: LocalModel | None = None  # only when main cannot see


def pick_local(models: list[LocalModel], ram: int | None) -> LocalPick:
    """Main = the largest model with tools that fits in 60% of RAM. Embedding models are never picked."""
    chat = [m for m in models if not m.embedding]
    usable = [m for m in chat if m.tools and (not ram or not m.size or m.size <= ram * RAM_FRACTION)]
    if not usable:
        return LocalPick()
    main = max(usable, key=lambda m: m.size)
    vision = None if main.vision else next((m for m in chat if m.vision), None)
    return LocalPick(main, min(usable, key=lambda m: m.size), vision)
