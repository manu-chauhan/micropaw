"""Where your data goes, in plain words: `paw privacy`, /privacy, /status and the line at the end of a reply."""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from micropaw import modelscan, privacy, webfetch
from micropaw.config import CONFIG_DIR, CONFIG_FILE, Config

_MODE_TEXT = {
    "airgapped": "air-gapped: nothing leaves this computer, and your network only if you allow it",
    "local": "local: models, memory search and speech stay on this computer or your network; web search "
             "only through your own server",
    "normal": "normal: the services you set up are used, and each reply says which one answered",
}
_VENDOR_CHANNELS = {"telegram": "Telegram", "discord": "Discord", "slack": "Slack", "whatsapp": "WhatsApp (Meta)"}


def destination(url: str) -> tuple[str, str]:
    """(host, where) with where one of: this computer, your network, your tailnet, the internet."""
    host = urlparse(url if "://" in url else f"http://{url}").netloc.rpartition("@")[2] or url
    if not privacy.is_local_url(url):
        return host, "the internet"
    return host, {"this computer": "this computer", "tailnet": "your tailnet"}.get(modelscan.where(url), "your network")


def _model_row(spec: str, registry) -> tuple[str, str, str]:
    """(host, where, scheme)."""
    ep = registry.resolve(spec) if spec else None
    if ep is None:
        return spec or "(not set)", "unknown", ""
    host, where = destination(ep.base_url)
    return host, where, urlparse(ep.base_url).scheme


_NEARBY = ("your network", "your tailnet")


def status_line(config: Config | None = None) -> str:
    """"Privacy: local (your network allowed)"."""
    from micropaw.config import load_config
    cfg = config or load_config()
    mode = privacy.mode()
    extra = ""
    if mode != "normal":
        extra = " (your network allowed)" if cfg.privacy.allow_lan else " (this computer only)"
    pending = cfg.privacy.mode
    if pending != mode:
        extra += f". Pending after restart: {pending}"
    return f"Privacy: {mode}{extra}"


