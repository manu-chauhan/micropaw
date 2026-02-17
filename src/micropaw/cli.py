"""MicroPaw CLI — Click commands for setup, chat, start, config, status, doctor."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from micropaw import __version__
from micropaw.config import (
    CONFIG_DIR,
    CONFIG_FILE,
    DB_FILE,
    Config,
    config_exists,
    display_config,
    load_config,
)
from micropaw.utils.logger import quiet_console, setup_logging

console = Console()


def _create_single_provider(provider_name: str, config: Config):
    """Create a single AI provider by name."""
    if provider_name == "claude":
        from micropaw.providers.claude import ClaudeProvider
        cfg = config.provider_config.claude
        if not cfg.api_key:
            return None
        return ClaudeProvider(cfg.api_key, cfg.model, cfg.max_tokens, cfg.base_url)
    elif provider_name == "openai":
        from micropaw.providers.openai import OpenAIProvider
        cfg = config.provider_config.openai
        if not cfg.api_key:
            return None
        return OpenAIProvider(cfg.api_key, cfg.model, cfg.max_tokens, cfg.base_url)
    elif provider_name == "ollama":
        from micropaw.providers.ollama import OllamaProvider
        cfg = config.provider_config.ollama
        return OllamaProvider(cfg.host, cfg.model, cfg.max_tokens)
    elif provider_name == "gemini":
        from micropaw.providers.gemini import GeminiProvider
        cfg = config.provider_config.gemini
        if not cfg.api_key:
            return None
        return GeminiProvider(cfg.api_key, cfg.model, cfg.max_tokens, cfg.base_url)
    return None


def _create_provider(config: Config):
    """Create the AI provider with optional failover chain."""
    primary = _create_single_provider(config.provider, config)
    if not primary:
        console.print(f"[red]Error: No API key for {config.provider}. Run 'micropaw setup'.[/red]")
        sys.exit(1)

    if config.fallback_providers:
        from micropaw.providers.failover import FailoverProvider
        providers = [primary]
        for name in config.fallback_providers:
            fallback = _create_single_provider(name, config)
            if fallback:
                providers.append(fallback)
        if len(providers) > 1:
            return FailoverProvider(providers)

    return primary


def _create_registry(config: Config):
    """Create and populate the tool registry, respecting disabled_tools config."""
    import logging
    from micropaw.tools.base import ToolRegistry
    from micropaw.tools.cron_tool import CronTool
    from micropaw.tools.file_read import FileReadTool
    from micropaw.tools.file_write import FileWriteTool
    from micropaw.tools.http_request import HttpRequestTool
    from micropaw.tools.memory_tool import MemoryTool
    from micropaw.tools.pdf_read import PdfReadTool
    from micropaw.tools.sessions import SessionsTool
    from micropaw.tools.shell import ShellTool
    from micropaw.tools.delegate import DelegateTool
    from micropaw.tools.vision import VisionTool
    from micropaw.tools.web_browse import WebBrowseTool
    from micropaw.tools.web_search import WebSearchTool

    disabled = set(config.permissions.disabled_tools)
    if disabled:
        logging.getLogger(__name__).info("Disabled tools: %s", ", ".join(sorted(disabled)))

    registry = ToolRegistry()
    for tool in [
        WebSearchTool(), WebBrowseTool(), FileReadTool(), FileWriteTool(),
        ShellTool(), HttpRequestTool(), MemoryTool(), PdfReadTool(),
        VisionTool(), SessionsTool(), DelegateTool(), CronTool(),
    ]:
        if tool.name not in disabled:
            registry.register(tool)

    return registry


def _create_channels(config: Config) -> list:
    """Create enabled channel instances."""
    channels = []

    if config.channels.terminal.enabled:
        from micropaw.channels.terminal import TerminalChannel
        channels.append(TerminalChannel())

    if config.channels.telegram.enabled and config.channels.telegram.token:
        from micropaw.channels.telegram import TelegramChannel
        channels.append(TelegramChannel(
            config.channels.telegram.token,
            config.channels.telegram.allowed_users or None,
        ))

    if config.channels.discord.enabled and config.channels.discord.token:
        from micropaw.channels.discord import DiscordChannel
        channels.append(DiscordChannel(
            config.channels.discord.token,
            config.channels.discord.allowed_users or None,
        ))

    if config.channels.slack.enabled and config.channels.slack.bot_token:
        from micropaw.channels.slack import SlackChannel
        channels.append(SlackChannel(
            config.channels.slack.bot_token,
            config.channels.slack.app_token,
            config.channels.slack.signing_secret,
        ))

    if config.channels.whatsapp.enabled and config.channels.whatsapp.token:
        from micropaw.channels.whatsapp import WhatsAppChannel
        channels.append(WhatsAppChannel(
            token=config.channels.whatsapp.token,
            phone_id=config.channels.whatsapp.phone_id,
            verify_token=config.channels.whatsapp.verify_token,
            app_secret=config.channels.whatsapp.app_secret,
        ))

    if config.channels.webchat.enabled:
        from micropaw.channels.webchat import WebChatChannel
        channels.append(WebChatChannel(
            host=config.channels.webchat.host,
            port=config.channels.webchat.port,
            auth_token=config.channels.webchat.auth_token,
            allowed_origins=config.channels.webchat.allowed_origins,
        ))

    return channels


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__, prog_name="MicroPaw")
def main():
    """MicroPaw — Your AI. Your machine. Your keys."""
    setup_logging()


@main.command()
def setup():
    """Run the interactive setup wizard."""
    from micropaw.setup_wizard import run_setup
    run_setup()


@main.command()
def chat():
    """Start a terminal-only chat session."""
    quiet_console()  # suppress INFO logs — details go to ~/.micropaw/logs/
    config = load_config()
    config.channels.terminal.enabled = True
    config.channels.telegram.enabled = False
    config.channels.discord.enabled = False
    config.channels.slack.enabled = False
    config.channels.whatsapp.enabled = False
    config.channels.webchat.enabled = False

    provider = _create_provider(config)
    registry = _create_registry(config)
    channels = _create_channels(config)

    from micropaw.gateway import Gateway
    gw = Gateway(config, provider, registry, channels)
    asyncio.run(gw.start())


@main.command()
def start():
    """Start the gateway with all enabled channels."""
    quiet_console()  # suppress INFO logs — details go to ~/.micropaw/logs/
    if not config_exists():
        console.print("[yellow]No config found. Running setup wizard first...[/yellow]\n")
        from micropaw.setup_wizard import run_setup
        run_setup()

    config = load_config()
    provider = _create_provider(config)
    registry = _create_registry(config)
    channels = _create_channels(config)

    if not channels:
        console.print("[red]No channels enabled. Run 'micropaw setup' to configure.[/red]")
        sys.exit(1)

    from micropaw.gateway import Gateway
    gw = Gateway(config, provider, registry, channels)
    asyncio.run(gw.start())


@main.command()
@click.argument("channel", required=False, type=click.Choice(["telegram", "discord", "slack", "whatsapp", "webchat"]))
def connect(channel):
    """Connect a messaging channel (Telegram, Discord, Slack, WhatsApp, WebChat)."""
    from micropaw.connector import connect_channel
    config = load_config()
    connect_channel(channel_name=channel, config=config)


@main.command()
def test():
    """Test connectivity to all configured channels and AI provider."""
    from micropaw.connector import test_channels
    config = load_config()
    test_channels(config)


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

    table = Table(title="MicroPaw Status")
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

    table = Table(title="MicroPaw Doctor")
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
@click.option("--conversation-id", "-c", default=None, help="Conversation ID to export.")
@click.option("--format", "-f", "fmt", type=click.Choice(["json", "markdown"]), default="json", help="Export format.")
@click.option("--output", "-o", "output_file", default=None, help="Output file (default: stdout).")
def export(conversation_id, fmt, output_file):
    """Export conversation history."""
    async def _export():
        from micropaw.memory.store import MemoryStore
        store = MemoryStore(DB_FILE)
        await store.init()
        try:
            if not conversation_id:
                convos = await store.list_conversations()
                if not convos:
                    console.print("[dim]No conversations found.[/dim]")
                    return
                table = Table(title="Conversations")
                table.add_column("ID", style="cyan")
                table.add_column("Platform")
                table.add_column("User")
                table.add_column("Updated")
                for c in convos:
                    table.add_row(c["id"][:12] + "…", c["platform"], c["user_id"], c["updated_at"])
                console.print(table)
                console.print("\n[dim]Use --conversation-id to export a specific conversation.[/dim]")
                return

            messages = await store.export_messages(conversation_id)
            if not messages:
                console.print(f"[red]No messages found for conversation {conversation_id}.[/red]")
                return

            if fmt == "json":
                text = json.dumps(messages, indent=2)
            else:
                lines = []
                for m in messages:
                    role = m["role"].upper()
                    lines.append(f"### {role} ({m['created_at']})\n\n{m['content']}\n")
                text = "\n---\n\n".join(lines)

            if output_file:
                from pathlib import Path
                Path(output_file).write_text(text)
                console.print(f"[green]Exported {len(messages)} messages to {output_file}[/green]")
            else:
                click.echo(text)
        finally:
            await store.close()

    asyncio.run(_export())


@main.command()
@click.confirmation_option(prompt="This will delete all config and data. Are you sure?")
def reset():
    """Reset all configuration and data."""
    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
        console.print("[green]All MicroPaw data has been reset.[/green]")
    else:
        console.print("[dim]Nothing to reset.[/dim]")


# ---------------------------------------------------------------------------
# Cron subcommands
# ---------------------------------------------------------------------------

def _parse_interval(s: str) -> int:
    """Parse '6h', '45m', '30s', '1d' → seconds."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    s = s.strip()
    if s and s[-1] in units:
        try:
            return int(s[:-1]) * units[s[-1]]
        except ValueError:
            pass
    try:
        return int(s)
    except ValueError:
        raise click.BadParameter(f"Invalid interval: {s}. Use e.g. 6h, 45m, 30s, 1d")


