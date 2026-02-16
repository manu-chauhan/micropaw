"""Rich-based structured logging for MicroClaw."""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

_configured = False


def setup_logging(level: str = "INFO") -> None:
    """Configure logging with Rich output. Call once at startup."""
    global _configured
    if _configured:
        return
    _configured = True

    console = Console(stderr=True)
    handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
    )

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[handler],
    )

    # Quiet noisy libraries
    for name in ("httpx", "httpcore", "asyncio", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)