def rows(config: Config, hide_lan: bool = False) -> list[tuple[str, str, str, str]]:
    """(what, goes to, where, scheme) for every place data can go. The scheme is only set for models.
    hide_lan leaves out the address of anything on your network: a vendor chat app shows it to the vendor
    and to everyone in a group."""
    from micropaw.core.profile import ProfileError, resolve_profiles
    from micropaw.providers.registry import Registry
    from micropaw.tools import web_search
    from micropaw.utils.voice import _endpoint, tts_endpoint

    registry = Registry(config)
    out: list[tuple[str, str, str, str]] = []

    def add(what: str, to: str, where: str, scheme: str = "") -> None:
        out.append((what, to, where, scheme))

    def shown(host: str, where: str) -> str:
        return "a server" if hide_lan and where in _NEARBY else host

    def at(host: str, where: str) -> str:
        return "" if hide_lan and where in _NEARBY else f" at {host}"

    try:
        profiles = resolve_profiles(config, registry)
    except ProfileError as e:
        profiles = {}
        add("agents", str(e), "unknown")
    for p in profiles.values():
        for role, spec in (("main", p.main), ("fast", p.fast or p.main), ("vision", p.vision),
                           *((f"fallback {i + 1}", s) for i, s in enumerate(p.fallbacks))):
            if spec:
                host, where, scheme = _model_row(spec, registry)
                add(f"{role} model ({p.name} agent)", f"{spec}{at(host, where)}", where, scheme)
    emb = config.embedding
    if emb.enabled == "false":
        add("memory search", "off", "-")
    elif emb.provider == "openai":
        add("memory search", "OpenAI, text of your messages and files", "the internet")
    elif emb.provider == "ollama" or (emb.provider == "auto" and emb.enabled == "true"):
        host, where = destination(config.provider_config.ollama.host)
        add("memory search", f"Ollama{at(host, where)}", where)
    else:
        add("memory search", "on this computer if installed", "this computer")
    for what, ep in (("speech to text", _endpoint(config) if config.voice.stt else None),
                     ("text to speech", tts_endpoint(config) if config.voice.tts else None)):
        if ep:
            host, where = destination(ep[0])
            add(what, shown(host, where), where)
        else:
            add(what, "off", "-")
    plan = web_search.plan(config.search, privacy.mode(), False)
    if config.search.provider == "off":
        add("web search", "off", "-")
    elif plan.refusal:
        add("web search", "refused: " + plan.refusal, "-")
    else:
        names = {"kiwix": ("Kiwix", config.search.kiwix_url), "docs": ("your documents", ""),
                 "searxng": ("your SearXNG", config.search.searxng_url),
                 "public": ("public SearXNG instances", ""), "brave": ("Brave Search", ""),
                 "duckduckgo": ("DuckDuckGo", "")}
        for b in plan.backends:
            label, url = names[b]
            where = destination(url)[1] if url else ("this computer" if b == "docs" else "the internet")
            add("web search", label + (at(destination(url)[0], where) if url else ""), where)
    allow = webfetch.privacy_allow(config)
    fetch_on = privacy.mode() == "normal" or (privacy.mode() == "local" and "web_fetch" in allow)
    add("web pages (http_request)", "any public site you approve" if fetch_on else "off",
        "the internet" if fetch_on else "-")
    browse_on = privacy.mode() == "normal" or (privacy.mode() == "local" and "web_browse" in allow)
    add("web browser", "any public site" if browse_on else "off", "the internet" if browse_on else "-")
    for name, vendor in _VENDOR_CHANNELS.items():
        if getattr(config.channels, name).enabled:
            add(f"channel {name}", f"every message passes through {vendor}", "the internet")
    for name in ("terminal", "webchat"):
        if getattr(config.channels, name).enabled:
            host = config.channels.webchat.host if name == "webchat" else "this computer"
            add(f"channel {name}", host,
                "this computer" if host in ("127.0.0.1", "localhost", "this computer") else "your network")
    for name, srv in config.mcp_servers.items():
        if not srv.enabled:
            continue
        if srv.url:
            host, where = destination(srv.url)
            add(f"MCP {name}", f"{shown(host, where)}. MicroPaw cannot see where that server sends data", where)
        else:
            add(f"MCP {name}", "a program on this computer, which may use the network itself", "this computer")
    return out


def facts(config: Config) -> list[tuple[str, str]]:
    from micropaw.tools import sandbox
    out = [("Mode", _MODE_TEXT[privacy.mode()])]
    guard = getattr(privacy, "guard_installed", None)
    if guard is not None:
        out.append(("Egress guard", "installed" if (guard() if callable(guard) else guard) else "not installed"))
    out.append(("Shell isolation", sandbox.describe()))
    if os.name != "nt":
        modes = []
        for label, path in (("config file", CONFIG_FILE), ("data folder", CONFIG_DIR)):
            if path.exists():
                modes.append(f"{label} {path.stat().st_mode & 0o777:03o}")
        out.append(("File permissions", ", ".join(modes) or "nothing saved yet"))
    storage = getattr(config, "storage", None)
    days = getattr(storage, "retention_days", None)
    out.append(("Keeps messages", f"{days} days" if days else "until you delete them"))
    blocked = getattr(privacy, "last_blocked", None)
    if callable(blocked) and (hosts := blocked()):
        out.append(("Last blocked", ", ".join(hosts[-5:])))
    return out


def render(config: Config, platform: str = "terminal") -> str:
    """Plain text for /privacy and `paw privacy`. On a vendor chat channel no address on your network is shown."""
    lines = [status_line(config), ""]
    lines += [f"{k}: {v}" for k, v in facts(config)]
    lines += ["", "Where data goes:"]
    lines += [f"- {what}: {to} ({where})" for what, to, where, _ in rows(config, platform in _VENDOR_CHANNELS)]
    lines += ["", "A local address is not proof that a server keeps data local: a proxy such as LiteLLM or "
                  "Open WebUI can forward to the cloud. MicroPaw cannot check that."]
    return "\n".join(lines)


