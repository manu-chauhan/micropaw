"""MicroPaw CLI — Click commands for setup, chat, start, config, status, doctor."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import UTC
from urllib.parse import urlparse

import click

from micropaw import __version__
from micropaw.config import (
    CONFIG_DIR,
    CONFIG_FILE,
    DB_FILE,
    Config,
    ConfigError,
    config_exists,
    display_config,
    load_config,
)
from micropaw.core.profile import ProfileError, build_runtimes
from micropaw.providers.registry import Registry
from micropaw.utils.logger import quiet_console, setup_logging


class _LazyConsole:
    """rich loads on the first print. `paw start --headless` (a service, Docker) never prints, so it never pays."""

    def __getattr__(self, name):
        global console
        from rich.console import Console
        console = Console()
        return getattr(console, name)


console = _LazyConsole()


def Table(*args, **kwargs):  # noqa: N802 (stands in for rich.table.Table, imported when a table is built)
    from rich.table import Table as RichTable
    return RichTable(*args, **kwargs)


def _privacy_upgrade_notice() -> None:
    """Once, for a config saved before privacy.mode existed: say that it is there and what it does."""
    import micropaw.config as config_mod
    marker = config_mod.CONFIG_DIR / ".privacy_notice"
    try:
        if not config_mod.CONFIG_FILE.exists() or marker.exists():
            return
        if "privacy" not in json.loads(config_mod.CONFIG_FILE.read_text()):
            console.print("[dim]New: privacy.mode in the config (normal, local or airgapped) keeps models, memory "
                          "and search on this computer or your network. Today it is normal, as before. "
                          "See 'paw privacy' and the Privacy section of the README.[/dim]")
        marker.write_text("shown\n")
    except (OSError, ValueError):
        pass


def _create_runtimes(config: Config) -> tuple[Registry, dict]:
    """The model registry and one runtime (profile, providers) per agent. Exits with a plain message on failure."""
    registry = Registry(config)
    try:
        runtimes = build_runtimes(config, registry)
    except ProfileError as e:
        console.print(f"[red]Error: {e} Run 'paw setup' or 'paw models list'.[/red]")
        sys.exit(1)
    for problem in dict.fromkeys(registry.problems):
        console.print(f"[yellow]{problem}[/yellow]")
    return registry, runtimes


def _make_gateway(config: Config, registry, channels):
    models, runtimes = _create_runtimes(config)
    main = runtimes["main"]
    from micropaw.gateway import Gateway
    return Gateway(config, main.provider, registry, channels, main.fast, main.vision, runtimes=runtimes, models=models)


def _create_registry(config: Config):
    """Create and populate the tool registry, respecting disabled_tools config."""
    from micropaw.core.toolsets import build_registry
    return build_registry(config)


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

class PawGroup(click.Group):
    """A bad config value is one plain line, not a traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except ConfigError as e:
            raise click.ClickException(str(e)) from None


@click.group(cls=PawGroup)
@click.version_option(version=__version__, prog_name="MicroPaw")
def main():
    """MicroPaw — Your AI. Your machine. Your keys."""
    setup_logging()


@main.command()
@click.option("--reset", is_flag=True, help="Start from default settings. The old file is kept as config.json.bak.")
def setup(reset):
    """Run the interactive setup wizard."""
    from micropaw.setup_wizard import run_setup
    run_setup(reset=reset)


@main.command()
def chat():
    """Start a terminal-only chat session."""
    quiet_console()  # suppress INFO logs — details go to ~/.micropaw/logs/
    _privacy_upgrade_notice()
    config = load_config()
    config.channels.terminal.enabled = True
    config.channels.telegram.enabled = False
    config.channels.discord.enabled = False
    config.channels.slack.enabled = False
    config.channels.whatsapp.enabled = False
    config.channels.webchat.enabled = False

    registry = _create_registry(config)
    channels = _create_channels(config)
    asyncio.run(_make_gateway(config, registry, channels).start())


def _quoted(text: str) -> str:
    """The text in quotes for a /goal line (double quotes unless it holds one)."""
    return f"'{text}'" if '"' in text else f'"{text}"'


@main.command()
@click.argument("task", nargs=-1, required=True)
@click.option("--check", default="", help="A command that exits 0 when the goal is done.")
@click.option("--max", "max_rounds", type=int, default=0, help="Most rounds (default 20).")
@click.option("--budget", type=float, default=None, help="Spending limit in USD. Required for cloud models.")
@click.option("--agent", "agent_name", default="", help="Which agent works on it (default main).")
def goal(task, check, max_rounds, budget, agent_name):
    """Work on a goal in rounds in a terminal chat, until the check passes or a limit is hit."""
    from micropaw.core.goal import parse_args
    line = "/goal " + " ".join(task)
    for flag, value in (("--check", check and _quoted(check)), ("--max", max_rounds or ""),
                        ("--budget", "" if budget is None else budget), ("--agent", agent_name)):
        if value != "":
            line += f" {flag} {value}"
    if isinstance(error := parse_args(line.removeprefix("/goal ")), str):
        _fail(click.UsageError(error))
    quiet_console()
    config = load_config()
    config.channels.terminal.enabled = True
    for name in ("telegram", "discord", "slack", "whatsapp", "webchat"):
        getattr(config.channels, name).enabled = False
    gw = _make_gateway(config, _create_registry(config), _create_channels(config))
    gw.first_command = line
    _run_single(gw)