@main.group()
def cron():
    """Manage scheduled tasks."""


@cron.command()
@click.argument("description")
@click.option("--every", "interval", default=None, help="Recurring interval (e.g. 6h, 45m, 30s, 1d).")
@click.option("--once", "once_delay", default=None, help="One-shot delay (e.g. 2h, 30m). Fires once then deletes.")
def add(description, interval, once_delay):
    """Add a scheduled task."""
    if not interval and not once_delay:
        console.print("[red]Provide --every (recurring) or --once (one-shot).[/red]")
        return
    if interval and once_delay:
        console.print("[red]Use either --every or --once, not both.[/red]")
        return

    one_shot = once_delay is not None
    seconds = _parse_interval(once_delay if one_shot else interval)  # type: ignore[arg-type]

    async def _add():
        from micropaw.scheduler import Scheduler
        sched = Scheduler(str(DB_FILE), handler=None, bus=None, channels=[])
        await sched.init()
        try:
            job_id = await sched.add_job(description, seconds, one_shot=one_shot)
            kind = "one-shot" if one_shot else "recurring"
            console.print(f"[green]Created {kind} job #{job_id}: \"{description}\"[/green]")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
        finally:
            await sched.close()

    asyncio.run(_add())


@cron.command(name="list")
def list_jobs():
    """List all scheduled tasks."""
    from datetime import datetime, timezone

    async def _list():
        from micropaw.scheduler import Scheduler
        sched = Scheduler(str(DB_FILE), handler=None, bus=None, channels=[])
        await sched.init()
        try:
            jobs = await sched.list_jobs()
            if not jobs:
                console.print("[dim]No scheduled tasks.[/dim]")
                return
            table = Table(title="Scheduled Tasks")
            table.add_column("ID", style="cyan")
            table.add_column("Description")
            table.add_column("Schedule")
            table.add_column("Next Run")
            for j in jobs:
                kind = "once" if j["one_shot"] else f"every {_fmt(j['interval_seconds'])}"
                next_dt = datetime.fromtimestamp(j["next_run_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                table.add_row(str(j["id"]), j["description"], kind, next_dt)
            console.print(table)
        finally:
            await sched.close()

    asyncio.run(_list())


@cron.command()
@click.argument("job_id", type=int)
def remove(job_id):
    """Remove a scheduled task by ID."""
    async def _remove():
        from micropaw.scheduler import Scheduler
        sched = Scheduler(str(DB_FILE), handler=None, bus=None, channels=[])
        await sched.init()
        try:
            removed = await sched.remove_job(job_id)
            if removed:
                console.print(f"[green]Removed job #{job_id}.[/green]")
            else:
                console.print(f"[red]Job #{job_id} not found.[/red]")
        finally:
            await sched.close()

    asyncio.run(_remove())


def _fmt(seconds: int) -> str:
    """Format seconds as human-readable interval."""
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


if __name__ == "__main__":
    main()
