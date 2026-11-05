"""Privacy mode: one setting, read once, frozen for the process. Stricter always wins.

airgapped > local > normal. Other modules ask `mode()`, `airgapped()`, `local_only()`. This module also holds
the locality policy (what counts as "this side"), the config check that lists every refusal at once, the
egress guard setup, short-lived permits, and the rules for tools and owner commands by mode.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from micropaw import egress
from micropaw.egress import EgressBlocked, Guard

if TYPE_CHECKING:
    from micropaw.config import Config, PrivacyConfig
    from micropaw.providers.registry import Registry

_ORDER = {"normal": 0, "local": 1, "airgapped": 2}
_mode: str | None = None


def _strictest(*modes: str) -> str:
    valid = [m for m in modes if m in _ORDER]
    return max(valid, key=_ORDER.__getitem__) if valid else "normal"


class PrivacyError(Exception):
    """The config asks for something the privacy mode forbids. The text lists every refusal."""


def parse_mode(value: object, where: str = "privacy.mode") -> str:
    """normal, local or airgapped from what the owner wrote. Case, spaces, '-' and '_' do not matter, so
    "air-gapped" works. An empty value is "". Anything else is an error: a typo must not mean no guard."""
    text = re.sub(r"[\s_-]", "", str(value or "").lower())
    if text and text not in _ORDER:
        raise PrivacyError(f'{where} is "{value}", which is not a privacy mode. Use normal, local or airgapped. '
                           "Not starting.")
    return text


def env_mode() -> str:
    return parse_mode(os.environ.get("MICROPAW_PRIVACY", ""), "MICROPAW_PRIVACY")


def freeze(config_mode: str) -> str:
    """Set the mode for this process once. The MICROPAW_PRIVACY env var can only tighten it."""
    global _mode
    if _mode is None:
        _mode = _strictest(config_mode, env_mode())
    else:
        _mode = _strictest(_mode, config_mode)  # never loosens while running
    return _mode


def _record_file():
    from micropaw.config import CONFIG_DIR
    return CONFIG_DIR / "privacy_last_mode"


def remembered(chosen: str) -> tuple[str, str]:
    """The mode to run with and a loud note when it differs from the config. A config that is looser than the
    last run (a tool or a child process may have rewritten it) is not obeyed until the owner confirms it at a
    terminal with MICROPAW_CONFIRM_PRIVACY=<mode>. The record is updated to what runs now."""
    path = _record_file()
    try:
        last = path.read_text().strip()
    except OSError:
        last = ""
    note = ""
    if last in _ORDER and _ORDER[last] > _ORDER[chosen]:
        if os.environ.get("MICROPAW_CONFIRM_PRIVACY", "").strip().lower() == chosen:
            note = f"privacy.mode is now {chosen}, as you confirmed (it was {last})."
        else:
            note = (f"privacy.mode in config.json is {chosen}, but this install last ran as {last}. Staying at "
                    f"{last}. To loosen it on purpose, start once with MICROPAW_CONFIRM_PRIVACY={chosen}.")
            chosen = last
    if chosen != "normal" or last:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(chosen)
        except OSError:
            pass
    return chosen, note


def confirm_mode(chosen: str, config: Config | None = None) -> None:
    """The owner chose this mode (and, with `config`, these settings) at a terminal: remember it as the new floor."""
    try:
        _record_file().write_text(chosen)
    except OSError:
        pass
    if config is not None:
        _write_settings(chosen, config)


# What the guard and the locality rules read. A config that is looser than the last strict run in any of these is
# held back to the old values until the owner confirms it, like the mode itself.
_FLAGS = ("allow_lan", "allow_cloud_fallback", "allow_cloud_helpers", "share_with_cloud")
_LISTS = ("lan_hosts", "allow", "trusted_local_services")
_floor: dict | None = None


def _settings_file():
    from micropaw.config import CONFIG_DIR
    return CONFIG_DIR / "privacy_last_settings"


def _items(values) -> list[str]:
    return [str(v).strip().lower() for v in values if str(v).strip()]


def _snapshot(config: Config) -> dict:
    p = config.privacy
    out: dict = {f: bool(getattr(p, f)) for f in _FLAGS}
    out.update({f: _items(getattr(p, f)) for f in _LISTS})
    out["mcp"] = {name: {"network": bool(srv.network), "paths": _items(srv.paths),
                         "write_paths": _items(srv.write_paths)} for name, srv in config.mcp_servers.items()}
    return out


def _write_settings(mode: str, config: Config) -> None:
    import json
    try:
        path = _settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": mode, "settings": _snapshot(config)}))
    except OSError:
        pass


def _tighten(config: Config, prev: dict) -> list[str]:
    """Put every setting that is looser than `prev` back to the old value, in place. Returns what was held."""
    p, held = config.privacy, []
    for f in _FLAGS:
        if getattr(p, f) and not prev.get(f, True):
            setattr(p, f, False)
            held.append(f)
    for f in _LISTS:
        if f not in prev:
            continue
        cur = list(getattr(p, f))
        keep = [x for x in cur if not str(x).strip() or str(x).strip().lower() in prev[f]]
        if len(keep) != len(cur):
            setattr(p, f, keep)
            held.append(f)
    for name, srv in config.mcp_servers.items():
        old = prev.get("mcp", {}).get(name, {})
        if srv.network and not old.get("network"):
            srv.network = False
            held.append(f"mcp_servers.{name}.network")
        for f in ("paths", "write_paths"):
            cur = list(getattr(srv, f))
            keep = [x for x in cur if str(x).strip().lower() in old.get(f, ())]
            if len(keep) != len(cur):
                setattr(srv, f, keep)
                held.append(f"mcp_servers.{name}.{f}")
    return held


def hold_loosened(config: Config, mode: str) -> str:
    """Apply the held-back rule to the guard settings of `config` (in place) and record what runs now. `mode` is
    the mode this install runs with (after `remembered`). Returns a loud note, or ""."""
    import json
    global _floor
    try:
        last = json.loads(_settings_file().read_text())
    except (OSError, ValueError):
        last = {}
    prev = last.get("settings") if last.get("mode") in ("local", "airgapped") else None
    note = ""
    if prev and os.environ.get("MICROPAW_CONFIRM_PRIVACY", "").strip().lower() != mode:
        if held := _tighten(config, prev):
            _floor = prev
            note = ("These privacy settings in config.json are looser than the last run: " + ", ".join(held)
                    + f". Keeping the old values. To use them on purpose, start once with "
                    f"MICROPAW_CONFIRM_PRIVACY={mode}.")
    if mode != "normal" or last:
        _write_settings(mode, config)
    return note


def apply_floor(config: Config) -> None:
    """A config loaded later in a process that is holding settings back gets the same held values."""
    if _floor is not None:
        _tighten(config, _floor)


def mode() -> str:
    if _mode is None:
        from micropaw.config import load_config
        cfg = load_config()
        configure(cfg.privacy)
        return freeze(cfg.privacy.mode)
    return _mode


def airgapped() -> bool:
    return mode() == "airgapped"


def local_only() -> bool:
    """True in local and airgapped: models, embeddings and speech must be on this machine or the LAN."""
    return _ORDER[mode()] >= 1


# ---------------------------------------------------------------------------
# Settings: the owner's lists, read once from config.privacy
# ---------------------------------------------------------------------------

# Ranges that count as "my network" when allow_lan is on. 100.64/10 is Tailscale (and some carriers' CGNAT).
LAN_NETS = tuple(ipaddress.ip_network(n) for n in
                 ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "100.64.0.0/10"))

# What "allow" may name (checked only by the owner's config; unknown entries are ignored)
ALLOWABLE = ("telegram", "discord", "slack", "whatsapp", "google", "web_fetch", "web_browse", "shell_network")
_VENDORS = {
    "telegram": ("Telegram", "api.telegram.org"), "discord": ("Discord", "discord.com"),
    "slack": ("Slack", "slack.com"), "whatsapp": ("WhatsApp", "graph.facebook.com"),
}
# The hosts each vendor's libraries talk to (for the owner who allowed it in local mode)
_VENDOR_SUFFIXES = {
    "telegram": (".telegram.org",), "discord": (".discord.com", ".discord.gg", ".discordapp.com", ".discordapp.net"),
    "slack": (".slack.com", ".slack-edge.com", ".slack-files.com"),
    "whatsapp": (".facebook.com", ".whatsapp.net", ".fbcdn.net"),
    "google": (".googleapis.com", "accounts.google.com"),
}
_CLOUD_TAG = (":cloud", "-cloud")


@dataclass(frozen=True)
class Settings:
    allow_lan: bool = True
    nets: tuple = ()                  # addresses and ranges the owner listed in lan_hosts
    names: frozenset = frozenset()    # names the owner listed in lan_hosts (never decided by DNS)
    allow: frozenset = frozenset()
    trusted_services: frozenset = frozenset()
    cloud_fallback: bool = False
    cloud_helpers: bool = False
    public: tuple = ()                # lan_hosts entries that are not private ranges (left out, refused at start)


_settings: Settings | None = None
_config: Config | None = None
_guard: Guard | None = None
_registry: Registry | None = None


_LISTABLE = LAN_NETS + tuple(ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "169.254.0.0/16", "fe80::/10"))


def _listable(net) -> bool:
    """lan_hosts may list only this machine and private ranges. 0.0.0.0/0 or a public address would turn the
    guard's connect rule into allow-all."""
    return any(net.version == r.version and net.subnet_of(r) for r in _LISTABLE)


