"""Interactive setup wizard for MicroPaw."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field

from rich.console import Console
from rich.panel import Panel

from micropaw.config import AgentConfig, Config, config_exists, load_config, save_config
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
    config.models.fast = plan.fast
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


def _choose_models(questionary, config: Config, provider: str) -> None:
    """The model step: look for local servers, pick RAM-sized defaults, offer hybrid, offer to pull."""
    from micropaw.providers.ollama import ram_bytes

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
            if questionary.confirm(f"No suitable model is pulled yet. Download {tag}?", default=True).ask():
                if _pull(config.provider_config.ollama.host, tag):
                    plan = ModelPlan(main=f"ollama:{tag}")
        if not plan.main:
            return
    if plan.main:
        console.print(f"[green]Main model: {plan.main}[/green]"
                      + (f"  Quick jobs: {plan.fast}" if plan.fast else "")
                      + (f"  Backup: {', '.join(plan.fallbacks)}" if plan.fallbacks else ""))
        if questionary.confirm("Use these?", default=True).ask():
            apply_plan(config, plan)


def run_setup() -> None:
    """Run the interactive setup wizard using questionary."""
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
    config = load_config() if config_exists() else Config()

    # Provider selection
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
        host = questionary.text(
            "Ollama host:", default=config.provider_config.ollama.host
        ).ask()
        if host:
            config.provider_config.ollama.host = host

    _choose_models(questionary, config, provider)

    # Persona
    name = questionary.text(
        "What should your assistant be called?", default="MicroPaw"
    ).ask()
    if name:
        config.persona.name = name

    location = questionary.text(
        "Your location? (optional, helps with 'near me' queries, e.g. 'NYC' or '94102')",
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
    install_browser = questionary.confirm(
        "Install web browsing support? (Playwright + Chromium, ~150MB)",
        default=False,
    ).ask()
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
                    questionary.Choice("OpenAI (text-embedding-3-small)", value="openai"),
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
            config.fallback_providers = fallbacks
            # Prompt for API keys for cloud fallback providers
            for fb in fallbacks:
                if fb == "ollama":
                    host = questionary.text(
                        "Ollama host for fallback:", default="http://localhost:11434"
                    ).ask()
                    if host:
                        config.provider_config.ollama.host = host
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