@main.command()
@click.option("--headless", is_flag=True, help="No terminal channel; restart after a crash. For services and Docker.")
def start(headless):
    """Start the gateway with all enabled channels."""
    quiet_console()  # suppress INFO logs — details go to ~/.micropaw/logs/
    if headless or not _interactive():
        sys.exit(_start_headless())
    if not config_exists():
        console.print("[yellow]No config found. Running setup wizard first...[/yellow]\n")
        from micropaw.setup_wizard import run_setup
        run_setup()

    _privacy_upgrade_notice()
    config = load_config()
    registry = _create_registry(config)
    channels = _create_channels(config)

    if not channels:
        console.print("[red]No channels enabled. Run 'paw setup' to configure.[/red]")
        sys.exit(1)

    _run_single(_make_gateway(config, registry, channels))


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

    from micropaw import privacy_report
    table.add_row("Privacy", privacy_report.status_line(config).removeprefix("Privacy: "))

    # Database
    db_exists = DB_FILE.exists()
    table.add_row("Database", f"{DB_FILE.stat().st_size / 1024:.0f} KB" if db_exists else "not created")
    table.add_row("Config", str(CONFIG_FILE))

    console.print(table)


@main.group(name="privacy", invoke_without_command=True)
@click.pass_context
def privacy_cmd(ctx):
    """Show where your data goes: the mode, every model, search, channel and server, and file permissions."""
    if ctx.invoked_subcommand is None:
        from micropaw import privacy_report
        click.echo(privacy_report.render(load_config()))


@privacy_cmd.command(name="share-history")
@click.option("--yes", is_flag=True, help="Do not ask first.")
def privacy_share_history(yes):
    """Let cloud models see what was saved in local or air-gapped mode."""
    from micropaw import storage
    click.echo("Rows saved in local or air-gapped mode become sendable to cloud models.")
    if not DB_FILE.exists():
        click.echo("There is no saved history yet.")
        return
    if not yes and not click.confirm("Share it?", default=False):
        click.echo("Nothing changed.")
        return
    counts = storage.share_local_history(DB_FILE)
    click.echo(f"Shared: {counts['messages']} messages, {counts['memories']} memories, "
               f"{counts['document_chunks']} document pieces, {counts['summaries']} summaries.")


@main.command()
@click.option("--quiet", is_flag=True, help="No output. Exit 0 if the gateway runs and the database opens, else 1.")
@click.option("--search", is_flag=True, help="Also run one test search (asks first, shows the query).")
def doctor(quiet, search):
    """Run diagnostics and check system health."""
    if quiet:
        from micropaw.service import healthy
        sys.exit(0 if healthy()[0] else 1)
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
    from micropaw.tools import sandbox
    sandbox.configure(load_config().permissions)
    sb_line = sandbox.describe()
    sb = sandbox.resolve()
    # Windows has no sandbox to find: every command asks, so that is a note, not a failure
    sb_mark = "[yellow]warn[/yellow]" if not sb.active and not sb.error and os.name == "nt" else ok(sb.active)
    table.add_row("Shell sandbox", sb_mark, sb_line)

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
    _doctor_install_rows(table, config)
    _doctor_server_rows(table, config)
    from micropaw import privacy_report
    for name, mark, detail in privacy_report.advice(config):
        table.add_row(name, {"pass": "[green]pass[/green]", "warn": "[yellow]warn[/yellow]"}.get(
            mark, "[dim]note[/dim]"), detail)

    console.print(table)
    if search:
        _doctor_search(config)


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
@click.option("--all", "everything", is_flag=True, help="Delete every message, memory, document, contact and log.")
@click.option("--older-than", "older", default="", metavar="AGE", help="Delete what is older than AGE (30d, 4w).")
@click.option("--yes", is_flag=True, help="Do not ask first.")
def forget(everything, older, yes):
    """Delete what MicroPaw remembers, and overwrite the space it used.

    --all stops if the gateway is running. Copies you made (exports) and backups are not touched.
    """
    from micropaw import service as svc
    from micropaw import storage
    if everything == bool(older):
        _fail(click.UsageError("Use --all, or --older-than 30d."))
    storage.harden_home(CONFIG_DIR)  # a database made by this command is owner-only too
    if everything:
        if svc.running_gateway():
            _fail(click.ClickException("MicroPaw is running. Stop it first: paw service stop"))
        if not yes:
            click.confirm("Delete every message, memory, document, contact and log?", abort=True)
        for problem in storage.forget_all(DB_FILE, CONFIG_DIR):
            console.print(f"[yellow]{problem}[/yellow]")
        console.print("Done. Your config and keys are still there. Files you exported are not touched.")
        return
    try:
        days = storage.parse_age(older)
    except ValueError as e:
        _fail(click.UsageError(str(e)))

    async def sweep():
        from micropaw.memory.store import MemoryStore
        store = MemoryStore(DB_FILE)
        await store.init()
        try:
            return await store.purge_older_than(days)
        finally:
            await store.close()

    counts = asyncio.run(sweep())
    counts["log lines and files"] = storage.purge_old_files(CONFIG_DIR, days)
    console.print("Removed " + (", ".join(f"{n} {k}" for k, n in counts.items() if n) or "nothing") + ".")


@main.command()
@click.confirmation_option(prompt="This will delete all config and data. Are you sure?")
def reset():
    """Reset all configuration and data. This is not a secure wipe: use `paw forget --all` first for that."""
    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
        console.print("[green]All MicroPaw data has been reset.[/green]")
    else:
        console.print("[dim]Nothing to reset.[/dim]")


# ---------------------------------------------------------------------------
# Background service, update, extras, headless start
# ---------------------------------------------------------------------------

def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()  # None under pythonw


def _start_headless() -> int:
    """Gateway without the terminal channel, restarted after a crash, one instance per machine."""
    from micropaw import service as svc

    def make():
        config = load_config()
        config.channels.terminal.enabled = False  # not built at all: it would load rich for nothing
        channels = _create_channels(config)
        if not channels:
            console.print("[red]No channels enabled. Run 'paw setup' to configure.[/red]")
            sys.exit(1)
        return _make_gateway(config, _create_registry(config), channels)

    try:
        return svc.run_headless(make)
    except svc.ServiceError as e:
        console.print(f"[red]{e}[/red]")
        return 1