def _split_hosts(entries: list[str]) -> tuple[list, set[str], list[str]]:
    """lan_hosts entries: an address or range, a name, or name=address (the name counts, the address is listed).
    Also returns the entries that are not private, which are left out."""
    nets, names, bad = [], set(), []

    def add(text: str, entry: str) -> bool:
        try:
            net = ipaddress.ip_network(text.strip(), strict=False)
        except ValueError:
            return False
        if isinstance(net, ipaddress.IPv6Network) and net.network_address.ipv4_mapped:
            net = ipaddress.ip_network(f"{net.network_address.ipv4_mapped}/{max(net.prefixlen - 96, 0)}", strict=False)
        if _listable(net):
            nets.append(net)
        else:
            bad.append(entry)
        return True

    for raw in entries:
        entry = raw.strip().lower()
        if not entry:
            continue
        if "=" in entry:
            entry, _, pinned = entry.partition("=")
            add(pinned, raw.strip())
        if not add(entry, raw.strip()):
            names.add(entry.rstrip("."))
    return nets, names, bad


def configure(privacy: PrivacyConfig) -> Settings:
    """Read the owner's lists. Call once at start; the guard and the locality rules use the result."""
    global _settings, _guard, _registry
    nets, names, bad = _split_hosts(privacy.lan_hosts)
    _settings = Settings(
        allow_lan=privacy.allow_lan, nets=tuple(nets), names=frozenset(names), public=tuple(bad),
        allow=frozenset(a.lower() for a in privacy.allow),
        trusted_services=frozenset(t.lower() for t in privacy.trusted_local_services),
        cloud_fallback=privacy.allow_cloud_fallback, cloud_helpers=privacy.allow_cloud_helpers,
    )
    _guard = _registry = None
    return _settings


