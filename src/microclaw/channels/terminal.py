"""Local terminal chat channel — always available, no auth needed."""

from __future__ import annotations

import asyncio
import sys
from typing import Callable, Awaitable

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

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
        console.print(Panel(
            "[bold cyan]MicroClaw Terminal[/bold cyan]\n"
            "Type your message and press Enter. Type [bold]quit[/bold] or [bold]exit[/bold] to stop.",
            border_style="cyan",
        ))

        while self._running:
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("\nYou: ")
                )
            except (EOFError, KeyboardInterrupt):
                break

            line = line.strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                console.print("[dim]Goodbye![/dim]")
                break

            if self._stream_handler:
                await self._handle_streaming(line)
            elif self._handler:
                await self._handle_batch(line)

    async def _handle_batch(self, text: str) -> None:
        console.print("[dim]MicroClaw is thinking...[/dim]", end="\r")
        response = await self._handler(text, "terminal-user", "terminal", None)
        console.print(" " * 40, end="\r")
        console.print()
        try:
            console.print(Markdown(response))
        except Exception:
            console.print(response)

    async def _handle_streaming(self, text: str) -> None:
        first_chunk = True

        async def on_chunk(chunk: str) -> None:
            nonlocal first_chunk
            if first_chunk:
                sys.stdout.write("\r" + " " * 40 + "\r")
                sys.stdout.write("\nMicroClaw: ")
                first_chunk = False
            sys.stdout.write(chunk)
            sys.stdout.flush()

        console.print("[dim]MicroClaw is thinking...[/dim]", end="\r")
        await self._stream_handler(text, "terminal-user", "terminal", None, on_chunk)

        if not first_chunk:
            sys.stdout.write("\n")
            sys.stdout.flush()

    async def stop(self) -> None:
        self._running = False

    async def send_message(self, chat_id: str, text: str) -> None:
        console.print(f"\nMicroClaw: {text}")