def _run_single(gw) -> None:
    """Run a gateway in the foreground, refusing when another one already runs."""
    from micropaw import service as svc
    try:
        svc.acquire_pid()
    except svc.ServiceError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
    try:
        asyncio.run(gw.start())
    finally:
        svc.release_pid()


def _fail(e: Exception) -> None:
    console.print(f"[red]{e}[/red]")
    sys.exit(1)


@main.group()
def service():
    """Run MicroPaw in the background and start it at login."""


@service.command(name="install")
@click.option("--force", is_flag=True, help="Replace an existing service without asking.")
def service_install(force):
    """Install the service and start it."""
    from micropaw import service as svc
    try:
        found = svc.existing_definition()
        if found and not force:
            console.print("A service is already installed:\n", markup=False)
            click.echo(found)
            if not click.confirm("Replace it?"):
                return
        for line in svc.install():
            console.print(line, markup=False)
    except svc.ServiceError as e:
        _fail(e)


@service.command(name="uninstall")
def service_uninstall():
    """Stop and remove the service."""
    from micropaw import service as svc
    try:
        for line in svc.uninstall():
            console.print(line, markup=False)
    except svc.ServiceError as e:
        _fail(e)


@service.command(name="start")
def service_start():
    """Start the installed service."""
    from micropaw import service as svc
    try:
        svc.start()
    except svc.ServiceError as e:
        _fail(e)
    console.print("Started.")


@service.command(name="stop")
def service_stop():
    """Stop the gateway (service or a headless run) and wait for it to exit."""
    from micropaw import service as svc
    try:
        done = svc.stop()
    except svc.ServiceError as e:
        _fail(e)
    console.print("Stopped." if done else "[red]It did not stop in time.[/red]")
    sys.exit(0 if done else 1)


@service.command(name="status")
def service_status():
    """Show whether the service is installed and the gateway is healthy."""
    from micropaw import service as svc
    try:
        console.print(f"Service: {svc.state()}", markup=False)
    except svc.ServiceError as e:
        _fail(e)
    good, detail = svc.healthy()
    console.print(f"Gateway: {detail}", markup=False)
    sys.exit(0 if good else 1)


@service.command(name="logs")
@click.option("-n", "count", default=50, help="How many lines to show.")
@click.option("--follow", "-f", is_flag=True, help="Keep printing new lines.")
def service_logs(count, follow):
    """Show the gateway log."""
    import subprocess
    import time

    from micropaw import service as svc
    if sys.platform == "linux" and svc.unit_path().exists() and shutil.which("journalctl"):
        cmd = ["journalctl", "--user", "-u", svc.UNIT_NAME, "-n", str(count), "--no-pager"]
        sys.exit(subprocess.call(cmd + (["-f"] if follow else [])))
    path = svc.log_file()
    for line in svc.tail_lines(path, count):
        click.echo(line)
    if follow:
        try:
            seen = len(svc.tail_lines(path, 10**9))
            while True:
                time.sleep(1)
                lines = svc.tail_lines(path, 10**9)
                for line in lines[seen:] if len(lines) >= seen else lines:
                    click.echo(line)
                seen = len(lines)
        except KeyboardInterrupt:
            pass


def _run_install_command(cmd: list[str]) -> bool:
    import subprocess
    console.print(f"Running: {' '.join(cmd)}", markup=False)
    try:
        ok = subprocess.run(cmd).returncode == 0
    except OSError as e:
        console.print(f"[red]{e}[/red]")
        ok = False
    if not ok:
        console.print("[red]That failed.[/red] Run it yourself from a new terminal. On Windows a running "
                      "paw.exe can block the upgrade; stop the service first.")
    return ok


@main.command()
@click.option("--yes", "-y", is_flag=True, help="Do not ask before upgrading.")
def update(yes):
    """Show the latest version and upgrade the way MicroPaw was installed."""
    from micropaw import service as svc
    method = svc.install_method()
    latest = svc.latest_version()
    console.print(f"Installed: {__version__} ({method})", markup=False)
    console.print(f"Latest on PyPI: {latest or 'could not check'}", markup=False)
    cmd = svc.update_command(method)
    if cmd is None:
        hint = {"docker": "Pull or rebuild the image: docker compose build --pull && docker compose up -d",
                "editable": "From source: git pull, then pip install -e . in the repo."}
        console.print(hint.get(method, "No update command for this install."), markup=False)
        return
    if latest and latest == __version__:
        console.print("Already up to date.")
        return
    console.print(f"Command: {' '.join(cmd)}", markup=False)
    if not yes and not click.confirm("Upgrade now?"):
        return
    was_running = svc.running_gateway() is not None
    try:
        if was_running:
            console.print("Stopping the gateway...")
            if not svc.stop():
                _fail(RuntimeError("The gateway did not stop. Nothing was changed."))
        ok = _run_install_command(cmd)
        if ok:
            console.print(f"Version: {__version__} -> {svc.installed_version()}", markup=False)
        if was_running:
            if svc.existing_definition() is not None:
                svc.start()
                console.print("Service started again.")
            else:
                console.print("Start the gateway again with 'paw start --headless'.")
    except svc.ServiceError as e:
        _fail(e)
    sys.exit(0 if ok else 1)


@main.group()
def extras():
    """Optional features (channels, PDF, browser, embeddings)."""


@extras.command(name="add")
@click.argument("names", nargs=-1, required=True)
def extras_add(names):
    """Install extras the way MicroPaw was installed. Names: telegram discord slack whatsapp browser pdf embeddings."""
    from micropaw import service as svc
    bad = [n for n in names if n not in svc.EXTRAS]
    if bad:
        _fail(click.BadParameter(f"Unknown extra: {', '.join(bad)}. Choose from: {', '.join(svc.EXTRAS)}"))
    sys.exit(0 if add_extras(list(names)) else 1)


def add_extras(names: list[str]) -> bool:
    """Install extras with the right tool. Returns True on success. Used by 'paw extras add' and setup."""
    from micropaw import service as svc
    method = svc.install_method()
    cmd = svc.extras_command(method, names)
    if cmd is None:
        console.print(f"Rebuild the image with: docker build --build-arg EXTRAS={','.join(names)} .", markup=False)
        return False
    return _run_install_command(cmd)