def settings() -> Settings:
    if _settings is None:
        from micropaw.config import load_config
        configure(load_config().privacy)
    assert _settings is not None
    return _settings


def bind_config(config: Config) -> None:
    """Remember the whole config for check_role and the registry it builds."""
    global _config, _registry
    _config = config
    _registry = None
    configure(config.privacy)


def _reset_for_tests() -> None:
    global _mode, _settings, _config, _guard, _registry, _floor
    _mode = _settings = _config = _guard = _registry = _floor = None


# ---------------------------------------------------------------------------
# Locality: what counts as this side of the line
# ---------------------------------------------------------------------------

def is_lan_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    ip = egress.parse_ip(str(ip)) or ip
    s = settings()
    if any(ip in net for net in s.nets):
        return True
    return s.allow_lan and (ip.is_link_local or any(ip in net for net in LAN_NETS))


def local_host(host: str) -> bool:
    """Strict rule for airgapped and local: loopback, "localhost", LAN addresses (when allowed) and the names the
    owner listed. A name is never decided by DNS."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host == "localhost":  # not x.localhost: the system resolver may send that name out
        return True
    ip = egress.parse_ip(host.strip("[]"))
    if ip is not None:
        return ip.is_loopback or is_lan_ip(ip)
    return host in settings().names


def is_local_url(url: str) -> bool:
    """True when the URL's host is this machine or (if allowed) the LAN or tailnet."""
    from micropaw.providers.registry import is_local_url as _registry_local
    return _registry_local(url)


