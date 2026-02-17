"""Rich-based structured logging for MicroPaw."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_configured = False

_LOG_DIR = Path.home() / ".micropaw" / "logs"
_LOG_FILE = _LOG_DIR / "micropaw.log"
_MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 3


def setup_logging(level: str = "INFO") -> None:
    """Configure logging with Rich output + optional file logging. Call once at startup."""
    global _configured
    if _configured:
        return
    _configured = True

    log_level = getattr(logging, level.upper(), logging.INFO)

    console = Console(stderr=True)
    handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
    )

    handlers: list[logging.Handler] = [handler]

    # File logging: enabled via MICROPAW_LOG_FILE env var or defaults to ~/.micropaw/logs/
    log_file = os.getenv("MICROPAW_LOG_FILE")
    enable_file_log = log_file or os.getenv("MICROPAW_FILE_LOG", "").lower() in ("1", "true", "yes")

    if enable_file_log:
        log_path = Path(log_file) if log_file else _LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(log_path),
            maxBytes=_MAX_LOG_SIZE,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        file_handler.setLevel(log_level)
        handlers.append(file_handler)

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        handlers=handlers,
    )

    # Quiet noisy libraries
    for name in ("httpx", "httpcore", "asyncio", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)