def _doctor_install_rows(table: Table, config: Config) -> None:
    """Install method, gateway, service, linger, file mode, bind address, WSL and Ollama."""
    from micropaw import service as svc

    def row(name: str, good: bool | None, detail: str) -> None:
        mark = "[yellow]warn[/yellow]" if good is None else "[green]pass[/green]" if good else "[red]FAIL[/red]"
        table.add_row(name, mark, detail)

    row("Installed with", True, svc.install_method())
    pid = svc.running_gateway()
    row("Gateway", True if pid else None, f"running, PID {pid}" if pid else "not running")
    try:
        service_state = svc.state()
    except svc.ServiceError as e:
        service_state = f"unknown ({e})"
    row("Service", True if service_state not in ("not installed",) else None, service_state)
    if sys.platform == "linux" and svc.unit_path().exists() and shutil.which("loginctl"):
        try:
            linger = svc.linger_enabled()
        except svc.ServiceError:
            linger = False
        row("Linger", linger, "on" if linger else "off: the service stops at logout. "
            f"Run: sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")
    if os.name != "nt" and CONFIG_FILE.exists():
        mode = CONFIG_FILE.stat().st_mode & 0o777
        private = not mode & 0o077
        row("Config file mode", private, f"{mode:03o}" + ("" if private else f": run chmod 600 {CONFIG_FILE}"))
    web = config.channels.webchat
    if web.enabled and web.host in ("0.0.0.0", "::", ""):
        row("WebChat bind", None, f"{web.host}:{web.port} is open to your network; use 127.0.0.1 unless you need it")


def _doctor_search(config: Config) -> None:
    """One test query through the configured search, after the owner says yes."""
    from micropaw import privacy
    from micropaw.tools import web_search
    from micropaw.tools.base import ToolContext
    web_search.set_search_config(config.search, config.privacy)
    tool = web_search.WebSearchTool()
    ctx = ToolContext(conversation_id="doctor", user_id="owner", platform="terminal", marked=True,
                      approve=lambda *_: asyncio.sleep(0, True))
    args = {"query": "MicroPaw test search", "max_results": 1}
    if ask := tool.needs_approval(args, ctx):
        click.echo(ask)
        if not click.confirm("Send this test search?"):
            return
    out = asyncio.run(tool.execute(args, ctx))
    click.echo(out.splitlines()[0] if out else "No answer.")
    if config.search.proxy and privacy.local_only():
        click.echo(f"The request was sent through {config.search.proxy.split('@')[-1]}.")


def _doctor_server_rows(table: Table, config: Config) -> None:
    """One row per model, memory-search and speech server in use: pass, or why not."""
    from micropaw.modelscan import doctor_rows
    rows = asyncio.run(doctor_rows(config))
    for name, mark, detail in rows:
        table.add_row(name, {"pass": "[green]pass[/green]", "warn": "[yellow]warn[/yellow]"}.get(
            mark, "[red]FAIL[/red]"), detail)
    if _on_wsl() and any(mark == "fail" for _, mark, _ in rows):
        table.add_row("Ollama from WSL", "[yellow]note[/yellow]",
                      "Ollama on the Windows side listens on Windows localhost only. Set OLLAMA_HOST=0.0.0.0 in "
                      "Windows, restart Ollama, and point the MicroPaw Ollama host at the Windows IP.")


def _on_wsl() -> bool:
    from pathlib import Path
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


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


# ---------------------------------------------------------------------------
# MCP subcommands
# ---------------------------------------------------------------------------

@main.group()
def mcp():
    """Manage MCP servers (extra tools from other programs)."""


_SECRET_HEADER = ("auth", "key", "token", "secret", "cookie")


@mcp.command(name="add")
@click.argument("name")
@click.argument("command", nargs=-1)
@click.option("--url", default="", help="A streamable HTTP server instead of a command.")
@click.option("--header", "headers", multiple=True, help="NAME=VALUE sent to the server. Use ${ENV_NAME} for secrets.")
def mcp_add(name, command, url, headers):
    """Add a server: a preset name, `-- COMMAND ARGS...`, or --url URL.

    Example: paw mcp add rea -- npx -y rea-agents@1.2.3
    """
    import re
    import shlex

    from micropaw.config import MCPServerConfig, save_mcp_server
    from micropaw.mcp import PRESETS

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise click.BadParameter("use letters, digits, - and _ (at most 32)")
    if command and url:
        raise click.UsageError("Give a command or --url, not both.")
    entry: dict = {"command": command[0], "args": list(command[1:])} if command else {"url": url} if url else {}
    if not entry and name in PRESETS:
        entry = dict(PRESETS[name])
    if not entry:
        presets = ", ".join(PRESETS) or "none yet"
        raise click.UsageError(
            f"Nothing to run. Presets: {presets}. Or: paw mcp add {name} -- COMMAND ARGS, or --url URL.")
    for header in headers:
        key, _, value = header.partition("=")
        if not key or not value:
            raise click.BadParameter(f"{header!r}: use NAME=VALUE")
        if any(w in key.lower() for w in _SECRET_HEADER) and "${" not in value:
            raise click.BadParameter(f"{key}: put the secret in an environment variable and write ${{ENV_NAME}}")
        entry.setdefault("headers", {})[key] = value
    cfg = MCPServerConfig(**entry)
    if cfg.url:
        console.print(f"This will connect to: {cfg.url}")
        console.print("Its tools can return anything, so every call asks you first and counts as outside content.")
    else:
        console.print(f"This will run: {shlex.join([cfg.command, *cfg.args])}")
        console.print("[yellow]It runs on this computer with your permissions. It is not sandboxed.[/yellow]")
    if not click.confirm(f"Add '{name}'?", default=False):
        console.print("Nothing was written.")
        return
    save_mcp_server(name, {**cfg.model_dump(exclude_defaults=True), "enabled": True})
    console.print(f"[green]Added '{name}'.[/green] Tools are pinned at first start. Try: paw mcp test {name}")