def cloud_tagged(model: str) -> bool:
    """An Ollama cloud model: served through the local daemon but run on ollama.com."""
    return model.lower().endswith(_CLOUD_TAG)


def _leaves(provider: object) -> list[object]:
    """The real providers inside a chain or wrapper."""
    attrs = getattr(provider, "__dict__", {})
    inner = attrs.get("providers") or [x for x in (attrs.get("_inner"), attrs.get("inner")) if x is not None]
    if not inner:
        return [provider]
    return [leaf for p in inner for leaf in _leaves(p)]


def is_local_provider(provider: object) -> bool:
    """True when every model behind this provider (chains and wrappers included) is local."""
    from micropaw.utils.prices import is_local_provider as _prices_local
    leaves = _leaves(provider)
    return all(_prices_local(p) and not cloud_tagged(str(getattr(p, "model", ""))) for p in leaves)


def _reg() -> Registry:
    global _registry, _config
    if _registry is None:
        from micropaw.config import load_config
        from micropaw.providers.registry import Registry
        if _config is None:
            _config = load_config()
        _registry = Registry(_config)
    return _registry


def check_role(spec: str, role: str) -> str | None:
    """A plain refusal when this model spec is not allowed for the role in the current mode, else None."""
    return _reg().refusal(spec, role) if spec else None


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------

def build_guard() -> Guard:
    s = settings()
    try:
        own = {socket.gethostname()}
    except OSError:
        own = set()
    suffixes = [x for v in sorted(s.allow) if not airgapped() for x in _VENDOR_SUFFIXES.get(v, ())]
    return Guard(mode(), allow_lan=s.allow_lan, nets=s.nets, lan_nets=LAN_NETS, names=s.names, own_names=own,
                 open_suffixes=suffixes)


def _learn_vendor_addresses(g: Guard) -> None:
    """Record the addresses a vendor name resolves to. The hook allows the lookup (the name is open) but cannot
    see the answer, so the lookup function itself reports it."""
    real = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        found = real(host, *args, **kwargs)
        if g.is_open(host):
            g.learn({a[4][0] for a in found})
        return found

    socket.getaddrinfo = getaddrinfo


def guard() -> Guard:
    """The rules in force. The installed hook's own object when there is one, so permits reach it."""
    global _guard
    live = egress.installed()
    if live is not None:
        return live
    if _guard is None:
        _guard = build_guard()
    return _guard


_FORCED_ENV = {"HF_HUB_OFFLINE": "1", "OLLAMA_NO_CLOUD": "1", "HF_HUB_DISABLE_XET": "1"}
_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "WS_PROXY", "WSS_PROXY")
_ALL_ENV = {"HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
            "HF_HUB_DISABLE_UPDATE_CHECK": "1"}