def label(spec: str, registry, config: Config, platform: str = "terminal") -> str:
    """"via claude (api.anthropic.com)" or "via ollama on this machine". On a channel run by a vendor (and so
    seen by everyone in a group chat) a model on your network is named without its address."""
    ep = registry.resolve(spec) if spec else None
    if ep is None:
        return f"via {spec}" if spec else ""
    host, where = destination(ep.base_url)
    if where == "this computer":
        return f"via {ep.name} on this machine"
    if ep.name == "ollama" and host.endswith("ollama.com"):
        return "via Ollama cloud (ollama.com)"
    if where != "the internet" and platform in _VENDOR_CHANNELS:
        return f"via {ep.name}, {where}"
    return f"via {ep.name} ({host}{'' if where == 'the internet' else ', ' + where})"


_ANSWERED_BY = re.compile(r"\[answered by (\S+?) because")


def reply_label(spec: str, response: str, registry, config: Config, platform: str = "terminal") -> str:
    """The line for the end of a reply. A reply that a fallback answered names that provider instead."""
    if not config.display.show_provider:
        return ""
    if m := _ANSWERED_BY.search(response):
        spec = m.group(1)
    text = label(spec, registry, config, platform)
    return f"\n\n[{text}]" if text else ""


def advice(config: Config) -> list[tuple[str, str, str]]:
    """(check, mark, detail) for `paw doctor`. mark is pass, warn or note."""
    import json
    import sys
    from pathlib import Path

    from micropaw.tools import web_search

    mode = privacy.mode()
    out: list[tuple[str, str, str]] = [("Privacy mode", "pass", status_line(config).removeprefix("Privacy: "))]
    table = rows(config)
    models = [(what, to, where, scheme) for what, to, where, scheme in table if " model (" in what]
    cloud = [f"{what.split(' model')[0]} {to.split(' at ')[0]}" for what, to, where, _ in models
             if where == "the internet"]
    if mode == "normal" and cloud:
        out.append(("Cloud models", "warn", "Messages go to the internet for: " + ", ".join(dict.fromkeys(cloud))))
    plain = [to.split(" at ")[1] for _, to, where, scheme in models
             if where == "your network" and scheme == "http" and " at " in to]
    if plain:
        out.append(("Model on your network", "warn", f"Plain http, not encrypted: {', '.join(dict.fromkeys(plain))}. "
                    "Use Tailscale or https if others share the network."))
    searx = config.search.searxng_url
    if mode == "local" and searx and "searxng" in web_search.plan(config.search, mode, False).backends \
            and not privacy.is_local_url(searx):
        out.append(("Search server", "warn", f"Your SearXNG at {destination(searx)[0]} is on the internet, not on "
                    "this computer or your network. Each query goes out only after you approve it."))
    if any(to.startswith("ollama") and where == "this computer" for _, to, where, _ in models):
        server = Path.home() / ".ollama" / "server.json"
        try:
            off = json.loads(server.read_text()).get("disable_ollama_cloud") is True
        except (OSError, ValueError):
            off = None
        if off is None:
            out.append(("Ollama cloud models", "note", f"{server} not found. Ollama can still serve cloud models "
                        "(names ending in -cloud). MicroPaw refuses them in local and air-gapped mode."))
        elif not off:
            out.append(("Ollama cloud models", "warn",
                        f"Add \"disable_ollama_cloud\": true to {server} and restart Ollama."))
    if mode == "airgapped" and sys.platform in ("darwin", "win32"):
        out.append(("Ollama updates", "note", "Ollama on macOS and Windows updates itself over the internet. "
                    "Turn that off in its settings."))
    out.append(("Disk encryption", "note", "MicroPaw cannot check it. Use BitLocker, FileVault or LUKS if this "
                "computer can be lost or stolen."))
    return out