@mcp.command(name="list")
def mcp_list():
    """Show configured MCP servers."""
    servers = load_config().mcp_servers
    if not servers:
        console.print("[dim]No MCP servers. Add one with: paw mcp add NAME -- COMMAND ARGS[/dim]")
        return
    table = Table(title="MCP servers")
    for column in ("Name", "Runs", "State", "Tool list"):
        table.add_column(column)
    for name, cfg in servers.items():
        runs = cfg.url or " ".join([cfg.command, *cfg.args])
        table.add_row(name, runs, "on" if cfg.enabled else "off", "pinned" if cfg.tools_hash else "not pinned yet")
    console.print(table)


def _mcp_server(name):
    servers = load_config().mcp_servers
    if name not in servers:
        raise click.ClickException(f"No MCP server named '{name}'. See: paw mcp list")
    return servers[name]


@mcp.command(name="test")
@click.argument("name")
def mcp_test(name):
    """Start one server and list its tools."""
    from micropaw.mcp import MCPError, start_server

    async def _run():
        server, tools = await start_server(name, _mcp_server(name), timeout=180.0)
        await server.close()
        return tools

    try:
        tools = asyncio.run(_run())
    except (OSError, MCPError) as e:
        raise click.ClickException(f"'{name}' did not start: {e}") from None
    console.print(f"[green]'{name}' started with {len(tools)} tools:[/green]")
    for tool in tools:
        console.print(f"  {tool.name}")


@mcp.command(name="trust")
@click.argument("name")
def mcp_trust(name):
    """Approve the tool list a server has now (after it changed)."""
    from micropaw.config import pin_mcp_tools
    from micropaw.mcp import MCPError, start_server

    cfg = _mcp_server(name).model_copy(update={"tools_hash": ""})

    async def _run():
        server, tools = await start_server(name, cfg, timeout=180.0, pin=False)
        await server.close()
        return tools, server.digest

    try:
        tools, digest = asyncio.run(_run())
    except (OSError, MCPError) as e:
        raise click.ClickException(f"'{name}' did not start: {e}") from None
    console.print(f"'{name}' now offers {len(tools)} tools:")
    for tool in tools:
        console.print(f"  {tool.name}: {tool.description[:160]}")
    if click.confirm("Trust this tool list?", default=False):
        pin_mcp_tools(name, digest)
        console.print("[green]Trusted.[/green]")


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
@click.option("--on-email", is_flag=True, help="Run for each new Gmail message from --from instead of on a timer.")
@click.option("--from", "from_address", default="", help="Exact sender address (with --on-email).")
@click.option("--subject-contains", default="", help="Words the subject must contain (with --on-email).")
@click.option("--label", default="", help="Gmail label to search in (with --on-email).")
@click.option("--agent", "agent_name", default="main", help="The agent that runs the task (default: main).")
def add(description, interval, once_delay, on_email, from_address, subject_contains, label, agent_name):
    """Add a scheduled task."""
    agent_name = agent_name.lower()
    if agent_name != "main" and agent_name not in load_config().agents:
        console.print(f"[red]No agent called '{agent_name}'. See: paw agent list[/red]")
        return
    if on_email:
        from micropaw.scheduler import email_rule

        if interval or once_delay:
            console.print("[red]--on-email does not take --every or --once.[/red]")
            return
        try:
            rule = email_rule(from_address, subject_contains, label)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            return
        seconds, one_shot = 0, False
    else:
        if not interval and not once_delay:
            console.print("[red]Provide --every (recurring) or --once (one-shot).[/red]")
            return
        if interval and once_delay:
            console.print("[red]Use either --every or --once, not both.[/red]")
            return
        rule = None
        one_shot = once_delay is not None
        seconds = _parse_interval(once_delay if one_shot else interval)  # type: ignore[arg-type]

    async def _add():
        from micropaw.scheduler import Scheduler
        sched = Scheduler(str(DB_FILE), handler=None, bus=None, channels=[])
        await sched.init()
        try:
            job_id = await sched.add_job(description, seconds, one_shot=one_shot, match=rule, agent=agent_name)
            if rule:
                console.print(f"[green]Created email job #{job_id}: \"{description}\" for mail from "
                              f"{rule['from']}[/green]")
                console.print("It runs unattended: it reads the email, summarizes it and reports to you. "
                              "Anything that would change something is refused.")
            else:
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
            table.add_column("Agent")
            for j in jobs:
                email = j["trigger"] == "email"
                kind = ("on email from " + j["match"]["from"] if email
                        else "once" if j["one_shot"] else f"every {_fmt(j['interval_seconds'])}")
                next_dt = ("-" if email
                           else datetime.fromtimestamp(j["next_run_at"], tz=UTC).strftime("%Y-%m-%d %H:%M UTC"))
                state = "on" if j["enabled"] else f"paused (paw cron enable {j['id']})"
                table.add_row(str(j["id"]), j["description"], kind, next_dt, state, j["agent"])
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


# ---------------------------------------------------------------------------
# Models and agents
# ---------------------------------------------------------------------------

@main.group()
def models():
    """See, find and test the models MicroPaw can use."""