def boot_env(chosen: str | None = None) -> None:
    """Environment for libraries that read it at import time. In airgapped and local the offline switches are
    forced (a value in .env cannot turn them off); telemetry is off in every mode."""
    chosen = chosen or mode()
    for key, value in _ALL_ENV.items():
        os.environ.setdefault(key, value)
    if chosen in ("local", "airgapped"):
        os.environ.update(_FORCED_ENV)
        # A proxy would hide the real target from the guard: the guard sees only the proxy's address.
        for key in _PROXY_ENV:
            os.environ.pop(key, None)
            os.environ.pop(key.lower(), None)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"  # also stops system proxy settings in httpx
    if chosen == "airgapped":
        os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"


def _system_tool_paths() -> set[str]:
    import shutil
    names = ("systemctl", "loginctl", "journalctl", "launchctl", "schtasks")
    roots = ("/usr/", "/bin/", "/sbin/", "/lib/", "/opt/", os.environ.get("SystemRoot", "C:\\Windows").lower())
    found = set()
    for n in names:
        path = shutil.which(n)
        if path and os.path.realpath(path).lower().startswith(roots):
            found.add(os.path.normcase(path))
    return found


def ensure_guard(config: Config | None = None) -> str | None:
    """Install the egress guard in local and airgapped mode. None when it is in place (or not needed), else the
    reason it is not, which the caller must treat as a refusal to start."""
    if config is not None:
        bind_config(config)
    boot_env(mode())
    if not local_only():
        return None
    g = build_guard()
    g.trusted = _system_tool_paths()
    if (why := egress.install(g)) is not None:
        return why
    global _guard
    _guard = egress.installed()
    if g.open_suffixes:
        _learn_vendor_addresses(g)
    return None


def permit(host: str, ttl: float = 120.0, ips: tuple[str, ...] = (), resolve: bool = True) -> str | None:
    """Let one host through for a short time after the owner approved a search or a fetch (local mode only).
    Resolves the name itself while its permit is live and permits the addresses it gets, unless `ips` is given
    (a proxy: the target is never resolved here). Returns a refusal text, or None."""
    if airgapped():
        return "Air-gapped mode allows no outside hosts."
    g = guard()
    g.permit(host, ttl, ips)
    if ips or not resolve or egress.parse_ip(host) is not None:
        return None
    try:
        found = {a[4][0] for a in socket.getaddrinfo(host, None)}
    except OSError:
        return f"Cannot look up {host}."
    g.permit(host, ttl, found)
    return None


def blocked(exc: BaseException | None) -> EgressBlocked | None:
    """The EgressBlocked anywhere in an exception chain (httpx wraps it as a ConnectError)."""
    seen = 0
    while exc is not None and seen < 12:
        if isinstance(exc, EgressBlocked):
            return exc
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


# ---------------------------------------------------------------------------
# Tools, owner commands and children by mode
# ---------------------------------------------------------------------------

def disabled_tools() -> set[str]:
    """Tool names that are not registered in the current mode (the gateway removes them at start)."""
    if not local_only():
        return set()
    allow = settings().allow
    out = set()
    if airgapped() or "web_fetch" not in allow:
        out.add("http_request")
    if airgapped() or "web_browse" not in allow:
        out.add("web_browse")
    if airgapped() or "google" not in allow:
        out |= {"email", "calendar"}
    return out


def tool_refusal(feature: str) -> str | None:
    """The reason a feature ('google', 'web_fetch', 'web_browse') is off in this mode, else None."""
    if not local_only():
        return None
    if airgapped():
        return f"Air-gapped mode is on, so {feature.replace('_', ' ')} is off. Change privacy.mode to use it."
    if feature not in settings().allow:
        return (f"Local mode is on, so {feature.replace('_', ' ')} is off. "
                f'Add "{feature}" to privacy.allow to turn it on.')
    return None


