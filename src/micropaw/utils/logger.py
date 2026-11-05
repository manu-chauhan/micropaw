"""Rich-based structured logging for MicroPaw."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from micropaw.config import private_dir
from micropaw.utils.redact import redact

_configured = False

_LOG_DIR = Path(os.environ.get("MICROPAW_HOME") or Path.home() / ".micropaw") / "logs"
# One file per kind of process: on Windows a service and a chat can't share a rotating file
_LOG_FILE = _LOG_DIR / ("gateway.log" if sys.argv[1:2] == ["start"] else "chat.log")
_MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 3


class _PrivateFileHandler(RotatingFileHandler):
    """A log file only its owner can read, also after a rotation creates a new one (POSIX)."""

    def _open(self):  # type: ignore[no-untyped-def]
        if os.name == "nt":
            return super()._open()
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)  # an older log made with the default mode
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


class RedactFilter(logging.Filter):
    """Removes known secrets from every record (message and traceback) before a handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = redact(msg)
        if record.exc_info and record.exc_info[0] is not None:
            trace = logging.Formatter().formatException(record.exc_info)
            clean_trace = redact(trace)
            if clean_trace != trace:  # fold the cleaned traceback into the message
                clean += "\n" + clean_trace
                record.exc_info = record.exc_text = None
        if clean != msg:
            record.msg, record.args = clean, None
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure logging with Rich output + file logging. Call once at startup."""
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

    # File logging: always enabled (users need it for debugging)
    log_file = os.getenv("MICROPAW_LOG_FILE")
    log_path = Path(log_file) if log_file else _LOG_FILE
    private_dir(log_path.parent)
    file_handler = _PrivateFileHandler(
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

    for h in handlers:
        h.addFilter(RedactFilter())

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        handlers=handlers,
    )

    # Quiet noisy libraries
    for name in ("httpx", "httpcore", "asyncio", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)


def quiet_console() -> None:
    """Suppress console log output to WARNING+ (for interactive chat/start modes).

    INFO and DEBUG still go to the log file at ~/.micropaw/logs/.
    """
    for handler in logging.root.handlers:
        if isinstance(handler, RichHandler):
            handler.setLevel(logging.WARNING)
            break