@models.command(name="list")
def models_list():
    """Show each agent's models and the built-in providers."""
    from micropaw.core.profile import resolve_profiles
    config = load_config()
    registry = Registry(config)
    try:
        profiles = resolve_profiles(config, registry)
    except ProfileError as e:
        console.print(f"[red]{e}[/red]")
        return
    table = Table(title="Agents")
    for col in ("Agent", "Model", "Fast", "Vision", "Fallbacks", "Where"):
        table.add_column(col)
    for p in profiles.values():
        where = "local only" if p.private else "cloud or mixed"
        table.add_row(p.name, p.main, p.fast or "(main)", p.vision or "-", ", ".join(p.fallbacks) or "-", where)
    console.print(table)
    presets = Table(title="Providers (use as provider:model)")
    for col in ("Name", "Address", "Key", "Where"):
        presets.add_column(col)
    for name in ("claude", "openai", "gemini", "ollama"):
        ep = registry.resolve(name)
        key = "-" if name == "ollama" else ("set" if getattr(config.provider_config, name).api_key else "missing")
        presets.add_row(name, ep.base_url, key, "local" if ep.local else "cloud")
    for name, pre in registry.presets.items():
        state = "set" if os.environ.get(pre.key_env) else "missing"
        key = f"{pre.key_env} {state}" if pre.key_env else "-"
        presets.add_row(name, pre.base_url, key, "local" if registry.is_local(f"{name}:x") else "cloud")
    console.print(presets)


@models.command()
@click.option("--host", "hosts", multiple=True, help="Also check this computer: an address or URL. Repeatable.")
@click.option("--lan", is_flag=True, help="Also look for servers on your own network. Asks first.")
@click.option("--range", "net_range", default="", help="Also look in this network, e.g. 192.168.1.0/24. Asks first.")
@click.option("--yes", "-y", is_flag=True, help="Do not ask before a network scan.")
def scan(hosts, lan, net_range, yes):
    """Look for model servers: this computer, the ones in your config, and any --host you name."""
    from micropaw import modelscan
    from micropaw.providers.ollama import ram_bytes
    from micropaw.providers.registry import pick_local
    config = load_config()
    from micropaw import privacy
    from micropaw.config import server_url
    try:
        if privacy.local_only():
            for raw in hosts:
                if not privacy.is_local_url(server_url(raw)):
                    raise ValueError(f"{raw} is not on this computer or your network, and this privacy mode "
                                     "only looks there.")
        targets, skipped = modelscan.local_only_targets(modelscan.scan_targets(config, hosts))
        network = modelscan.lan_range(net_range or None) if lan or net_range else None  # fail before any scan
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    found, silent = asyncio.run(modelscan.run_scan(targets))
    ram = ram_bytes()
    for target, server in found:
        remote = not modelscan.is_loopback(server.url)
        click.echo(f"{target.label or target.name} at {server.url}"
                   + (f" ({modelscan.where(server.url)})" if remote else ""))
        if server.problem == "key":
            click.echo(f"  This server needs a key. Put it in ~/.micropaw/.env as NAME=... and add it with: "
                       f"paw models add {modelscan.suggested_name(server.url)} {server.url} --key-env NAME")
            continue
        prefix = modelscan.usable_spec(config, target, server)
        for m in server.models:
            notes = [n for n, on in (("no tools", not m.tools), ("vision", m.vision), ("embedding", m.embedding)) if on]
            size = f"{m.size / 1e9:.1f} GB" if m.size else ""
            click.echo(f"  {(prefix + ':') if prefix else ''}{m.name}  {size} {' '.join(notes)}".rstrip())
        pick = pick_local(server.models, None if remote else ram)
        if prefix and pick.main:
            click.echo(f"  Suggested main model: {prefix}:{pick.main.name}"
                       + ("  (sized for that computer, not this one)" if remote else ""))
        elif not prefix:
            click.echo(f"  To use it: paw models add {modelscan.suggested_name(server.url)} {server.url}")
    for target in silent:
        click.echo(f"{target.label or target.name} at {target.url}: not answering (asleep, off, or a firewall)")
    for target in skipped:
        click.echo(f"{target.label or target.name} at {target.url}: skipped: not on this computer or your network")
    if not found and not silent and not network:
        console.print("[yellow]No local model server found.[/yellow] Install Ollama from https://ollama.com/download, "
                      "or start LM Studio's server (Developer tab).")
    elif ram and any(modelscan.is_loopback(s.url) for _, s in found):
        click.echo(f"Memory: {ram / 2**30:.0f} GB. Models up to 60% of that are suggested for this computer.")
    if network:
        _scan_network(*network, yes)


def _scan_network(net, addresses: list[str], yes: bool) -> None:
    from micropaw import modelscan
    if not yes and not click.confirm(
            f"This sends one short connection attempt to {len(addresses)} addresses on {net}, ports "
            f"{modelscan.PORT_LIST}. Only scan a network you own or may scan. Continue?"):
        return
    hits = asyncio.run(modelscan.scan_lan(addresses))
    if unfinished := getattr(hits, "unfinished", 0):
        click.echo(f"The scan stopped after {modelscan.SCAN_DEADLINE:.0f} s: {hits.attempts - unfinished} of "
                   f"{hits.attempts} connection attempts were checked. Run it again on a smaller range.")
    if not hits:
        click.echo(f"No model server found on {net}" + (" in the part that was checked." if unfinished else "."))
        return
    click.echo("Address               Port   Looks like   Models")
    for h in hits:
        click.echo(f"{h.host:<21} {h.port:<6} {h.label:<12} {len(h.server.models)}")
    click.echo("To use one: paw models add NAME ADDRESS, for example")
    for h in hits:
        click.echo(f"  paw models add {modelscan.suggested_name(h.url)} {h.url}")
    click.echo("Nothing was saved.")


@models.command(name="add")
@click.argument("name")
@click.argument("address")
@click.option("--key-env", default="", help="Name of the variable that holds the key (in ~/.micropaw/.env).")
@click.option("--kind", type=click.Choice(["auto", "ollama", "openai"]), default="auto", show_default=True)
@click.option("--use", type=click.Choice(["main", "fast", "vision", "fallback"]), default=None,
              help="Also use its first model for this job.")