def channel_refusal(name: str) -> str | None:
    """Why a chat channel may not run in this mode, else None. Terminal and webchat are local."""
    if name not in _VENDORS or not local_only():
        return None
    vendor, host = _VENDORS[name]
    if not airgapped() and name in settings().allow:
        return None
    word = "Air-gapped" if airgapped() else "Local"
    return (f"{vendor} needs {host} and messages pass through {vendor} servers. {word} mode is on: "
            f'turn the channel off{"" if airgapped() else f", or add {name!r} to privacy.allow"}, '
            "or change privacy.mode.")


def owner_command_gate(what: str, hosts: tuple[str, ...] = ()) -> str | None:
    """For commands the owner runs at a terminal that need the internet (paw update, extras, model downloads).
    Airgapped refuses with a pointer to the offline route. Local permits the named hosts for a short time."""
    if airgapped():
        return (f"{what} needs the internet, and air-gapped mode is on. Install from a wheelhouse "
                "(pip install --no-index --find-links <folder>) or switch privacy.mode on a connected machine.")
    if local_only():
        for h in hosts:
            if (why := permit(h)) is not None:
                return why
    return None


def child_isolation() -> str:
    """How shell and MCP children are cut off from the network here: 'bwrap', or 'none' (with the reason in
    sandbox.describe())."""
    from micropaw.tools import sandbox
    return "bwrap" if sandbox.isolation_ok() else "none"


# ---------------------------------------------------------------------------
# Config check: every refusal at once
# ---------------------------------------------------------------------------

@dataclass
class Report:
    refusals: list[str] = field(default_factory=list)  # the config cannot run in this mode
    notices: list[str] = field(default_factory=list)   # allowed, but the owner should know

    def raise_if_any(self) -> None:
        if self.refusals:
            raise PrivacyError(
                f"privacy.mode is {mode()}, and the config asks for things it forbids:\n"
                + "\n".join(f"- {r}" for r in self.refusals))


def _voice_refusals(config: Config) -> list[str]:
    from micropaw.utils.voice import _endpoint, tts_endpoint
    out = []
    for label, ep, spec in (("voice.stt", _endpoint(config), config.voice.stt),
                            ("voice.tts", tts_endpoint(config), config.voice.tts)):
        if spec and ep is not None and not is_local_url(ep[0]):
            out.append(f"{label} {spec} would send audio or text to {urlparse(ep[0]).hostname}. "
                       "Use a local speech server or turn it off.")
    return out


