"""Model servers on other computers: probe, add, scan the network, and the doctor's server rows.

Everything here is run by the owner from the command line. It only ever lists models (GET), never chats,
and a key goes only to the host it was named for. None of it is an agent tool.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from micropaw import net, privacy
from micropaw.config import SCHEMA_VERSION, Config, server_url
from micropaw.providers.registry import BUILTIN, LOCAL_SERVERS, PRESETS, LocalModel, LocalServer, Registry, parse_spec

# `paw models scan` also looks here: the llama.cpp README now says 9931, older builds use 8080. The preset
# stays on 8080, so a server found here is added by address (`paw models add`), not used by preset name.
SCAN_EXTRA: dict[str, tuple[str, str]] = {"llamacpp-9931": ("openai", "http://127.0.0.1:9931")}

# (port, what it usually is, API kind). Only these ports are ever tried on another computer.
KNOWN_PORTS: tuple[tuple[int, str, str], ...] = (
    (11434, "Ollama", "ollama"), (9931, "llama.cpp", "openai"), (8080, "llama.cpp", "openai"),
    (8000, "vLLM", "openai"), (1337, "Jan", "openai"), (5001, "KoboldCpp", "openai"), (1234, "LM Studio", "openai"),
)
PORT_LIST = ", ".join(str(p) for p, _, _ in KNOWN_PORTS)
_LAN_RANGES = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")]
MAX_RANGE = 1024
SCAN_CONNECT_TIMEOUT = 0.5
SCAN_CONCURRENCY = 128
SCAN_DEADLINE = 25.0
NO_KEY_WARNING = ("Anyone on your network can use this server. "
                  "See 'Models on another computer' in the README.")


def is_loopback(url: str) -> bool:
    host = (urlparse(url if "://" in url else f"http://{url}").hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Server(LocalServer):
    problem: str = ""  # "" = answered with a model list; "key" = 401 or 403; "other" = answered, not a model server
    ms: int = 0        # time to answer the list request


async def _list_models(client: httpx.AsyncClient, name: str, kind: str, url: str, details: bool) -> Server | None:
    """List the models on one server. None when nothing answered. `client` carries any key and the timeouts."""
    import time

    from micropaw.providers.ollama import fetch_show
    start = time.monotonic()

    def took() -> int:
        return int((time.monotonic() - start) * 1000)

    try:
        if kind == "ollama":
            resp = await client.get(f"{url}/api/tags")
            resp.raise_for_status()
            raw = resp.json().get("models") or []
            infos = await asyncio.gather(*(fetch_show(client, url, m.get("name", "")) for m in raw)) \
                if details else [None] * len(raw)
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
    except httpx.HTTPStatusError as e:
        return Server(name, url, [], "key" if e.response.status_code in (401, 403) else "other", took())
    except (ValueError, KeyError, TypeError, AttributeError):
        return Server(name, url, [], "other", took())
    except httpx.HTTPError:
        return None
    return Server(name, url, models, "", took())


async def probe_target(name: str, kind: str, url: str, key: str = "", details: bool = True,
                       connect: float | None = None) -> Server | None:
    """One server, with its own client so a key only ever goes to that server. Connect timeout: 0.3 s
    on this computer, 1.5 s for another one (Wi-Fi, a sleeping box)."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    timeout = httpx.Timeout(3.0, connect=connect if connect is not None else 0.3 if is_loopback(url) else 1.5)
    async with net.client(url, timeout=timeout, headers=headers) as client:  # LAN hosts skip HTTP proxies
        return await _list_models(client, name, kind, url.rstrip("/"), details)


async def scan_many(targets: dict[str, tuple[str, ...]]) -> list[Server]:
    """Probe targets {name: (kind, url[, key])} in parallel. Returns the ones that answered, including ones that
    want a key. A key goes to its own server only."""
    found = await asyncio.gather(*(probe_target(n, t[0], t[1], t[2] if len(t) > 2 else "")
                                   for n, t in targets.items()))
    return [s for s in found if s and s.problem != "other"]


def _hostport(url: str) -> tuple[str, int]:
    p = urlparse(url if "://" in url else f"http://{url}")
    host = (p.hostname or "").lower()
    return ("127.0.0.1" if host == "localhost" else host), p.port or (443 if p.scheme == "https" else 80)