@click.option("--yes", "-y", is_flag=True, help="Save even if nothing answered. Pick the first when several answer.")
def models_add(name, address, key_env, kind, use, yes):
    """Test a model server on another computer and save it. Nothing is saved unless it answers."""
    from micropaw import modelscan
    from micropaw.config import server_url
    from micropaw.providers.registry import pick_local
    name = name.lower()
    if problem := modelscan.check_name(name):
        raise click.ClickException(problem)
    try:
        url = server_url(address, "ollama" if kind == "ollama" else "openai")
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    config = load_config()
    if key_env and not os.environ.get(key_env):
        raise click.ClickException(f"{key_env} is not set. Put {key_env}=... in ~/.micropaw/.env and run again.")
    key = os.environ.get(key_env, "") if key_env else ""
    if refusal := modelscan.refusal_for(config, name, url, key_env):  # before anything is sent
        raise click.ClickException(refusal)
    if urlparse(url).port or "://" in address:
        kind, server = asyncio.run(modelscan.detect(url, kind, key))
    else:
        answers = asyncio.run(modelscan.probe_host(modelscan._hostport(url)[0], key))
        if len(answers) > 1 and not yes:
            for i, a in enumerate(answers, 1):
                click.echo(f"  {i}. {a.name} at {a.url}")
            server = answers[click.prompt("Which one?", type=click.IntRange(1, len(answers)), default=1) - 1]
        else:
            server = answers[0] if answers else None
        if server:
            url, kind = server.url, modelscan.kind_for(server.url)
    if server is None and not yes:
        raise click.ClickException(f"Nothing answered at {url}. Is that computer on, and is the server set to listen "
                                   "on the network? Run again with --yes to save it anyway.")
    if server is not None and server.problem == "key":
        raise click.ClickException("This server needs a key. Put it in ~/.micropaw/.env as NAME=... and run again "
                                   "with --key-env NAME.")
    models = server.models if server else []
    pick = pick_local(models, None)
    first = (pick.main or (models[0] if models else None))
    spec = f"{name}:{first.name}" if first else f"{name}:MODEL"
    saved = modelscan.stored_url(url, kind)
    try:
        modelscan.write_entry(name, saved, kind, key_env, use or "", spec, config)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    if models:
        click.echo(f"Found {len(models)} model(s) at {url}:")
        for m in models:
            click.echo(f"  {name}:{m.name}")
    click.echo(f"Saved {name}. Use it as {spec}" + (f" (set as {use})." if use else "."))
    if server and not modelscan.is_loopback(url) and not key:
        click.echo(modelscan.NO_KEY_WARNING)


@models.command(name="remove")
@click.argument("name")
def models_remove(name):
    """Forget a model server added with `paw models add`."""
    from micropaw import modelscan
    from micropaw.config import update_config_file
    name = name.lower()
    raw = modelscan.read_raw()
    if name not in (raw.get("providers") or {}):
        raise click.ClickException(f"There is no server named '{name}' in your config.")
    if used := modelscan.uses_of(raw, name):
        raise click.ClickException(f"'{name}' is still used by {', '.join(used)}. Change those first.")
    update_config_file(lambda r: r["providers"].pop(name, None))
    click.echo(f"Removed {name}.")


@models.command(name="fetch")
@click.argument("what", type=click.Choice(["embeddings"]))
@click.option("--model", default="", help="Embedding model name (default: the one this install uses).")
def models_fetch(what, model):
    """Download the embedding model into ~/.micropaw/models, for a computer that runs offline.

    Run it on a connected machine, then copy the models folder to the offline one.
    """
    import subprocess

    from micropaw import privacy
    from micropaw.memory.embeddings import choose_local_model, models_dir
    if privacy.airgapped():
        _fail(click.ClickException(
            "Airgapped mode never downloads. Run this on a connected machine and copy "
            f"{models_dir()} to this one."))
    name = choose_local_model(DB_FILE, model or load_config().embedding.model)
    if privacy.local_only():
        console.print("Local mode: this command connects to huggingface.co. MicroPaw itself still does not.")
    console.print(f"Downloading {name} ...")
    env = {k: v for k, v in os.environ.items() if k != "HF_HUB_OFFLINE"}  # a fresh process: the library reads it once
    code = "import sys; from micropaw.memory.embeddings import fetch_local_model; fetch_local_model(sys.argv[1])"
    result = subprocess.run([sys.executable, "-c", code, name], env=env)
    if result.returncode != 0:
        _fail(click.ClickException("The download failed. fastembed is needed: pip install 'micropaw[embeddings]'."))
    console.print(f"Saved in {models_dir()}. Copy that folder to ~/.micropaw/models/fastembed on the offline machine.")


@models.command(name="test")
@click.argument("spec", required=False)
def models_test(spec):
    """Send one small chat and one tool call to a model (default: every model in your agents)."""
    from micropaw.core.profile import resolve_profiles
    config = load_config()
    registry = Registry(config)
    if spec:
        specs = [spec]
    else:
        profiles = resolve_profiles(config, registry).values()
        specs = list(dict.fromkeys(s for p in profiles for s in (p.main, p.fast, p.vision, *p.fallbacks) if s))
    table = Table(title="Model test")
    for col in ("Model", "Chat", "Tools", "Vision"):
        table.add_column(col)

    async def run_all() -> list:
        rows = [(sp, *await _test_model(registry, sp)) for sp in specs]
        await registry.close()
        return rows

    for row in asyncio.run(run_all()):
        table.add_row(*row)
    console.print(table)
    for problem in dict.fromkeys(registry.problems):
        console.print(f"[yellow]{problem}[/yellow]")


