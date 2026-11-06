"""Model registry: one "provider:model" spec everywhere, presets, custom servers, one shared provider per spec."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import logging
import os
import socket
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from micropaw import net, privacy
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
    kind: str = "compat"  # "ollama" = the native Ollama API (your own named Ollama server)
    num_ctx: int = 0
    keep_alive: str = ""


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


def _is_name(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def parse_spec(spec: str) -> tuple[str, str]:
    """"ollama:qwen3.6:27b" -> ("ollama", "qwen3.6:27b"). A bare "claude" has an empty model."""
    name, _, model = spec.partition(":")
    return name.strip().lower(), model.strip()


# The last one is Tailscale's CGNAT range.
_PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                 ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "100.64.0.0/10")]

DNS_TTL = 300.0     # seconds a finished lookup is remembered
DNS_FAIL_TTL = 10.0  # a failed lookup is retried after this, so a box that wakes up is found soon
DNS_TIMEOUT = 2.0   # a bad DNS server on a small board must not stall start
_dns: dict[str, tuple[bool | None, float]] = {}
_dns_pool = concurrent.futures.ThreadPoolExecutor(2, thread_name_prefix="dns")


def _now() -> float:
    return time.monotonic()


def _lookup(host: str) -> bool | None:
    """True when the name resolves only to loopback or private addresses, False when any answer is public,
    None when the lookup failed or timed out. A finished lookup is remembered for DNS_TTL, a failed one for
    DNS_FAIL_TTL: a name that does not resolve can take seconds each time (Windows), and every client asks."""
    hit = _dns.get(host)
    if hit and hit[1] > _now():
        return hit[0]
    try:
        infos = _dns_pool.submit(socket.getaddrinfo, host, None).result(timeout=DNS_TIMEOUT)
        private = all(_private_ip(ipaddress.ip_address(a[4][0])) for a in infos)
    except (OSError, ValueError, concurrent.futures.TimeoutError):
        _dns[host] = (None, _now() + DNS_FAIL_TTL)
        return None
    _dns[host] = (private, _now() + DNS_TTL)
    return private


def _resolves_private(host: str) -> bool:
    return _lookup(host) is True


def _private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_loopback or any(ip in net for net in _PRIVATE_NETS)


def origin_of(url: str) -> str:
    """scheme://host:port of a base URL, without the /v1 path."""
    u = urlparse(url if "://" in url else f"http://{url}")
    return f"{u.scheme}://{u.netloc}"


async def ollama_verdict(client: httpx.AsyncClient, origin: str, model: str, key: str = "") -> str:
    """Does this model run on the server at `origin`? Asks Ollama's /api/show, which also answers behind an
    OpenAI-style /v1 address. "remote" (it names ollama.com or another host), "here", "down" (nothing listens),
    "unknown" (an Ollama that gave no usable answer) or "other" (not an Ollama, so there is nothing to ask)."""
    from micropaw.providers.ollama import ShowInfo, show_status
    got = await show_status(client, origin, model, key)
    if isinstance(got, ShowInfo):
        return "remote" if got.remote else "here"
    if got == "down":
        return "down"
    ollama = urlparse(origin).port == 11434
    if not ollama:
        try:
            resp = await client.get(f"{origin}/api/tags", headers={"Authorization": f"Bearer {key}"} if key else {})
            ollama = resp.status_code == 200 and isinstance(resp.json().get("models"), list)
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    return "unknown" if ollama else "other"


def _host_of(url: str) -> tuple[str, str]:
    """(scheme, lower-case host) of a URL or bare host[:port]."""
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    return parsed.scheme, (parsed.hostname or "").lower()


def is_local_url(url: str) -> bool:
    """This machine or the local network. Derived from the host, never declared.

    In airgapped and local mode: loopback, the LAN ranges (when privacy.allow_lan) and the names the owner listed
    in privacy.lan_hosts, with no DNS lookup. Otherwise a plain-http host name is looked up (a LAN box like
    "gpu-box"), and so is any Tailscale name (.ts.net). Other https names count as cloud.
    """
    scheme, host = _host_of(url)
    if not host:
        return False
    if privacy.local_only():
        return privacy.local_host(host)
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return _private_ip(ipaddress.ip_address(host))
    except ValueError:
        return (scheme == "http" or host.endswith(".ts.net")) and _resolves_private(host)


