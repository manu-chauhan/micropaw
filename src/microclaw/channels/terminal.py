"""Local terminal chat channel — always available, no auth needed."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Callable, Awaitable

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from microclaw.channels.base import Channel

console = Console()

# Callback types
MessageHandler = Callable[[str, str, str, str | None], Awaitable[str]]
StreamHandler = Callable[[str, str, str, str | None, Callable[[str], Awaitable[None]] | None], Awaitable[str]]


class TerminalChannel(Channel):
    """CLI REPL channel for direct terminal interaction."""

    name = "terminal"

    def __init__(self) -> None:
        self._running = False
        self._handler: MessageHandler | None = None
        self._stream_handler: StreamHandler | None = None

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_stream_handler(self, handler: StreamHandler) -> None:
        self._stream_handler = handler

    async def start(self) -> None:
        self._running = True

        # Suppress log noise during interactive chat
        logging.getLogger("microclaw").setLevel(logging.WARNING)

        console.print()
        console.print(Panel(
            "[bold cyan]MicroClaw[/bold cyan]\n"
            "[dim]Your AI. Your machine. Your keys.[/dim]\n\n"
            "Type your message and press Enter.\n"
            "Type [bold]quit[/bold] or [bold]exit[/bold] to stop.",
            border_style="bright_cyan",
            padding=(1, 2),
        ))
        console.print()

        while self._running:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
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

            if self._stream_handler:
                await self._handle_streaming(line)
            elif self._handler:
                await self._handle_batch(line)

    async def _handle_batch(self, text: str) -> None:
        console.print("[dim italic]  thinking...[/dim italic]", end="\r")
        response = await self._handler(text, "terminal-user", "terminal", None)
        console.print(" " * 40, end="\r")
        self._print_response(response)

    async def _handle_streaming(self, text: str) -> None:
        first_chunk = True

        async def on_chunk(chunk: str) -> None:
            nonlocal first_chunk
            if first_chunk:
                sys.stdout.write("\r" + " " * 40 + "\r")
                console.print("[bold cyan]MicroClaw >[/bold cyan] ", end="")
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

    def _print_response(self, response: str) -> None:
        console.print()
        console.print("[bold cyan]MicroClaw >[/bold cyan]")
        try:
            console.print(Markdown(response), style="white")
        except Exception:
            console.print(response)
        console.print()

    async def stop(self) -> None:
        self._running = False

    async def send_message(self, chat_id: str, text: str) -> None:
        self._print_response(text)
