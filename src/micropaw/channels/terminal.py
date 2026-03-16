"""Local terminal chat channel — always available, no auth needed."""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from micropaw.channels.base import Channel

console = Console()

SLASH_COMMANDS = {
    "/help": "Show available commands",
    "/new": "Start a fresh conversation",
    "/tools": "List available tools and their status",
    "/usage": "Show token usage for this session",
    "/clear": "Clear the screen",
    "/config": "Show current config summary",
}


class TerminalChannel(Channel):
    """CLI REPL channel for direct terminal interaction."""

    name = "terminal"

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._session_id: str = uuid.uuid4().hex[:8]
        self._total_usage: dict[str, int] = {"input": 0, "output": 0}
        self._last_delta: dict[str, int] = {"input": 0, "output": 0}
        self._message_count: int = 0
        # Set by gateway for tool visibility and config access
        self._registry: Any = None
        self._config: Any = None
        # Callback to notify agent of conversation reset
        self._on_new_conversation: Callable[[], Awaitable[None]] | None = None

    async def start(self) -> None:
        self._running = True

        # Suppress log noise during interactive chat — but keep tool calls visible
        logging.getLogger("micropaw").setLevel(logging.WARNING)
        # Re-enable agent tool logging so users see what tools are used
        logging.getLogger("micropaw.core.agent").setLevel(logging.INFO)

        console.print()
        console.print(Panel(
            "[bold cyan]MicroPaw[/bold cyan]\n"
            "[dim]Your AI. Your machine. Your keys.[/dim]\n\n"
            "Type your message and press Enter.\n"
            "Type [bold]/help[/bold] for commands, [bold]quit[/bold] to stop.",
            border_style="bright_cyan",
            padding=(1, 2),
        ))
        console.print()

        while self._running:
            try:
                line = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: console.input("[bold green]You >[/bold green] ")
                )
            except (EOFError, KeyboardInterrupt):
                break

            line = line.strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                console.print("\n[dim]Goodbye![/dim]")
                break

            # Handle slash commands
            if line.startswith("/"):
                handled = await self._handle_command(line)
                if handled:
                    continue
            # Shell escape: !ls, !pwd, etc.
            if line.startswith("!"):
                cmd = line[1:].strip()
                if cmd:
                    await self._run_shell(cmd)
                continue
            self._message_count += 1

            if self._stream_handler:
                await self._handle_streaming(line)
            elif self._handler:
                await self._handle_batch(line)

    async def _handle_command(self, cmd: str) -> bool:
        """Handle a slash command. Returns True if handled."""
        parts = cmd.lower().split()
        command = parts[0]

        if command == "/help":
            table = Table(title="Commands", show_header=False, border_style="dim")
            table.add_column("Command", style="cyan")
            table.add_column("Description")
            for name, desc in SLASH_COMMANDS.items():
                table.add_row(name, desc)
            table.add_row("!cmd", "Run a shell command (e.g. !ls -la)")
            table.add_row("quit", "Exit MicroPaw")
            console.print(table)
            return True

        elif command == "/new":
            self._session_id = uuid.uuid4().hex[:8]
            self._total_usage = {"input": 0, "output": 0}
            self._last_delta = {"input": 0, "output": 0}
            self._message_count = 0
            if self._on_new_conversation:
                await self._on_new_conversation()
            console.print("[dim]Started a new conversation.[/dim]\n")
            return True

        elif command == "/tools":
            if self._registry:
                table = Table(title="Available Tools", border_style="dim")
                table.add_column("Tool", style="cyan")
                table.add_column("Description")
                for tool in self._registry.all():
                    table.add_row(tool.name, tool.description)
                console.print(table)
            else:
                console.print("[dim]No tool info available.[/dim]")
            return True

        elif command == "/usage":
            console.print(Panel(
                f"[bold]Session Usage[/bold]\n\n"
                f"Messages: {self._message_count}\n"
                f"Input tokens: {self._total_usage['input']:,}\n"
                f"Output tokens: {self._total_usage['output']:,}\n"
                f"Total tokens: {self._total_usage['input'] + self._total_usage['output']:,}\n"
                f"Session ID: {self._session_id}",
                border_style="dim",
            ))
            return True

        elif command == "/clear":
            console.clear()
            return True

        elif command == "/config":
            if self._config:
                import json

                from micropaw.config import display_config
                console.print_json(json.dumps(display_config(self._config), indent=2))
            else:
                console.print("[dim]No config available.[/dim]")
            return True

        return False  # Not a known command — send to AI

    async def _handle_batch(self, text: str) -> None:
        console.print("[dim italic]  thinking...[/dim italic]", end="\r")
        response = await self._handler(text, "terminal-user", "terminal", None)
        console.print(" " * 40, end="\r")
        self._print_response(response)
        self._show_usage_hint()

    async def _handle_streaming(self, text: str) -> None:
        first_chunk = True

        async def on_chunk(chunk: str) -> None:
            nonlocal first_chunk
            if first_chunk:
                sys.stdout.write("\r" + " " * 40 + "\r")
                console.print("[bold cyan]MicroPaw >[/bold cyan] ", end="")
                first_chunk = False
            sys.stdout.write(chunk)
            sys.stdout.flush()

        console.print("[dim italic]  thinking...[/dim italic]", end="\r")
        response = await self._stream_handler(text, "terminal-user", "terminal", None, on_chunk)

        if not first_chunk:
            sys.stdout.write("\n\n")
            sys.stdout.flush()
        else:
            # Streaming didn't yield chunks — print the full response
            console.print(" " * 40, end="\r")
            self._print_response(response)
        self._show_usage_hint()

    def _print_response(self, response: str) -> None:
        console.print()
        console.print("[bold cyan]MicroPaw >[/bold cyan]")
        try:
            console.print(Markdown(response), style="white")
        except Exception:
            console.print(response)
        console.print()

    def _show_usage_hint(self) -> None:
        """Show dim right-aligned token usage after each response."""
        delta = self._last_delta["input"] + self._last_delta["output"]
        total = self._total_usage["input"] + self._total_usage["output"]
        if delta > 0:
            console.print(f"[dim]  +{delta:,} tokens ({total:,} total)[/dim]", justify="right")
            console.print()

    def track_usage(self, usage: dict[str, int]) -> None:
        """Track token usage from agent responses."""
        self._last_delta = usage.copy()
        self._total_usage["input"] += usage.get("input", 0)
        self._total_usage["output"] += usage.get("output", 0)

    async def stop(self) -> None:
        self._running = False

    async def send_message(self, chat_id: str, text: str) -> None:
        self._print_response(text)

    async def _run_shell(self, cmd: str) -> None:
        """Run a shell command and print output. Timeout after 30s."""
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if stdout:
                console.print(stdout.decode(errors="replace").rstrip())
            if stderr:
                console.print(f"[red]{stderr.decode(errors='replace').rstrip()}[/red]")
        except TimeoutError:
            await proc.kill()
            console.print("[red]Command timed out (30s limit).[/red]")
        except Exception as e:
            console.print(f"[red]{e}[/red]")