# Wrapper: concurrency limit, context-length retry, plain errors for local servers

class _Gate(AIProvider):
    """Thin wrapper around one provider. `local` is the preset name for a local server, else None.
    `pending` is the host name of a LAN entry that did not resolve when MicroPaw started."""

    def __init__(self, inner: AIProvider, sem: asyncio.Semaphore, local: str | None,
                 key_env: str = "", pending: str = "", verify: tuple[str, str, str] | None = None) -> None:
        self._inner = inner
        self._verify_args = verify  # (origin, model, key) to ask Ollama's /api/show before the first chat
        self._sem = sem
        self._local = local
        self._key_env = key_env
        self._pending = pending
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

    async def _resolve_pending(self) -> None:
        """A LAN name that was not found at start is looked up again; nothing is sent until it resolves to a
        private address."""
        if not self._pending:
            return
        state = await asyncio.to_thread(_lookup, self._pending)
        if state is True:
            self._pending = ""
            return
        if state is None:
            err = httpx.ConnectError(f"cannot look up {self._pending}")
            raise FriendlyError(
                f"Cannot look up {self._pending} from this computer. Is it on, and is the name right? "
                "You can use its address instead, for example 192.168.1.50.", err)
        raise FriendlyError(
            f"{self._pending} resolves to a public address, so plain http would send the key in clear text. "
            "Use https, or a private address (192.168.x, 10.x, Tailscale 100.x).", PermissionError(self._pending))

    async def _verify(self) -> None:
        """An OpenAI-style address can be an Ollama that serves a model from ollama.com under a local name. Ask
        once, and refuse in local and airgapped mode unless it is clear the model runs here."""
        if self._verify_args is None:
            return
        origin, model, key = self._verify_args
        async with net.client(origin, timeout=httpx.Timeout(5.0, connect=2.0), local=True) as client:
            verdict = await ollama_verdict(client, origin, model, key)
        if verdict in ("here", "other"):
            self._verify_args = None
        elif verdict in ("remote", "unknown"):
            what = ("it runs on ollama.com, not on this network" if verdict == "remote" else
                    "Ollama gave no usable answer to /api/show, so it cannot be confirmed that it runs here")
            raise privacy.EgressBlocked(f"privacy mode blocked the model {model}: {what}")

    def _where(self) -> tuple[str, bool]:
        """(host:port, True when that is this machine) of the server behind this gate."""
        raw = getattr(self._inner, "host", "") or getattr(self._inner, "_api_url", "")
        if not raw:
            return "", True
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        host = (parsed.hostname or "").lower()
        shown = f"{host}:{parsed.port}" if parsed.port else host  # never the user:password part
        return shown, host in ("localhost", "::1") or host.startswith("127.") or host.endswith(".localhost")

    def _friendly(self, e: Exception) -> Exception:
        if self._local is None:
            return e
        root = unwrap(e)
        where, loopback = self._where()
        if privacy.blocked(e):
            return FriendlyError(f"Blocked by privacy mode: {privacy.blocked(e)}", e)
        if isinstance(root, (httpx.ConnectError, httpx.ConnectTimeout)):
            if loopback:
                generic = f"The {self._local} server at {where} is not running. Start it and try again."
                return FriendlyError(_DOWN.get(self._local, generic), e)
            text = (f"Cannot reach {where}. Is that computer on and awake, "
                    "and is the server set to listen on the network?")
            if isinstance(root, httpx.ConnectTimeout):
                text += f" A firewall on that computer may be blocking port {where.rpartition(':')[2] or 'the port'}."
            return FriendlyError(text, e)
        if isinstance(root, httpx.TimeoutException):
            if loopback:
                return FriendlyError(
                    "The local model did not answer within 5 minutes. It may still be loading. "
                    "Try again, or use a smaller model.", e)
            return FriendlyError(
                f"The model at {where} did not answer within 5 minutes. It may still be loading; "
                "try again or pick a smaller model.", e)
        if isinstance(root, httpx.HTTPStatusError) and root.response.status_code in (401, 403):
            if self._key_env:
                return FriendlyError(
                    f"The server at {where} rejected the key in {self._key_env}. Check it in ~/.micropaw/.env.", e)
            return FriendlyError(
                f"The server at {where} needs a key. Run: paw models add {self._local} <url> --key-env <NAME>.", e)
        return e

    def _raise(self, e: Exception):
        friendly = self._friendly(e)
        if friendly is e:
            raise e
        raise friendly from e

    async def chat(self, messages: list[Message], tools: list[ToolDef] | None = None) -> AIResponse:
        await self._resolve_pending()
        async with self._sem:
            try:
                await self._verify()
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
        await self._resolve_pending()
        async with self._sem:
            for attempt in (0, 1):
                started = False
                try:
                    await self._verify()
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


