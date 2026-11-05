"""MicroPaw CLI — Click commands for setup, chat, start, config, status, doctor."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import UTC

import click
from rich.console import Console
from rich.table import Table

from micropaw import __version__
from micropaw.config import (
    CONFIG_DIR,
    CONFIG_FILE,
    DB_FILE,
    OPENAI_COMPATIBLE,
    Config,
    config_exists,
    display_config,
    load_config,
)
from micropaw.utils.logger import quiet_console, setup_logging

console = Console()


def _create_single_provider(provider_name: str, config: Config, model: str = ""):
    """Create a single AI provider by name, optionally with a different model."""
    if provider_name == "claude":
        from micropaw.providers.claude import ClaudeProvider
        cfg = config.provider_config.claude
        if not cfg.api_key:
            return None
        return ClaudeProvider(cfg.api_key, model or cfg.model, cfg.max_tokens, cfg.base_url, effort=cfg.effort)
    elif provider_name == "openai":
        from micropaw.providers.openai import OpenAIProvider
        cfg = config.provider_config.openai
        if not cfg.api_key:
            return None
        return OpenAIProvider(cfg.api_key, model or cfg.model, cfg.max_tokens, cfg.base_url)
    elif provider_name == "ollama":
        from micropaw.providers.ollama import OllamaProvider
        cfg = config.provider_config.ollama
        return OllamaProvider(cfg.host, model or cfg.model, cfg.max_tokens)
    elif provider_name == "gemini":
        from micropaw.providers.gemini import GeminiProvider
        cfg = config.provider_config.gemini
        if not cfg.api_key:
            return None
        return GeminiProvider(cfg.api_key, model or cfg.model, cfg.max_tokens, cfg.base_url)
    elif provider_name in OPENAI_COMPATIBLE and model:
        from micropaw.providers.openai import OpenAIProvider
        base_url, key_env = OPENAI_COMPATIBLE[provider_name]
        key = os.environ.get(key_env, "") if key_env else "local"
        if not key:
            return None
        return OpenAIProvider(key, model, config.provider_config.openai.max_tokens, base_url)
    return None


def _create_role_provider(spec: str, config: Config):
    """A model for one job, from "provider:model". None when unset or unusable (the main model is used)."""
    if not spec:
        return None
    name, _, model = spec.partition(":")
    provider = _create_single_provider(name.strip().lower(), config, model.strip())
    if provider is None:
        console.print(f"[yellow]Model '{spec}' isn't usable (unknown provider or missing key); "
                      "using the main model for that job.[/yellow]")
    return provider


def _create_provider(config: Config):
    """Create the AI provider with optional failover chain."""
    primary = _create_single_provider(config.provider, config)
    if not primary:
        console.print(f"[red]Error: No API key for {config.provider}. Run 'paw setup'.[/red]")
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
    from micropaw.tools.calendar_tool import CalendarTool
    from micropaw.tools.channel_send import ChannelSendTool
    from micropaw.tools.contacts_tool import ContactsTool
    from micropaw.tools.cron_tool import CronTool
    from micropaw.tools.delegate import DelegateTool
    from micropaw.tools.email_tool import EmailTool
    from micropaw.tools.file_read import FileReadTool
    from micropaw.tools.file_write import FileWriteTool
    from micropaw.tools.files_tool import FilesTool
    from micropaw.tools.http_request import HttpRequestTool
    from micropaw.tools.memory_tool import MemoryTool
    from micropaw.tools.pdf_read import PdfReadTool
    from micropaw.tools.sessions import SessionsTool
    from micropaw.tools.shell import ShellTool
    from micropaw.tools.tool_output import ToolOutputTool
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
        EmailTool(), CalendarTool(), ContactsTool(), FilesTool(),
        ChannelSendTool(), ToolOutputTool(),
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
        channels.append(TelegramChannel(config.channels.telegram.token))

    if config.channels.discord.enabled and config.channels.discord.token:
        from micropaw.channels.discord import DiscordChannel
        channels.append(DiscordChannel(config.channels.discord.token))

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
    gw = Gateway(config, provider, registry, channels, **_role_providers(config))
    asyncio.run(gw.start())


def _role_providers(config: Config) -> dict:
    return {
        "fast_provider": _create_role_provider(config.models.fast, config),
        "vision_provider": _create_role_provider(config.models.vision, config),
    }


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
        console.print("[red]No channels enabled. Run 'paw setup' to configure.[/red]")
        sys.exit(1)

    from micropaw.gateway import Gateway
    gw = Gateway(config, provider, registry, channels, **_role_providers(config))
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

    def ok(v):
        return "[green]pass[/green]" if v else "[red]FAIL[/red]"

    py_ver = plat.python_version()
    py_ok = tuple(int(x) for x in py_ver.split(".")[:2]) >= (3, 11)
    table.add_row("Python version", ok(py_ok), py_ver)
    table.add_row("Config file", ok(config_exists()), str(CONFIG_FILE))
    db_detail = (
        f"{DB_FILE.stat().st_size / 1024:.0f} KB" if DB_FILE.exists() else "not created"
    )
    table.add_row("Database", ok(DB_FILE.exists()), db_detail)

    provider = config.provider
    has_key = provider == "ollama" or bool(getattr(config.provider_config, provider).api_key)
    table.add_row(f"Provider ({provider})", ok(has_key), "API key configured" if has_key else "no API key")

    # Optional dependencies
    optional_deps = [
        ("Playwright", "playwright"), ("pypdf", "pypdf"),
        ("discord.py", "discord"), ("telegram", "telegram"),
    ]
    for name, pkg in optional_deps:
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
                    cid = c["id"]
                    display_id = (cid[:12] + "…") if len(cid) > 12 else cid
                    table.add_row(display_id, c["platform"], c["user_id"], c["updated_at"])
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


@main.command()
@click.argument("platform", type=click.Choice(["telegram", "discord", "slack", "whatsapp"]))
@click.argument("user_id")
@click.option("--remove", is_flag=True, help="Take this ID off the list instead.")
def allow(platform, user_id, remove):
    """Let one of your own accounts use the assistant. Everyone else is refused.

    Message the bot once from your account: it replies with your ID.
    """
    from micropaw.config import set_allowed_user
    ids = set_allowed_user(platform, user_id.strip(), remove=remove)
    console.print(f"[green]{platform} allowed IDs:[/green] {', '.join(ids) or 'none'}")


@main.command(name="log")
@click.option("-n", "count", default=30, help="How many recent entries to show.")
def show_log(count):
    """Show recent tool calls and approvals (the audit log)."""
    from micropaw.utils.audit import AUDIT_FILE, tail
    entries = tail(count)
    if not entries:
        console.print(f"[dim]No entries yet in {AUDIT_FILE}.[/dim]")
        return
    table = Table(title="Recent tool calls")
    for col in ("Time", "Tool", "Outcome", "Details"):
        table.add_column(col)
    for e in entries:
        details = e.get("approval") or ", ".join(f"{k}={v}" for k, v in e.get("args", {}).items())
        table.add_row(e["ts"][5:16].replace("T", " "), e["tool"], e["outcome"], details[:120])
    console.print(table)


@main.command()
@click.option("--days", default=7, show_default=True, help="How many days back to show.")
def usage(days):
    """Show model usage and spend by day, agent and model."""
    async def _usage():
        from micropaw.memory.ledger import Ledger
        from micropaw.memory.store import MemoryStore
        store = MemoryStore(DB_FILE)
        await store.init()
        try:
            ledger = Ledger(store.db)
            await ledger.init()
            rows = await ledger.breakdown(days)
        finally:
            await store.close()
        if not rows:
            console.print("[dim]No model calls recorded yet.[/dim]")
            return
        table = Table(title=f"Usage, last {days} day(s)")
        for col in ("Day", "Agent", "Model", "USD", "Tokens", "Calls"):
            table.add_column(col, justify="right" if col in ("USD", "Tokens", "Calls") else "left")
        for day, agent, model, t in rows:
            usd = f"{t.usd:.2f}" + ("+" if t.unpriced_tokens else "")
            table.add_row(day, agent, model, usd, f"{t.tokens:,}", str(t.calls))
        console.print(table)
        if any(t.unpriced_tokens for *_, t in rows):
            console.print("[dim]+ means some calls had no known price and are not in the USD figure.[/dim]")

    asyncio.run(_usage())


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
    from datetime import datetime

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
            table.add_column("State")
            for j in jobs:
                kind = "once" if j["one_shot"] else f"every {_fmt(j['interval_seconds'])}"
                next_dt = datetime.fromtimestamp(j["next_run_at"], tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
                state = "on" if j["enabled"] else f"paused (paw cron enable {j['id']})"
                table.add_row(str(j["id"]), j["description"], kind, next_dt, state)
            console.print(table)
        finally:
            await sched.close()

    asyncio.run(_list())


@cron.command()
@click.argument("job_id", type=int)
@click.option("--off", is_flag=True, help="Pause the task instead.")
def enable(job_id, off):
    """Turn on a task the assistant scheduled (they start paused)."""
    async def _enable():
        from micropaw.scheduler import Scheduler
        sched = Scheduler(str(DB_FILE), handler=None, bus=None, channels=[])
        await sched.init()
        try:
            job = await sched.set_enabled(job_id, not off)
            if job:
                state = "paused" if off else "enabled"
                console.print(f"[green]Job #{job_id} {state}: \"{job['description']}\"[/green]")
            else:
                console.print(f"[red]Job #{job_id} not found.[/red]")
        finally:
            await sched.close()

    asyncio.run(_enable())


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
    from micropaw.tools.cron_tool import _fmt_interval
    return _fmt_interval(seconds)


if __name__ == "__main__":
    main()
