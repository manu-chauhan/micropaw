"""Interactive setup wizard for MicroPaw."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field

from rich.console import Console
from rich.panel import Panel

from micropaw.config import (
    AgentConfig,
    Config,
    CustomProviderConfig,
    config_exists,
    load_config,
    save_config,
    server_url,
)
from micropaw.providers.registry import (
    CLOUD_DEFAULTS,
    LOCAL_SERVERS,
    LocalPick,
    LocalServer,
    pick_local,
    scan_local,
    suggest_pull,
)

console = Console()

OLLAMA_DOWNLOAD = "https://ollama.com/download"


@dataclass
class ModelPlan:
    main: str = ""  # spec ("" = keep what the user chose)
    fast: str = ""
    vision: str = ""
    fallbacks: list[str] = field(default_factory=list)


def best_local(servers: list[LocalServer], ram: int | None) -> tuple[LocalServer, LocalPick] | None:
    """The first server (Ollama first) that has a model fit to run as the main model."""
    for server in servers:
        pick = pick_local(server.models, ram)
        if pick.main:
            return server, pick
    return None


def plan_models(provider: str, servers: list[LocalServer], ram: int | None, hybrid: bool) -> ModelPlan:
    """Default models by what is available (plan 4.1). Cloud: main and fast from one lab, or hybrid with a
    small local model for quick jobs and a local fallback. Local only: main = largest that fits, fast = main."""
    local = best_local(servers, ram)
    if provider in CLOUD_DEFAULTS:
        main_id, fast_id = CLOUD_DEFAULTS[provider]
        plan = ModelPlan(main=f"{provider}:{main_id}", fast=f"{provider}:{fast_id}")
        if hybrid and local:
            server, pick = local
            plan.fast = f"{server.name}:{pick.fast.name}"
            plan.fallbacks = [f"{server.name}:{pick.main.name}"]
        return plan
    if local:
        server, pick = local
        plan = ModelPlan(main=f"{server.name}:{pick.main.name}")
        if pick.vision:
            plan.vision = f"{server.name}:{pick.vision.name}"
        return plan
    return ModelPlan()


def apply_plan(config: Config, plan: ModelPlan) -> None:
    """Write a plan into the config: the legacy fields for the built-in providers, agents.main for the rest."""
    if plan.main:
        name, _, model = plan.main.partition(":")
        if name in ("claude", "openai", "gemini", "ollama"):
            config.provider = name
            getattr(config.provider_config, name).model = model
        else:
            config.agents["main"] = AgentConfig(model=plan.main)
    if plan.fast:  # an empty plan value must not wipe what the user already set
        config.models.fast = plan.fast
    if plan.vision:
        config.models.vision = plan.vision
    if plan.fallbacks:
        config.fallback_providers = plan.fallbacks


def _pull(host: str, tag: str) -> bool:
    """`ollama pull` through the API, with a progress bar."""
    from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn

    from micropaw.providers.ollama import pull_model

    async def run() -> None:
        with Progress(TextColumn("{task.description}"), BarColumn(), DownloadColumn(), console=console) as bar:
            tasks: dict[str, int] = {}
            async for chunk in pull_model(host, tag):
                status = chunk.get("status", "")
                if chunk.get("total"):
                    task = tasks.setdefault(status, bar.add_task(status[:30], total=chunk["total"]))
                    bar.update(task, completed=chunk.get("completed", 0))

    try:
        asyncio.run(run())
        return True
    except Exception as e:
        console.print(f"[yellow]Download failed: {e}[/yellow]")
        return False


def _choose_models(questionary, config: Config, provider: str, where: str = "auto") -> None:
    """The model step: look for local servers, pick RAM-sized defaults, offer hybrid, offer to pull.

    `where` is the answer to "Where do your models run?": "this computer", "network", "cloud" or "mix".
    "cloud" skips the scan, "network" adds a server on another computer, "auto" is the old flow.
    """
    from micropaw.providers.ollama import ram_bytes

    if where == "cloud":
        apply_plan(config, plan_models(provider, [], None, hybrid=False))
        return
    if where == "network":
        _add_remote(questionary, config, provider)
        return

    console.print("[dim]Looking for local model servers...[/dim]")
    targets = dict(LOCAL_SERVERS)
    targets["ollama"] = ("ollama", config.provider_config.ollama.host.rstrip("/"))  # the host the user typed
    servers = asyncio.run(scan_local(targets))
    if provider == "ollama":
        servers = [sv for sv in servers if sv.name == "ollama"]
    ram = ram_bytes()
    for server in servers:
        console.print(f"[green]Found {server.name}[/green] at {server.url}: {len(server.models)} model(s)")
    hybrid = False
    if where == "mix" and provider in CLOUD_DEFAULTS and not best_local(servers, ram):
        console.print("[yellow]No model server found on this computer.[/yellow] You can add one on another computer "
                      "later with: paw models add NAME ADDRESS")
    if provider in CLOUD_DEFAULTS and best_local(servers, ram):
        hybrid = bool(questionary.confirm(
            "Use a small local model for quick jobs (summaries) and as a backup if the cloud is down?",
            default=True).ask())
    plan = plan_models(provider, servers, ram, hybrid)

    if provider not in CLOUD_DEFAULTS and not plan.main:
        # Nothing usable on this computer. Offer a download sized for the RAM.
        ollama_up = any(s.name == "ollama" for s in servers)
        if not ollama_up and not shutil.which("ollama"):
            console.print(f"[yellow]Ollama is not installed.[/yellow] Download it from {OLLAMA_DOWNLOAD}, "
                          "then run paw setup again.")
        elif not ollama_up:
            console.print("[yellow]Ollama is installed but not running.[/yellow] Start it, then run paw setup again.")
        else:
            tag = suggest_pull(ram)
            host = config.provider_config.ollama.host
            if questionary.confirm(f"No suitable model is pulled yet. Download {tag} onto {host}?", default=True).ask():
                if _pull(host, tag):
                    plan = ModelPlan(main=f"ollama:{tag}")
        if not plan.main:
            return
    if plan.main:
        console.print(f"[green]Main model: {plan.main}[/green]"
                      + (f"  Quick jobs: {plan.fast}" if plan.fast else "")
                      + (f"  Backup: {', '.join(plan.fallbacks)}" if plan.fallbacks else ""))
        if questionary.confirm("Use these?", default=True).ask():
            apply_plan(config, plan)


LAB_NAMES = {"claude": "Anthropic", "openai": "OpenAI", "gemini": "Google"}


def _privacy_choices(questionary) -> list:
    return [
        questionary.Choice("Air-gapped: no internet at all. Models, memory and search stay on this computer "
                           "(and your network, if you allow it).", value="airgapped"),
        questionary.Choice("Local: models, memory and speech stay on this computer or your network. Web search "
                           "only through your own server.", value="local"),
        questionary.Choice("Normal: the cloud services you set up are used. Each reply says which one answered.",
                           value="normal"),
    ]


def _ask_address(questionary, prompt: str, default: str = "", kind: str = "openai") -> str:
    """Ask for a server address until it is one (three tries, then none). Returns the text as typed, "" for blank."""
    for _ in range(3):
        raw = (questionary.text(prompt, default=default).ask() or "").strip()
        if not raw:
            return ""
        try:
            server_url(raw, kind)
            return raw
        except ValueError as e:
            console.print(f"[yellow]{e}[/yellow]")
    return ""


def _choose_privacy(questionary, config: Config) -> str:
    """The first question. Local is preselected when a model server already runs on this computer."""
    console.print("[dim]Looking for model servers on this computer...[/dim]")
    found = [s for s in asyncio.run(scan_local()) if s.models]
    default = "local" if found else "normal"
    mode = questionary.select("How private should MicroPaw be?", choices=_privacy_choices(questionary),
                              default=default).ask() or default
    config.privacy.mode = mode
    if mode == "normal":
        return mode
    config.privacy.allow_lan = bool(questionary.confirm(
        "Use model servers on other computers on your network or Tailscale?", default=True).ask())
    hosts = questionary.text("Names or addresses on your network that count as local (comma-separated, optional):",
                             default=",".join(config.privacy.lan_hosts)).ask() or ""
    config.privacy.lan_hosts = [h.strip() for h in hosts.split(",") if h.strip()]
    kiwix = _ask_address(questionary, "Kiwix server for offline Wikipedia and docs (address, optional):",
                         config.search.kiwix_url)
    config.search.kiwix_url = server_url(kiwix) if kiwix else ""
    if kiwix:
        books = questionary.text("Kiwix book names to search (comma-separated, up to 5):",
                                 default=",".join(config.search.kiwix_books)).ask() or ""
        config.search.kiwix_books = [b.strip() for b in books.split(",") if b.strip()][:5]
    own = ""
    if mode == "local":
        own = _ask_address(questionary, "Your own SearXNG server for online search (address, optional):",
                           config.search.searxng_url)
        config.search.searxng_url = server_url(own) if own else ""
        if own:
            config.search.proxy = (questionary.text("SOCKS proxy for it, such as Tor (optional, "
                                                    "socks5h://127.0.0.1:9050):", default=config.search.proxy).ask()
                                   or "").strip()
    config.search.provider = "searxng" if own else "offline"
    return mode


def _ask_search(questionary, config: Config) -> None:
    """Normal mode: name who sees the queries, and preselect off."""
    choice = questionary.select(
        "Web search?",
        choices=[
            questionary.Choice("Off", value="off"),
            questionary.Choice("Public search services (public SearXNG instances and DuckDuckGo see your queries)",
                               value="auto"),
            questionary.Choice("My own SearXNG server", value="searxng"),
        ],
        default="off",
    ).ask() or "off"
    config.search.provider = choice
    if choice == "searxng":
        url = _ask_address(questionary, "Your SearXNG address:", config.search.searxng_url)
        if url:
            config.search.searxng_url = server_url(url)
        else:
            config.search.provider = "off"


def _ask_browser(questionary, config: Config, mode: str) -> bool:
    """Whether to install the browser. Air-gapped has none. Local mode only has it once privacy.allow names it."""
    if mode == "airgapped":
        console.print("[dim]The web browser is off in air-gapped mode, so it is not offered.[/dim]")
        return False
    if mode == "local" and "web_browse" not in config.privacy.allow:
        if not questionary.confirm("Allow the web browser? It reaches the internet, and every page address is shown "
                                   "to you for approval first.", default=False).ask():
            return False
        config.privacy.allow = [*config.privacy.allow, "web_browse"]
    return bool(questionary.confirm("Install web browsing support? (Playwright + Chromium, ~150MB)",
                                    default=False).ask())


def _ask_retention(questionary, config: Config) -> None:
    """How long messages are kept. Only when this version has the storage setting."""
    storage = getattr(config, "storage", None)
    if storage is None or not hasattr(storage, "retention_days"):
        return
    days = questionary.select(
        "How long should MicroPaw keep old messages?",
        choices=[questionary.Choice("30 days", value=30), questionary.Choice("90 days", value=90),
                 questionary.Choice("Keep them until I delete them", value=0)],
        default=90,
    ).ask()
    if days is not None:
        storage.retention_days = days


def _ask_cloud_fallback(questionary, config: Config, provider: str, fallbacks: list[str]) -> list[str]:
    """A local main model only falls back to the cloud when the owner says so, in plain words."""
    cloud = [f for f in fallbacks if f in CLOUD_DEFAULTS]
    if provider != "ollama" or not cloud:
        return fallbacks
    if questionary.confirm(f"If the local model fails, your messages would go to {', '.join(cloud)}. Allow that?",
                           default=False).ask():
        config.privacy.allow_cloud_fallback = True
        return fallbacks
    return [f for f in fallbacks if f not in cloud]


def _ask_where(questionary, provider: str) -> str:
    """"Where do your models run?" The cloud choices only exist for a cloud provider."""
    cloud = provider in CLOUD_DEFAULTS
    choices = [questionary.Choice("A cloud API", value="cloud"),
               questionary.Choice("A mix: the cloud, plus a model on this computer", value="mix"),
               questionary.Choice("Another computer on my network or tailnet", value="network")] if cloud else [
               questionary.Choice("This computer", value="this computer"),
               questionary.Choice("Another computer on my network or tailnet", value="network")]
    return questionary.select("Where do your models run?", choices=choices, default=choices[0].value).ask() or "auto"


def _safe_name(host: str) -> str:
    import re
    return ("lan-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-"))[:32].rstrip("-")


def _add_remote(questionary, config: Config, provider: str) -> None:
    """A model server on another computer: type the address, test it, save it, say how it is used."""
    from urllib.parse import urlparse

    from micropaw import modelscan
    from micropaw.providers.registry import pick_local

    raw = _ask_address(questionary, "Address of the other computer (for example 192.168.1.50 or gpu-box.local):")
    if not raw:
        return
    url = server_url(raw)
    if refusal := modelscan.refusal_for(config, _safe_name(modelscan._hostport(url)[0]), url, ""):  # before any probe
        console.print(f"[yellow]{refusal}[/yellow]")
        return
    server = None
    if urlparse(url).port or "://" in raw:
        kind, server = asyncio.run(modelscan.detect(url, "auto"))
    else:
        answers = asyncio.run(modelscan.probe_host(modelscan._hostport(url)[0]))
        server = answers[0] if answers else None
        if server:
            url, kind = server.url, modelscan.kind_for(server.url)
    if server is not None and server.problem == "key":
        console.print("[yellow]This server needs a key.[/yellow] Put it in ~/.micropaw/.env as NAME=... and run: "
                      f"paw models add NAME {url} --key-env NAME")
        return
    if server is None or not server.models:
        console.print(f"[yellow]Nothing answered at {url}.[/yellow] Is that computer on, and is the server set to "
                      "listen on the network? See 'Models on another computer' in the README.")
        return
    name = _safe_name(modelscan._hostport(url)[0])
    fields = {"base_url": modelscan.stored_url(url, kind)}
    if modelscan._entries_know_ollama():  # named endpoints that speak Ollama's own API
        fields["kind"] = kind
    entry = CustomProviderConfig(**fields)
    config.providers[name] = entry
    pick = pick_local(server.models, None)  # sized for that computer, not this one
    model = (pick.main or server.models[0]).name
    spec = f"{name}:{model}"
    console.print(f"[green]Found {len(server.models)} model(s) at {url}.[/green] Suggested: {spec}")
    if not modelscan.is_loopback(url):
        console.print(modelscan.NO_KEY_WARNING)
    if provider in CLOUD_DEFAULTS:
        role = questionary.select(
            "Use it for", choices=[questionary.Choice("Quick jobs (summaries)", value="fast"),
                                   questionary.Choice("A backup if the cloud is down", value="fallback")]).ask()
        if role == "fast":
            config.models.fast = spec
        elif role == "fallback":
            config.fallback_providers = [*config.fallback_providers, spec]
    elif questionary.confirm(f"Use {spec} as the main model?", default=True).ask():
        apply_plan(config, ModelPlan(main=spec))


def _offer_lan_scan(questionary, config: Config, provider: str) -> None:
    """Optional, off by default: look for servers on your own network, after saying what is sent."""
    from micropaw import modelscan
    try:
        net, addresses = modelscan.lan_range()
    except ValueError as e:
        console.print(f"[dim]{e}[/dim]")
        return
    if not questionary.confirm(
            f"Look for model servers on your network? This sends one short connection attempt to {len(addresses)} "
            f"addresses on {net}, ports {modelscan.PORT_LIST}. Only scan a network you own or may scan.",
            default=False).ask():
        return
    hits = asyncio.run(modelscan.scan_lan(addresses))
    if unfinished := getattr(hits, "unfinished", 0):
        console.print(f"[yellow]The scan stopped early: {hits.attempts - unfinished} of {hits.attempts} connection "
                      "attempts were checked. Run paw models scan --range on a smaller range.[/yellow]")
    for h in hits:
        console.print(f"Found {h.label} at {h.url}: {len(h.server.models)} model(s). "
                      f"Add it with: paw models add {modelscan.suggested_name(h.url)} {h.url}")
    if not hits:
        console.print(f"No model server found on {net}.")


def run_setup(reset: bool = False) -> None:
    """Run the interactive setup wizard using questionary. `reset` starts from defaults, keeping a .bak copy."""
    try:
        import questionary
    except ImportError:
        console.print("[red]questionary not installed. Install with: pip install questionary[/red]")
        return

    console.print(Panel(
        "[bold cyan]MicroPaw Setup Wizard[/bold cyan]\n"
        "Let's configure your AI assistant.",
        border_style="cyan",
    ))

    # Load existing config to preserve API keys, channel tokens, etc.
    if reset and config_exists():
        from micropaw.config import CONFIG_FILE
        shutil.copy2(CONFIG_FILE, CONFIG_FILE.with_name(CONFIG_FILE.name + ".bak"))
        console.print(f"[dim]The old settings are kept in {CONFIG_FILE.name}.bak[/dim]")
    config = load_config() if config_exists() and not reset else Config()

    privacy_mode = _choose_privacy(questionary, config)

    # Provider selection. Local and air-gapped modes only use models on this computer or your network.
    if privacy_mode == "normal":
        provider = questionary.select(
            "Which AI provider do you want to use?",
            choices=[
                questionary.Choice("Claude (Anthropic)", value="claude"),
                questionary.Choice("OpenAI (GPT-4o)", value="openai"),
                questionary.Choice("Gemini (Google)", value="gemini"),
                questionary.Choice("Ollama (Local)", value="ollama"),
            ],
        ).ask()
        if provider is None:
            return
        lab = LAB_NAMES.get(provider)
        console.print(f"[dim]Your messages go to {lab}. Search is off unless you turn it on below; nothing else "
                      "leaves this machine.[/dim]" if lab else "[dim]Your messages stay on this machine.[/dim]")
    else:
        provider = "ollama"
        console.print("[dim]Local and air-gapped modes only use models on this computer or your network.[/dim]")
    config.provider = provider

    # API key
    if provider in ("claude", "openai", "gemini"):
        key_prompts = {
            "claude": "Enter your Anthropic API key:",
            "openai": "Enter your OpenAI API key:",
            "gemini": "Enter your Google Gemini API key:",
        }
        api_key = questionary.password(key_prompts[provider]).ask()
        if api_key:
            getattr(config.provider_config, provider).api_key = api_key
    elif provider == "ollama":
        host = _ask_address(questionary, "Ollama host:", config.provider_config.ollama.host, "ollama")
        if host:
            config.provider_config.ollama.host = server_url(host, "ollama")

    where = _ask_where(questionary, provider)
    _choose_models(questionary, config, provider, where)
    if where == "network" or privacy_mode != "normal":
        _offer_lan_scan(questionary, config, provider)

    if privacy_mode == "normal":
        _ask_search(questionary, config)
    _ask_retention(questionary, config)

    # Persona
    name = questionary.text(
        "What should your assistant be called?", default="MicroPaw"
    ).ask()
    if name:
        config.persona.name = name

    location = questionary.text(
        "Your location? (optional, helps with 'near me' queries, e.g. 'NYC' or '94102'. "
        "It is sent to the model with every message.)",
        default="",
    ).ask()
    if location and location.strip():
        config.persona.location = location.strip()

    # Permissions (show existing values as defaults)
    allow_shell = questionary.confirm(
        "Allow shell command execution?", default=config.permissions.allow_shell
    ).ask()
    if allow_shell is not None:
        config.permissions.allow_shell = allow_shell

    allow_write = questionary.confirm(
        "Allow file writing?", default=config.permissions.allow_file_write
    ).ask()
    if allow_write is not None:
        config.permissions.allow_file_write = allow_write

    # Browser (optional — Playwright for web browsing/screenshots)
    install_browser = _ask_browser(questionary, config, privacy_mode)
    if install_browser:
        console.print("[dim]Installing Playwright...[/dim]")
        import subprocess
        import sys as _sys

        from micropaw.cli import add_extras
        try:
            if not add_extras(["browser"]):
                raise FileNotFoundError("could not add the browser extra")
            subprocess.run(
                [_sys.executable, "-m", "playwright", "install", "chromium"],
                check=True, capture_output=True,
            )
            console.print("[green]Playwright + Chromium installed. Web browsing is ready.[/green]")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            console.print(f"[yellow]Browser install failed: {e}[/yellow]")
            console.print("[dim]You can install later: paw extras add browser && playwright install chromium[/dim]")

    # Embeddings (for long-term memory with vector search)
    enable_embeddings = questionary.confirm(
        "Enable long-term memory with vector search?", default=False
    ).ask()
    if enable_embeddings:
        config.embedding.enabled = True
        if provider == "ollama":
            config.embedding.provider = "ollama"
            config.embedding.model = "nomic-embed-text"
            console.print(
                "[dim]Using Ollama embeddings (nomic-embed-text)."
                " Pull it with: ollama pull nomic-embed-text[/dim]"
            )
        else:
            # Check if fastembed is available
            _has_fe = False
            try:
                import fastembed  # noqa: F401
                _has_fe = True
            except ImportError:
                pass

            local_label = "Local (fastembed/ONNX, no API key)"
            if not _has_fe:
                local_label += " [NOT INSTALLED]"

            emb_choice = questionary.select(
                "Embedding provider:",
                choices=[
                    questionary.Choice(local_label, value="local"),
                    *([questionary.Choice("OpenAI (text-embedding-3-small)", value="openai")]
                      if privacy_mode == "normal" else []),
                    questionary.Choice("Ollama (nomic-embed-text)", value="ollama"),
                ],
            ).ask()
            if emb_choice:
                config.embedding.provider = emb_choice
                if emb_choice == "local":
                    config.embedding.model = "nomic-ai/nomic-embed-text-v1.5"
                    if not _has_fe:
                        console.print("[yellow]Warning: fastembed not installed. "
                                      "Install with: paw extras add embeddings[/yellow]")
                        console.print("[dim]Vector search will be disabled until installed.[/dim]")
                elif emb_choice == "openai":
                    config.embedding.model = "text-embedding-3-small"
                elif emb_choice == "ollama":
                    config.embedding.model = "nomic-embed-text"

    # Fallback providers
    use_fallback = not config.fallback_providers and questionary.confirm(
        "Set up fallback providers?", default=False
    ).ask()
    if use_fallback:
        available = [p for p in ["claude", "openai", "gemini", "ollama"] if p != provider]
        fallbacks = questionary.checkbox(
            "Select fallback providers (in order of preference):",
            choices=available,
        ).ask()
        if fallbacks:
            fallbacks = _ask_cloud_fallback(questionary, config, provider, fallbacks)
        if fallbacks:
            config.fallback_providers = fallbacks
            # Prompt for API keys for cloud fallback providers
            for fb in fallbacks:
                if fb == "ollama":
                    host = _ask_address(questionary, "Ollama host for fallback:",
                                        config.provider_config.ollama.host, "ollama")
                    if host:
                        config.provider_config.ollama.host = server_url(host, "ollama")
                elif fb in ("claude", "openai", "gemini"):
                    existing_key = getattr(config.provider_config, fb).api_key
                    if existing_key:
                        console.print(f"[dim]{fb} API key already set.[/dim]")
                    else:
                        key_labels = {"claude": "Anthropic", "openai": "OpenAI", "gemini": "Google Gemini"}
                        fb_key = questionary.password(
                            f"Enter your {key_labels[fb]} API key for {fb} fallback:"
                        ).ask()
                        if fb_key:
                            getattr(config.provider_config, fb).api_key = fb_key

    # --- Google Services (Gmail + Calendar) ---
    setup_google = questionary.confirm(
        "Configure Google services (Gmail + Calendar)?", default=False
    ).ask()
    if setup_google:
        console.print(Panel(
            "[bold]Google OAuth2 Setup[/bold]\n\n"
            "1. Go to https://console.cloud.google.com/apis/credentials\n"
            "2. Create a project (or select existing)\n"
            "3. Enable the [bold]Gmail API[/bold] and [bold]Google Calendar API[/bold]\n"
            "4. Create OAuth2 credentials (Desktop app type)\n"
            "5. Add [cyan]http://localhost:8089[/cyan] as redirect URI\n"
            "6. Copy the Client ID and Client Secret\n\n"
            "[dim]On first use, say 'authorize' in chat to sign in via browser.[/dim]",
            border_style="blue",
            padding=(1, 2),
        ))
        open_url = questionary.confirm(
            "Open Google Cloud Console in your browser?", default=False
        ).ask()
        if open_url:
            import webbrowser
            webbrowser.open("https://console.cloud.google.com/apis/credentials")
        client_id = questionary.text("OAuth2 Client ID:").ask()
        client_secret = questionary.password("OAuth2 Client Secret:").ask()
        if client_id and client_secret:
            config.google.enabled = True
            config.google.client_id = client_id.strip()
            config.google.client_secret = client_secret.strip()
            services = questionary.checkbox(
                "Which services to enable?",
                choices=[
                    questionary.Choice("Gmail", value="gmail", checked=True),
                    questionary.Choice("Google Calendar", value="calendar", checked=True),
                ],
            ).ask()
            if services:
                config.google.gmail_enabled = "gmail" in services
                config.google.calendar_enabled = "calendar" in services
            console.print("[green]Google services configured. Say 'authorize' in chat on first use.[/green]")

    # Save
    save_config(config)
    console.print("\n[green]Config saved![/green]")

    # Offer channel connection
    connect_channel = questionary.confirm(
        "Connect a messaging channel (Telegram, Discord, etc.)?", default=False
    ).ask()
    if connect_channel:
        from micropaw.connector import connect_channel as do_connect
        while True:
            do_connect(config=config)
            another = questionary.confirm("Connect another channel?", default=False).ask()
            if not another:
                break

    console.print("\n[dim]Run 'paw chat' to start chatting, or 'paw start' for all channels![/dim]")
