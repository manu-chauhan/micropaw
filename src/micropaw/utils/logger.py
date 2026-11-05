"""Logging for MicroPaw: Rich output on a terminal, plain lines in a service, a rotating private file always."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from micropaw.config import private_dir
from micropaw.utils.redact import redact

_configured = False
_log_content = False  # message text, search queries and file paths stay out of every log unless the owner allows it

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


def short_hash(text: str) -> str:
    """8 hex characters that tell two ids apart in a log without saying what they are."""
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def allow_content(on: bool) -> None:
    """storage.log_content: let records marked content=True reach the logs (never the default)."""
    global _log_content
    _log_content = on


class ContentFilter(logging.Filter):
    """Drops records that carry what people wrote or looked up (logger.debug(..., extra={"content": True}))."""

    def filter(self, record: logging.LogRecord) -> bool:
        return _log_content or not getattr(record, "content", False)


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


def _console_handler() -> logging.Handler:
    """Rich on a terminal (imported only here: the daemon never loads it), plain lines everywhere else."""
    if sys.stderr.isatty():
        from rich.console import Console
        from rich.logging import RichHandler
        handler: logging.Handler = RichHandler(
            console=Console(stderr=True), show_time=True, show_path=False,
            markup=False,  # "[web_search]" in a message is text, not markup
            rich_tracebacks=True, tracebacks_show_locals=False,
        )
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.name = "micropaw-console"
    return handler


def _in_docker() -> bool:
    return bool(os.environ.get("MICROPAW_IN_DOCKER")) or Path("/.dockerenv").exists()


def setup_logging(level: str = "INFO") -> None:
    """Configure logging: a console handler plus the file log. Call once at startup."""
    global _configured
    if _configured:
        return
    _configured = True

    log_level = getattr(logging, level.upper(), logging.INFO)
    handler = _console_handler()
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
    if not sys.stderr.isatty() and not _in_docker():
        # Under systemd stderr goes to the journal (on a Pi, the same SD card): one copy of each line is enough
        handler.setLevel(logging.WARNING)

    for h in handlers:
        h.addFilter(RedactFilter())
        h.addFilter(ContentFilter())

    logging.basicConfig(
        level=log_level,
        format="%(message)s",
        handlers=handlers,
    )

    # Quiet noisy libraries. aiosqlite logs every statement with its parameters at DEBUG: the message text.
    for name in ("httpx", "httpcore", "asyncio", "hpack", "aiosqlite"):
        logging.getLogger(name).setLevel(logging.WARNING)


def quiet_console() -> None:
    """Suppress console log output to WARNING+ (for interactive chat/start modes).

    INFO and DEBUG still go to the log file at ~/.micropaw/logs/.
    """
    for handler in logging.root.handlers:
        if handler.name == "micropaw-console":  # not isinstance(StreamHandler): the file handler is one too
            handler.setLevel(logging.WARNING)
            break