async def _test_model(registry: Registry, spec: str) -> tuple[str, str, str]:
    import time

    from micropaw.core.profile import providers_in
    from micropaw.providers.base import Message, ToolDef
    provider = registry.get(spec)
    if provider is None:
        return "not usable", "-", "-"
    try:
        start = time.monotonic()
        await asyncio.wait_for(provider.chat([Message(role="user", content="Reply with one word.")]), 180)
        chat = f"ok, {time.monotonic() - start:.1f}s"
        tool = ToolDef("ping", "Call this with any number.", {
            "type": "object", "properties": {"n": {"type": "number"}}, "required": ["n"]})
        reply = await asyncio.wait_for(provider.chat(
            [Message(role="user", content="Call the ping tool with n=1.")], [tool]), 180)
        tools = "ok" if any(c.name == "ping" for c in reply.tool_calls) else "did not call it"
    except Exception as e:
        from micropaw.core.agent import _classify_error
        return f"failed: {_classify_error(e)}", "-", "-"
    vision = "not tested"
    for p in providers_in(provider):
        if hasattr(p, "load_capabilities"):
            info = await p.load_capabilities()
            vision = "yes" if info and info.vision else "no"
    return chat, tools, vision


@main.group()
def docs():
    """Your own documents, searched offline."""


@docs.command(name="add")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=str))
def docs_add(folder):
    """Index the .txt, .md and .pdf files in a folder for offline keyword search.

    Indexing sends nothing. In normal mode a cloud embedder may embed the pieces at the next start.
    """
    from pathlib import Path

    from micropaw import docsearch
    from micropaw.memory.store import MemoryStore
    quiet_console()

    async def run() -> tuple[int, int, list[str]]:
        store = MemoryStore(DB_FILE)  # no embedder: nothing is sent anywhere
        await store.init()
        files = chunks = 0
        notes: list[str] = []
        try:
            for path in await asyncio.to_thread(docsearch.find_files, Path(folder)):
                if path.stat().st_size > docsearch.MAX_FILE_BYTES:
                    notes.append(f"{path} is too big (over 10 MB), skipped")
                    continue
                try:
                    text = await asyncio.to_thread(docsearch.read_file, path)
                except (OSError, ValueError) as e:
                    notes.append(f"{path}: {e}")
                    continue
                if n := await store.ingest_document(str(path), text):
                    files, chunks = files + 1, chunks + n
        finally:
            await store.close()
        return files, chunks, notes

    files, chunks, notes = asyncio.run(run())
    for note in notes:
        click.echo(note)
    click.echo(f"Indexed {files} file(s), {chunks} piece(s). Search them with the web_search tool when "
               "search.provider is offline or docs.")


@main.group()
def agent():
    """Named agents: each has its own model, persona and tools."""


def _tool_names(config: Config) -> list[str]:
    return _create_registry(config).names


@agent.command(name="add")
@click.argument("name")
@click.option("--model", required=True, help="Main model, provider:model (e.g. ollama:qwen3.5:9b).")
@click.option("--fast", default=None, help="Model for summaries and sub-agents (empty = the main model).")
@click.option("--vision", default=None, help="Model that describes photos.")
@click.option("--fallback", "fallbacks", multiple=True, help="Fallback model, repeatable.")
@click.option("--strict", is_flag=True, help="No fallbacks at all (ignore the top-level list).")
@click.option("--persona", default="", help="Personality, e.g. 'terse senior engineer'.")
@click.option("--allow", "allow", multiple=True, help="Tool the agent may use (only these), repeatable.")
@click.option("--deny", "deny", multiple=True, help="Tool the agent may not use, repeatable. Patterns: mcp_*.")
@click.option("--workspace", default="", help="Folder for its files and shell.")
@click.option("--separate-history", is_flag=True, help="Give it its own conversations.")
@click.option("--allow-cloud", is_flag=True, help="Let a local agent use cloud models you list.")
@click.option("--route", "routes", multiple=True, help="Chat for this agent: telegram:-100123 or discord:*.")
def agent_add(name, model, fast, vision, fallbacks, strict, persona, allow, deny, workspace,
              separate_history, allow_cloud, routes):
    """Add an agent to the config after checking it. Restart `paw start` to use it."""
    from micropaw.config import update_config_file
    from micropaw.core.profile import resolve_profiles, unknown_tool_names

    entry: dict = {"model": model}
    if fast is not None:
        entry["fast"] = fast
    if vision is not None:
        entry["vision"] = vision
    if fallbacks or strict:
        entry["fallbacks"] = list(fallbacks)
    if persona:
        entry["persona"] = {"personality": persona}
    if allow or deny:
        entry["tools"] = {"allow": list(allow), "deny": list(deny)}
    if workspace:
        entry["workspace"] = workspace
    if separate_history:
        entry["history"] = "separate"
    if allow_cloud:
        entry["allow_cloud"] = True

    def change(raw: dict) -> None:
        agents = raw.setdefault("agents", {})
        if name in agents:
            raise ValueError(f"Agent '{name}' already exists. Remove it first: paw agent remove {name}")
        agents[name] = entry
        for chat in routes:
            raw.setdefault("routes", {})[chat] = name

    def validate(cfg: Config) -> None:
        resolve_profiles(cfg, Registry(cfg))
        if bad := unknown_tool_names([*allow, *deny], _tool_names(cfg)):
            raise ValueError(f"Unknown tool name(s): {', '.join(bad)}. Type /tools in the terminal chat for the names.")

    try:
        update_config_file(change, validate)
    except (ValueError, ProfileError) as e:
        raise click.ClickException(str(e))
    console.print(f"[green]Agent '{name}' added.[/green] Restart paw start to use it. "
                  f"In a chat, /agent {name} switches to it; /whoami shows the chat ID for --route.")


@agent.command(name="list")
def agent_list():
    """List the agents and where each one runs."""
    models_list.callback()


@agent.command(name="remove")
@click.argument("name")
def agent_remove(name):
    """Remove an agent and the routes that point to it."""
    from micropaw.config import update_config_file

    def change(raw: dict) -> None:
        if name not in raw.get("agents", {}):
            raise ValueError(f"No agent called '{name}' in the config.")
        del raw["agents"][name]
        raw["routes"] = {k: v for k, v in raw.get("routes", {}).items() if v != name}

    try:
        update_config_file(change)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]Agent '{name}' removed.[/green] Restart paw start to apply.")


if __name__ == "__main__":
    main()
