"""MicroClaw CLI — Click commands for setup, chat, start, config, status, doctor."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from microclaw import __version__
from microclaw.config import (
    CONFIG_DIR,
    CONFIG_FILE,
    DB_FILE,
    Config,
    config_exists,
    display_config,
    load_config,
)
from microclaw.utils.logger import setup_logging

console = Console()


def _create_single_provider(provider_name: str, config: Config):
    """Create a single AI provider by name."""
    if provider_name == "claude":
        from microclaw.providers.claude import ClaudeProvider
        cfg = config.provider_config.claude
        if not cfg.api_key:
            return None
        return ClaudeProvider(cfg.api_key, cfg.model, cfg.max_tokens)
    elif provider_name == "openai":
        from microclaw.providers.openai import OpenAIProvider
        cfg = config.provider_config.openai
        if not cfg.api_key:
            return None
        return OpenAIProvider(cfg.api_key, cfg.model, cfg.max_tokens)
    elif provider_name == "ollama":
        from microclaw.providers.ollama import OllamaProvider
        cfg = config.provider_config.ollama
        return OllamaProvider(cfg.host, cfg.model, cfg.max_tokens)
    elif provider_name == "gemini":
        from microclaw.providers.gemini import GeminiProvider
        cfg = config.provider_config.gemini
        if not cfg.api_key:
            return None
        return GeminiProvider(cfg.api_key, cfg.model, cfg.max_tokens)
    return None


def _create_provider(config: Config):
    """Create the AI provider with optional failover chain."""
    primary = _create_single_provider(config.provider, config)
    if not primary:
        console.print(f"[red]Error: No API key for {config.provider}. Run 'microclaw setup'.[/red]")
        sys.exit(1)

    if config.fallback_providers:
        from microclaw.providers.failover import FailoverProvider
        providers = [primary]
        for name in config.fallback_providers:
            fallback = _create_single_provider(name, config)
            if fallback:
                providers.append(fallback)
        if len(providers) > 1:
            return FailoverProvider(providers)

    return primary


def _create_registry():
    """Create and populate the tool registry."""
    from microclaw.tools.base import ToolRegistry
    from microclaw.tools.file_read import FileReadTool
    from microclaw.tools.file_write import FileWriteTool
    from microclaw.tools.http_request import HttpRequestTool
    from microclaw.tools.memory_tool import MemoryTool
    from microclaw.tools.pdf_read import PdfReadTool
    from microclaw.tools.sessions import SessionsTool
    from microclaw.tools.shell import ShellTool
    from microclaw.tools.vision import VisionTool
    from microclaw.tools.web_browse import WebBrowseTool
    from microclaw.tools.web_search import WebSearchTool

    registry = ToolRegistry()
    for tool in [
        WebSearchTool(), WebBrowseTool(), FileReadTool(), FileWriteTool(),
        ShellTool(), HttpRequestTool(), MemoryTool(), PdfReadTool(),
        VisionTool(), SessionsTool(),
    ]:
        registry.register(tool)

    return registry


def _create_channels(config: Config) -> list:
    """Create enabled channel instances."""
    channels = []

    if config.channels.terminal.enabled:
        from microclaw.channels.terminal import TerminalChannel
        channels.append(TerminalChannel())

    if config.channels.telegram.enabled and config.channels.telegram.token:
        from microclaw.channels.telegram import TelegramChannel
        channels.append(TelegramChannel(
            config.channels.telegram.token,
            config.channels.telegram.allowed_users or None,
        ))

    if config.channels.discord.enabled and config.channels.discord.token:
        from microclaw.channels.discord import DiscordChannel
        channels.append(DiscordChannel(
            config.channels.discord.token,
            config.channels.discord.allowed_users or None,
        ))

    if config.channels.slack.enabled and config.channels.slack.bot_token:
        from microclaw.channels.slack import SlackChannel
        channels.append(SlackChannel(
            config.channels.slack.bot_token,
            config.channels.slack.app_token,
            config.channels.slack.signing_secret,
        ))

    if config.channels.whatsapp.enabled and config.channels.whatsapp.token:
        from microclaw.channels.whatsapp import WhatsAppChannel
        channels.append(WhatsAppChannel(
            token=config.channels.whatsapp.token,
            phone_id=config.channels.whatsapp.phone_id,
            verify_token=config.channels.whatsapp.verify_token,
            app_secret=config.channels.whatsapp.app_secret,
        ))

    if config.channels.webchat.enabled:
        from microclaw.channels.webchat import WebChatChannel
        channels.append(WebChatChannel(
            host=config.channels.webchat.host,
            port=config.channels.webchat.port,
            auth_token=config.channels.webchat.auth_token,
        ))

    return channels


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__, prog_name="MicroClaw")
def main():
    """MicroClaw — Your AI. Your machine. Your keys."""
    setup_logging()


@main.command()
def setup():
    """Run the interactive setup wizard."""
    from microclaw.setup_wizard import run_setup
    run_setup()


@main.command()
def chat():
    """Start a terminal-only chat session."""
    config = load_config()
    config.channels.terminal.enabled = True
    config.channels.telegram.enabled = False
    config.channels.discord.enabled = False
    config.channels.slack.enabled = False
    config.channels.whatsapp.enabled = False
    config.channels.webchat.enabled = False

    provider = _create_provider(config)
    registry = _create_registry()
    channels = _create_channels(config)

    from microclaw.gateway import Gateway
    gw = Gateway(config, provider, registry, channels)
    asyncio.run(gw.start())


@main.command()
def start():
    """Start the gateway with all enabled channels."""
    if not config_exists():
        console.print("[yellow]No config found. Running setup wizard first...[/yellow]\n")
        from microclaw.setup_wizard import run_setup
        run_setup()

    config = load_config()
    provider = _create_provider(config)
    registry = _create_registry()
    channels = _create_channels(config)

    if not channels:
        console.print("[red]No channels enabled. Run 'microclaw setup' to configure.[/red]")
        sys.exit(1)

    from microclaw.gateway import Gateway
    gw = Gateway(config, provider, registry, channels)
    asyncio.run(gw.start())


@main.command()
@click.argument("channel", required=False, type=click.Choice(["telegram", "discord", "slack", "whatsapp", "webchat"]))
def connect(channel):
    """Connect a messaging channel (Telegram, Discord, Slack, WhatsApp, WebChat)."""
    from microclaw.connector import connect_channel
    config = load_config()
    connect_channel(channel_name=channel, config=config)


@main.command(name="config")
def show_config():
    """Show current configuration (redacted)."""
    config = load_config()
    data = display_config(config)
    console.print_json(json.dumps(data, indent=2))


@main.command()
def status():
    """Show system status."""
    config = load_config()

    table = Table(title="MicroClaw Status")
    table.add_column("Component", style="cyan")
    table.add_column("Status", style="green")

    # Provider
    provider = config.provider
    has_key = provider == "ollama" or bool(getattr(config.provider_config, provider).api_key)
    table.add_row("AI Provider", f"{provider} {'✓' if has_key else '✗ (no key)'}")

    # Channels
    for ch_name in ("terminal", "telegram", "discord", "slack", "whatsapp", "webchat"):
        ch = getattr(config.channels, ch_name)
        if ch.enabled:
            has_token = True
            if hasattr(ch, "token"):
                has_token = bool(ch.token)
            elif hasattr(ch, "bot_token"):
                has_token = bool(ch.bot_token)
            table.add_row(f"  {ch_name}", "enabled ✓" if has_token else "enabled (no token)")
        else:
            table.add_row(f"  {ch_name}", "disabled")

    # Database
    db_exists = DB_FILE.exists()
    table.add_row("Database", f"{DB_FILE.stat().st_size / 1024:.0f} KB" if db_exists else "not created")
    table.add_row("Config", str(CONFIG_FILE))

    console.print(table)


@main.command()
def doctor():
    """Run diagnostics and check system health."""
    import platform as plat
    config = load_config()

    table = Table(title="MicroClaw Doctor")
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Detail", style="dim")

    ok = lambda v: "[green]pass[/green]" if v else "[red]FAIL[/red]"

    py_ver = plat.python_version()
    table.add_row("Python version", ok(tuple(int(x) for x in py_ver.split(".")[:2]) >= (3, 11)), py_ver)
    table.add_row("Config file", ok(config_exists()), str(CONFIG_FILE))
    table.add_row("Database", ok(DB_FILE.exists()), f"{DB_FILE.stat().st_size / 1024:.0f} KB" if DB_FILE.exists() else "not created")

    provider = config.provider
    has_key = provider == "ollama" or bool(getattr(config.provider_config, provider).api_key)
    table.add_row(f"Provider ({provider})", ok(has_key), "API key configured" if has_key else "no API key")

    # Optional dependencies
    for name, pkg in [("Playwright", "playwright"), ("pypdf", "pypdf"), ("discord.py", "discord"), ("telegram", "telegram")]:
        try:
            __import__(pkg)
            table.add_row(name, ok(True), "installed")
        except ImportError:
            table.add_row(name, "[yellow]warn[/yellow]", "not installed")

    # Disk space
    usage = shutil.disk_usage("/")
    free_mb = usage.free / (1024 * 1024)
    table.add_row("Disk space", ok(free_mb > 100), f"{free_mb:.0f} MB free")

    console.print(table)


@main.command()
@click.confirmation_option(prompt="This will delete all config and data. Are you sure?")
def reset():
    """Reset all configuration and data."""
    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
        console.print("[green]All MicroClaw data has been reset.[/green]")
    else:
        console.print("[dim]Nothing to reset.[/dim]")


if __name__ == "__main__":
    main()