@dataclass
class Endpoint:
    name: str
    model: str
    base_url: str
    key_env: str = ""
    quirks: frozenset[str] = frozenset()
    kind: str = "compat"  # claude | openai | gemini | ollama | compat
    num_ctx: int = 0
    keep_alive: str = ""
    pending: bool = False  # a LAN name that has not resolved yet: held back until it does
    local: bool = field(init=False)

    def __post_init__(self) -> None:
        # An Ollama cloud model is served through a local daemon but runs on ollama.com
        self.local = (self.pending or is_local_url(self.base_url)) and not (
            self.kind in ("ollama", "compat") and privacy.cloud_tagged(self.model))


class Registry:
    """Builds each provider once per spec, so every agent and role using it shares one connection pool."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.problems: list[str] = []  # why a spec could not be built, for the caller to show
        self._cache: dict[str, AIProvider] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self.pending: dict[str, str] = {}  # custom entry name -> host name still to be found
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
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in ("http", "https") or not host:
                self.problems.append(f"Provider '{name}': base_url must start with http:// or https://.")
                continue
            if parsed.scheme == "http" and not is_local_url(cfg.base_url):
                state = None if privacy.local_only() else _lookup(host)
                if state is None and not privacy.local_only() and _is_name(host):
                    self.pending[name] = host  # not found yet (box asleep, mDNS not up): looked up again on first use
                else:
                    self.problems.append(
                        f"Provider '{name}' refused: {host} "
                        + ("is not on this machine or listed in privacy.lan_hosts. "
                           f'Add "{host}" to privacy.lan_hosts to use it.' if privacy.local_only()
                           else "resolves to a public address, so plain http would send the key in clear text. "
                                "Use https, or a private address (192.168.x, 10.x, Tailscale 100.x)."))
                    continue
            out[name] = Preset(cfg.base_url, cfg.api_key_env, kind=cfg.kind if cfg.kind == "ollama" else "compat",
                               num_ctx=cfg.num_ctx, keep_alive=cfg.keep_alive)
        return out

    def resolve(self, spec: str) -> Endpoint | None:
        """Where a spec points, with the provider's configured default model for a bare name."""
        name, model = parse_spec(spec)
        pc = self.config.provider_config
        if name in ("claude", "openai", "gemini"):
            cfg = getattr(pc, name)
            return Endpoint(name, model or cfg.model, cfg.base_url, kind=name)
        if name == "ollama":
            return Endpoint(name, model or pc.ollama.model, pc.ollama.host, kind=name,
                            num_ctx=pc.ollama.num_ctx, keep_alive=pc.ollama.keep_alive)
        preset = self.presets.get(name)
        if preset is None or not model:
            return None
        return Endpoint(name, model, preset.base_url, preset.key_env, preset.quirks,
                        "ollama" if preset.kind == "ollama" else "compat", preset.num_ctx, preset.keep_alive,
                        pending=name in self.pending)

    def is_local(self, spec: str) -> bool:
        ep = self.resolve(spec)
        return ep is not None and ep.local

    def refusal(self, spec: str, role: str = "") -> str | None:
        """Why the privacy mode does not allow this model for this role, else None. Listed in plain words,
        with the setting to change."""
        ep = self.resolve(spec)
        if ep is None or not privacy.local_only():
            return None
        mode = privacy.mode()
        label = f"{role} model" if role else "model"
        if ep.kind in ("ollama", "compat") and privacy.cloud_tagged(ep.model):
            return (f"privacy.mode is {mode}: the {label} {spec} runs on ollama.com, even though the server is "
                    "on this machine. Use a model that runs here.")
        if ep.local:
            return None
        host = urlparse(ep.base_url if "://" in ep.base_url else f"http://{ep.base_url}").hostname or ep.base_url
        fix = (f' If it is a computer on your network, add "{host}" to privacy.lan_hosts.'
               if ep.kind in ("ollama", "compat") and _is_name(host) else "")
        return (f"privacy.mode is {mode}: the {label} {spec} would send this chat to {host}. "
                f"Use a local model or switch to normal.{fix}")

    def _sem(self, ep: Endpoint) -> asyncio.Semaphore:
        key = ep.name
        if ep.local:  # one gate per server, however its address is spelled
            u = urlparse(ep.base_url if "://" in ep.base_url else f"http://{ep.base_url}")
            key = f"{u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}"
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
            key = os.environ.get(ep.key_env, "") if ep.key_env else ""
            if ep.key_env and not key:
                return None
            # The context size follows the machine that runs the model, not the one MicroPaw runs on
            remote_ctx = 0 if ep.local and _host_of(ep.base_url)[1] in ("localhost", "127.0.0.1", "::1") else 8192
            return OllamaProvider(ep.base_url, ep.model, pc.ollama.max_tokens, ep.num_ctx or remote_ctx,
                                  ep.keep_alive or pc.ollama.keep_alive, key)
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

    def get(self, spec: str, role: str = "") -> AIProvider | None:
        """The provider for "provider:model", built once. None when the name is unknown, its key is missing, or
        the privacy mode refuses it (the reason is added to `problems`)."""
        ep = self.resolve(spec)
        if ep is None:
            self.problems.append(f"Model '{spec}' is not usable: unknown provider, or no model after the colon.")
            return None
        if msg := self.refusal(spec, role):
            self.problems.append(msg)
            return None
        key = f"{ep.name}:{ep.model}"
        if key not in self._cache:
            inner = self._build(ep)
            if inner is None:
                need = ep.key_env or f"the {ep.name} API key"
                self.problems.append(f"Model '{spec}' is not usable: {need} is not set.")
                return None
            verify = None
            if ep.kind == "compat" and ep.local and privacy.local_only():
                verify = (origin_of(ep.base_url), ep.model, os.environ.get(ep.key_env, "") if ep.key_env else "")
            self._cache[key] = _Gate(inner, self._sem(ep), ep.name if ep.local else None, ep.key_env,
                                     self.pending.get(ep.name, ""), verify)
        return self._cache[key]

    def _built(self, specs: list[str], role: str = "") -> list[AIProvider]:
        """The providers that build, in order, without duplicates. Specs that cannot build are skipped."""
        built: list[AIProvider] = []
        for spec in specs:
            p = self.get(spec, role)
            if p is not None and p not in built:
                built.append(p)
        return built

    def _hold_back(self, built: list[AIProvider]) -> tuple[list[AIProvider], list[str]]:
        """A local first choice does not hand the chat to a cloud model when it fails, unless the owner allows it.
        Returns the providers to use and the names held back."""
        if len(built) < 2 or self.config.privacy.allow_cloud_fallback or not privacy.is_local_provider(built[0]):
            return built, []
        keep = [p for p in built if privacy.is_local_provider(p)]
        return keep, [f"{p.name}:{getattr(p, 'model', '')}".rstrip(":") for p in built if p not in keep]

    def chain(self, specs: list[str], role: str = "") -> AIProvider | None:
        """One provider, or a FallbackProvider when several build. Not metered: see `role`."""
        built, held = self._hold_back(self._built(specs, role))
        if not built:
            return None
        return built[0] if len(built) == 1 and not held else FallbackProvider(built, held)

    def role(self, agent: str, role: str, specs: list[str]) -> AIProvider | None:
        """Like `chain`, but every provider is metered on its own, so the ledger records the one that answered."""
        from micropaw.providers.metered import meter
        built, held = self._hold_back(self._built(specs, role))
        metered = [meter(p, agent=agent, role=role, ledger=self.ledger, budget=self.config.budget) for p in built]
        if not metered:
            return None
        return metered[0] if len(metered) == 1 and not held else FallbackProvider(metered, held)

    async def close(self) -> None:
        for provider in self._cache.values():
            try:
                await asyncio.wait_for(provider.close(), timeout=5.0)
            except Exception as e:
                logger.error("Error closing provider: %s", e)


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
    # Local servers and the LAN never go through an HTTP proxy from the environment
    async with net.client(timeout=httpx.Timeout(3.0, connect=0.3), local=True) as client:
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