def _netloc(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"


def where(url: str) -> str:
    """"this computer", "tailnet" or "local network"."""
    host = _hostport(url)[0]
    if is_loopback(url):
        return "this computer"
    try:
        if ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10"):
            return "tailnet"
    except ValueError:
        if host.endswith(".ts.net"):
            return "tailnet"
    return "local network"


def kind_for(url: str) -> str:
    """The API kind a known port usually speaks."""
    port = _hostport(url)[1]
    return next((k for p, _, k in KNOWN_PORTS if p == port), "openai")


def provider_kind(entry: object) -> str:
    return getattr(entry, "kind", "openai")


def provider_root(entry_url: str, kind: str) -> str:
    """The address `_probe` wants: no trailing /v1 (it adds its own path)."""
    url = entry_url.rstrip("/")
    return url[:-3] if kind != "ollama" and url.endswith("/v1") else url


def _entries_know_ollama() -> bool:
    """True when a providers entry can say it is an Ollama server (it has a `kind`). Without that, the entry is
    used as an OpenAI-style server, so an Ollama box is saved at its /v1 address, which Ollama also serves."""
    from micropaw.config import CustomProviderConfig
    return "kind" in CustomProviderConfig.model_fields


def stored_url(url: str, kind: str) -> str:
    """The address saved in config. OpenAI-style servers keep their /v1, like the presets do."""
    native = kind == "ollama" and _entries_know_ollama()
    return url if native or urlparse(url).path.rstrip("/") else f"{url}/v1"


# ---------------------------------------------------------------------------
# One host, one address
# ---------------------------------------------------------------------------

async def probe_host(host: str, key: str = "", details: bool = True) -> list[Server]:
    """Try every known model port on one host at once (1.5 s to connect). Returns the ones that answered."""
    async def one(port: int, label: str, kind: str) -> Server | None:
        url = f"http://{_netloc(host, port)}"
        found = await probe_target(label, kind, url, key, details)
        return found if found and found.problem != "other" else None

    return [s for s in await asyncio.gather(*(one(*k) for k in KNOWN_PORTS)) if s]


async def detect(url: str, kind: str, key: str = "", details: bool = True) -> tuple[str, Server | None]:
    """(kind, server) for one address. kind "auto" tries Ollama's list first, then the OpenAI-style one."""
    order = ("ollama", "openai") if kind == "auto" else (kind,)
    for k in order:
        found = await probe_target(k, k, provider_root(url, k), key, details)
        if found and found.problem != "other":
            return k, found
    return order[0], None


# ---------------------------------------------------------------------------
# paw models add / remove
# ---------------------------------------------------------------------------

def check_name(name: str) -> str | None:
    from micropaw.config import _AGENT_NAME
    if not _AGENT_NAME.match(name):
        return "The name must be lowercase letters, digits, - or _ (up to 32), starting with a letter or digit."
    if name in BUILTIN or name in PRESETS or name == "local":
        return f"'{name}' is a built-in or preset name. Pick another name for your server."
    return None


def refusal_for(config: Config, name: str, url: str, key_env: str) -> str | None:
    """Why the registry would not accept this entry (plain http to a public host, and so on), else None."""
    from micropaw.config import CustomProviderConfig
    if text := _outside_network(url, key_env):
        return text
    trial = config.model_copy(deep=True)
    trial.providers[name] = CustomProviderConfig(base_url=url, api_key_env=key_env)
    problems = Registry(trial).problems
    return problems[0] if problems else None


def _outside_network(url: str, key_env: str) -> str | None:
    """The reason a server address is not on this computer or your network, in words that name the real cause."""
    if privacy.is_local_url(url):
        return None
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return (f"Could not find {host} on your network. Use its IP address (for example 192.168.1.50), "
                "or add the name to privacy.lan_hosts.")
    if privacy.local_only():
        return f"{host} is not on this computer or your network, and this privacy mode only uses those."
    if parsed.scheme == "http":
        what = "your messages and key" if key_env else "your messages"
        return (f"{url} is plain http to a host outside your own network, so {what} would travel in clear text. "
                "Use https.")
    return None


def read_raw() -> dict:
    import json

    import micropaw.config as config_mod
    path = config_mod.CONFIG_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def uses_of(raw: dict, name: str) -> list[str]:
    """Where a provider name is used as a spec: agents, models slots, fallbacks."""
    def is_it(spec: object) -> bool:
        return isinstance(spec, str) and parse_spec(spec)[0] == name

    out = []
    for agent, cfg in (raw.get("agents") or {}).items():
        for role in ("model", "fast", "vision"):
            if is_it(cfg.get(role)):
                out.append(f"agents.{agent}.{role}")
        if any(is_it(s) for s in cfg.get("fallbacks") or []):
            out.append(f"agents.{agent}.fallbacks")
    for role in ("fast", "vision"):
        if is_it((raw.get("models") or {}).get(role)):
            out.append(f"models.{role}")
    if any(is_it(s) for s in raw.get("fallback_providers") or []):
        out.append("fallback_providers")
    if is_it(raw.get("provider")):
        out.append("provider")
    return out


def write_entry(name: str, url: str, kind: str, key_env: str, use: str, spec: str, config: Config) -> None:
    """Save the providers entry and, with `use`, put the spec where the owner asked."""
    from micropaw.config import update_config_file

    def change(raw: dict) -> None:
        raw.setdefault("schema_version", SCHEMA_VERSION)
        entry: dict = {"base_url": url}
        if kind == "ollama" and _entries_know_ollama():
            entry["kind"] = "ollama"
        if key_env:
            entry["api_key_env"] = key_env
        raw.setdefault("providers", {})[name] = entry
        if use == "main":
            raw.setdefault("agents", {}).setdefault("main", {})["model"] = spec
        elif use in ("fast", "vision"):
            raw.setdefault("models", {})[use] = spec
        elif use == "fallback":
            raw["fallback_providers"] = [*(raw.get("fallback_providers") or []), spec]

    update_config_file(change, validate=lambda c: _no_problem(c, name))


def _no_problem(config: Config, name: str) -> None:
    problems = [p for p in Registry(config).problems if f"'{name}'" in p]
    if problems:
        raise ValueError(problems[0])


# ---------------------------------------------------------------------------
# paw models scan
# ---------------------------------------------------------------------------

@dataclass
class Target:
    name: str          # key given to scan_local, and the spec prefix when the registry can use it
    kind: str
    url: str
    key: str = ""
    configured: bool = False  # named in the config: say so when it does not answer
    label: str = ""           # for --host results


def scan_targets(config: Config, hosts: tuple[str, ...] = ()) -> list[Target]:
    """This computer's usual ports, the Ollama host, every providers entry, and each --host."""
    targets: dict[tuple[str, int], Target] = {}

    def add(t: Target) -> None:
        targets[_hostport(t.url)] = t  # a configured entry replaces a default on the same address

    for name, (kind, url) in {**LOCAL_SERVERS, **SCAN_EXTRA}.items():
        add(Target(name, kind, url))
    host = config.provider_config.ollama.host
    if (here := targets.get(_hostport(LOCAL_SERVERS["ollama"][1]))) and _hostport(host) != _hostport(here.url):
        here.name = "ollama-here"  # the configured Ollama is elsewhere; this computer's one is still listed
    add(Target("ollama", "ollama", host, configured=not is_loopback(host)))
    for name, entry in config.providers.items():
        kind = provider_kind(entry)
        key = os.environ.get(entry.api_key_env, "") if entry.api_key_env else ""
        add(Target(name.lower(), kind, provider_root(entry.base_url, kind), key, configured=True))
    count = 0
    for raw in hosts:
        url = server_url(raw)
        if urlparse(url).port or "://" in raw.strip():
            count += 1
            add(Target(f"host-{count}", kind_for(url), url, label=raw, configured=True))
        else:
            h = _hostport(url)[0]
            for port, label, kind in KNOWN_PORTS:
                count += 1
                add(Target(f"host-{count}", kind, f"http://{_netloc(h, port)}", label=f"{label} on {h}"))
    return list(targets.values())


def local_only_targets(targets: list[Target]) -> tuple[list[Target], list[Target]]:
    """(targets to scan, targets skipped). In local and air-gapped modes only this computer and your network
    are scanned, so no key or connection goes to another address."""
    if not privacy.local_only():
        return targets, []
    kept = [t for t in targets if privacy.is_local_url(t.url)]
    return kept, [t for t in targets if t not in kept]


async def run_scan(targets: list[Target]) -> tuple[list[tuple[Target, Server]], list[Target]]:
    """(answered, configured targets that did not answer)."""
    targets, _ = local_only_targets(targets)
    by_name = {t.name: t for t in targets}
    found = await scan_many({t.name: (t.kind, t.url, t.key) for t in targets})
    answered = [(by_name[s.name], s) for s in found if s.name in by_name]
    done = {t.name for t, _ in answered}
    return answered, [t for t in targets if t.configured and t.name not in done]


def usable_spec(config: Config, target: Target, server: Server) -> str:
    """The provider name the registry already knows for this address, or "" when it has to be added first."""
    hp = _hostport(server.url)
    if target.name == "ollama" and hp == _hostport(config.provider_config.ollama.host):
        return "ollama"
    if target.name in config.providers and hp == _hostport(config.providers[target.name].base_url):
        return target.name
    if target.name in PRESETS and hp == _hostport(PRESETS[target.name].base_url):
        return target.name
    return ""


def suggested_name(url: str) -> str:
    return ("lan-" + re.sub(r"[^a-z0-9]+", "-", _hostport(url)[0]).strip("-"))[:32].rstrip("-")


# ---------------------------------------------------------------------------
# paw models scan --lan / --range
# ---------------------------------------------------------------------------

def own_ip() -> str | None:
    """This computer's address on the default route. A UDP connect sends no packet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def lan_range(cidr: str | None = None) -> tuple[ipaddress.IPv4Network, list[str]]:
    """The network to scan and its host addresses. Raises ValueError with a plain message."""
    if cidr is None:
        ip = own_ip()
        try:
            addr = ipaddress.IPv4Address(ip) if ip else None
        except ValueError:
            addr = None
        if addr is None or not any(addr in n for n in _LAN_RANGES):
            raise ValueError("Could not find this computer's network address. Run with --range 192.168.1.0/24.")
        net = ipaddress.ip_network(f"{addr}/24", strict=False)
    else:
        try:
            net = ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError:
            raise ValueError(f"'{cidr}' is not a network. Example: --range 192.168.1.0/24.") from None
        if net.version != 4 or not any(net.subnet_of(n) for n in _LAN_RANGES):
            raise ValueError("Only private IPv4 ranges can be scanned: 10.x, 172.16 to 172.31, 192.168.x "
                             "or Tailscale 100.64 to 100.127.")
        if net.num_addresses > MAX_RANGE:
            raise ValueError(f"That range has {net.num_addresses} addresses. The most that can be scanned "
                             f"at once is {MAX_RANGE}.")
    return net, [str(h) for h in net.hosts()]


@dataclass
class LanHit:
    host: str
    port: int
    label: str
    kind: str
    server: Server = field(repr=False)

    @property
    def url(self) -> str:
        return f"http://{_netloc(self.host, self.port)}"


async def scan_lan(hosts: list[str], *, timeout: float = SCAN_CONNECT_TIMEOUT, limit: int = SCAN_CONCURRENCY,
                   deadline: float = SCAN_DEADLINE) -> list[LanHit]:
    """Stage 1: a TCP connect to each known port. Stage 2: a model-list GET, only to the ports that opened,
    with no key. At most `limit` connects at once and `deadline` seconds in all."""
    gate = asyncio.Semaphore(limit)
    open_ports: list[tuple[str, int, str, str]] = []

    async def knock(host: str, port: int, label: str, kind: str) -> None:
        async with gate:
            try:
                _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
            except (TimeoutError, OSError):
                return
            w.close()
            try:
                await w.wait_closed()
            except OSError:
                pass
            open_ports.append((host, port, label, kind))

    tasks = [asyncio.ensure_future(knock(h, p, lbl, k)) for h in hosts for p, lbl, k in KNOWN_PORTS]
    _done, pending = await asyncio.wait(tasks, timeout=deadline)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    async def ask(host: str, port: int, label: str, kind: str) -> LanHit | None:
        url = f"http://{_netloc(host, port)}"
        found = await probe_target(label, kind, url, "", False, connect=1.5)
        return LanHit(host, port, label, kind, found) if found and found.problem != "other" else None

    hits = await asyncio.gather(*(ask(*o) for o in sorted(open_ports)))
    return [h for h in hits if h]


# ---------------------------------------------------------------------------
# paw doctor: one row per model server in use
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    url: str                 # no trailing /v1
    kind: str                # ollama | openai | speech
    key_env: str = ""
    models: set[str] = field(default_factory=set)
    used_by: set[str] = field(default_factory=set)
    optional: bool = False   # only memory search with embedding auto uses it: down is a warning, not a failure


def doctor_endpoints(config: Config) -> list[Endpoint]:
    """Every distinct model, embedding and speech server this config uses that is not a cloud service."""
    from micropaw.core.profile import ProfileError, resolve_profiles
    registry = Registry(config)
    found: dict[tuple[str, int], Endpoint] = {}

    def add(url: str, kind: str, key_env: str = "", model: str = "", role: str = "", optional: bool = False) -> None:
        ep = found.setdefault(_hostport(url), Endpoint(url.rstrip("/"), kind, key_env, optional=optional))
        ep.optional = ep.optional and optional  # a second use that needs it makes it required
        if model:
            ep.models.add(model)
        if role:
            ep.used_by.add(role)

    try:
        profiles = resolve_profiles(config, registry).values()
    except ProfileError:
        profiles = []
    for p in profiles:
        for role, spec in (("main", p.main), ("fast", p.fast), ("vision", p.vision),
                           *(("fallback", s) for s in p.fallbacks)):
            ep = registry.resolve(spec) if spec else None
            if ep and ep.local:
                kind = "ollama" if ep.kind == "ollama" else "openai"
                add(provider_root(ep.base_url, kind), kind, ep.key_env, ep.model, f"{p.name} {role}")
    if config.embedding.enabled != "false" and config.embedding.provider in ("auto", "ollama"):
        add(config.provider_config.ollama.host, "ollama", role="memory search",
            optional=config.embedding.enabled != "true" and config.embedding.provider == "auto")
    for spec, url, role in ((config.voice.stt, config.voice.stt_url, "speech to text"),
                            (config.voice.tts, config.voice.tts_url, "text to speech")):
        if spec.startswith("local:"):
            add(url, "speech", role=role)
    return list(found.values())


async def _check(ep: Endpoint) -> tuple[str, str]:
    """(mark, detail) for one server. mark is pass, warn or fail."""
    mark, detail = await _probe_endpoint(ep)
    if mark == "fail" and ep.optional:
        return "warn", detail + ". Not needed: memory search picks another method"
    return mark, detail


async def _probe_endpoint(ep: Endpoint) -> tuple[str, str]:
    key = os.environ.get(ep.key_env, "") if ep.key_env else ""
    scope = where(ep.url)
    if ep.kind == "speech":
        try:
            async with net.client(ep.url, timeout=httpx.Timeout(3.0, connect=2.0)) as c:
                await c.get(ep.url)
        except httpx.HTTPError:
            return "fail", f"{scope}, asleep or unreachable"
        return "pass", f"{scope}, answers"
    server = await probe_target(ep.kind, ep.kind, ep.url, key, False, connect=2.0)
    if server is None:
        return "fail", f"{scope}, asleep or unreachable"
    if server.problem == "key":
        return "fail", f"{scope}, 401: key missing or rejected" + (f" (check {ep.key_env} in ~/.micropaw/.env)"
                                                                  if ep.key_env else "")
    if server.problem:
        return "warn", f"{scope}, answered, but it is not a model server"
    have = {m.name for m in server.models}
    missing = sorted(m for m in ep.models if m not in have and f"{m}:latest" not in have)
    if missing:
        return "warn", f"{scope}, model {missing[0]} not on that server"
    if not is_loopback(ep.url) and not key:
        return "warn", f"{scope}, {server.ms} ms. Answered without a key: anyone on your network can use it"
    return "pass", f"{scope}, {server.ms} ms"


async def doctor_rows(config: Config) -> list[tuple[str, str, str]]:
    """(name, mark, detail) per server, all probed at once (2 s to connect)."""
    eps = doctor_endpoints(config)
    results = await asyncio.gather(*(_check(e) for e in eps))
    return [(f"Server {_hostport(e.url)[0]}:{_hostport(e.url)[1]}", mark,
             detail + (f" ({', '.join(sorted(e.used_by))})" if e.used_by else ""))
            for e, (mark, detail) in zip(eps, results)]