def check_config(config: Config, channels: list | None = None) -> Report:
    """Every part of the config that the current mode refuses, with the setting to change."""
    rep = Report()
    if not local_only():
        return rep
    bind_config(config)
    reg = _reg()
    seen: set[tuple[str, str]] = set()

    def role(spec: str, label: str) -> None:
        if spec and (spec, label) not in seen:
            seen.add((spec, label))
            if msg := reg.refusal(spec, label):
                rep.refusals.append(msg)

    agents = dict(config.agents)
    agents.setdefault("main", None)
    for name, ac in agents.items():
        who = "" if name == "main" else f"agent {name} "
        role((ac.model if ac else "") or config.provider, f"{who}main")
        role((ac.fast if ac and ac.fast is not None else config.models.fast), f"{who}fast")
        role((ac.vision if ac and ac.vision is not None else config.models.vision), f"{who}vision")
        for spec in (ac.fallbacks if ac and ac.fallbacks is not None else config.fallback_providers):
            role(spec, f"{who}fallback")
    if os.environ.get("OLLAMA_HOST") and not is_local_url(config.provider_config.ollama.host):
        rep.refusals.append(f"OLLAMA_HOST points at {config.provider_config.ollama.host}, which is not on this "
                            "machine or a listed LAN host.")
    if config.embedding.provider == "openai":
        rep.refusals.append("embedding.provider is openai, which would send every message and file to OpenAI. "
                            "Use ollama or local embeddings.")
    for entry in settings().public:
        rep.refusals.append(f'privacy.lan_hosts has "{entry}", which is not on this machine or a private network. '
                            "Only private ranges (10.x, 172.16-31.x, 192.168.x, Tailscale 100.64-127.x) and names "
                            "can be listed.")
    rep.refusals += _voice_refusals(config)
    names = {c.name for c in channels} if channels is not None else {
        n for n in _VENDORS if getattr(config.channels, n).enabled}
    for n in sorted(names & set(_VENDORS)):
        if msg := channel_refusal(n):
            rep.refusals.append(msg)
        else:
            rep.notices.append(f"{_VENDORS[n][0]} is on by your choice: your messages pass through "
                               f"{_VENDORS[n][0]} servers in plain text.")
    if airgapped() and config.channels.webchat.enabled and not local_host(config.channels.webchat.host):
        rep.refusals.append(f"channels.webchat.host {config.channels.webchat.host} is not a loopback or listed "
                            "LAN address.")
    wc = config.channels.webchat
    ip = egress.parse_ip((wc.host or "127.0.0.1").strip("[]"))
    if wc.enabled and wc.host != "localhost" and not (ip and ip.is_loopback):
        rep.notices.append("The web chat is on your LAN over plain http. Anyone on the network can read it.")
    if airgapped() and config.permissions.sandbox in ("docker", "podman"):
        rep.notices.append("The Docker and Podman shell sandboxes pull images, so shell is off in air-gapped mode.")
    for name, srv in config.mcp_servers.items():
        if not srv.enabled:
            continue
        if msg := mcp_refusal(name, srv):
            rep.notices.append(msg)
    if config.google.enabled and (msg := tool_refusal("google")):
        rep.notices.append(msg)
    for entry in sorted(settings().allow - set(ALLOWABLE)):
        rep.notices.append(f'privacy.allow has "{entry}", which is not a known setting.')
    return rep


_DOWNLOADERS = {"npx", "uvx", "pipx", "bunx", "pnpx"}


def mcp_refusal(name: str, srv) -> str | None:
    """Why an MCP server may not start in this mode, else None."""
    if not local_only():
        return None
    if srv.url:
        host = (urlparse(srv.url).hostname or "").lower()
        if host not in ("localhost", "127.0.0.1", "::1"):  # the agent's own tools never reach the LAN
            return (f"MCP server {name} at {host} is not on this machine, so {mode()} mode refuses it. "
                    "Run it on this machine and use a localhost address.")
        if airgapped() and name.lower() not in settings().trusted_services and host not in settings().trusted_services:
            return (f"MCP server {name} runs outside MicroPaw's sandbox and MicroPaw cannot see where it sends "
                    f'data. Add "{name}" to privacy.trusted_local_services to use it.')
        return None
    base = os.path.basename(srv.command).lower().removesuffix(".cmd").removesuffix(".exe")
    if airgapped() and (base in _DOWNLOADERS or (base == "pipx" and "run" in srv.args)):
        return (f"MCP server {name} would download packages. Install the server and point its command at "
                "the local path.")
    return None


def enforce(config: Config, channels: list | None = None) -> Report:
    """At start: install the guard, list every refusal, raise PrivacyError if any. Returns the notices."""
    if (why := ensure_guard(config)) is not None:
        raise PrivacyError(f"privacy.mode is {mode()}, but the network guard is not working: {why}. Not starting.")
    rep = check_config(config, channels)
    rep.raise_if_any()
    return rep


def describe() -> str:
    """One line for status screens."""
    s = settings()
    m = mode()
    if m == "normal":
        return "Privacy: normal"
    lan = "LAN allowed" if s.allow_lan or s.nets or s.names else "this machine only"
    return f"Privacy: {m} ({lan})"

