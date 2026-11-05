"""Append-only log of tool calls and approvals: ~/.micropaw/audit.jsonl (read with `paw log`).

It records what was done, not what was read: arguments and the approval text are cut and redacted, and a tool's
output is never written. With storage.audit = "names" only the tool name, conversation and outcome are kept.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from micropaw.config import CONFIG_DIR, private_dir
from micropaw.utils.redact import redact, redact_obj

AUDIT_FILE = CONFIG_DIR / "audit.jsonl"
_MAX_VALUE = 300  # long arguments (email bodies, file contents) are cut in the log
_MAX_BYTES = 5 * 1024 * 1024  # then audit.jsonl moves to audit.jsonl.1
_KEEP = 3  # old files kept: audit.jsonl.1 to audit.jsonl.3
_TAIL_BYTES = 64 * 1024  # `paw log` reads this much from the end, never the whole file
_names_only = False

logger = logging.getLogger(__name__)


def configure(mode: str) -> None:
    """storage.audit: "full" (default) or "names"."""
    global _names_only
    _names_only = mode == "names"


def _cut(v: Any) -> Any:
    return (v[:_MAX_VALUE] + "...") if isinstance(v, str) and len(v) > _MAX_VALUE else v


def _rotate() -> None:
    """Keep the log bounded. Each process opens the file for every line, so none keeps writing to a moved file."""
    try:
        if AUDIT_FILE.stat().st_size < _MAX_BYTES:
            return
    except OSError:
        return
    for i in range(_KEEP, 1, -1):
        older = AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.{i - 1}")
        if older.exists():
            os.replace(older, AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.{i}"))
    os.replace(AUDIT_FILE, AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.1"))


def record(tool: str, args: dict[str, Any], outcome: str, conversation: str, approval: str = "") -> None:
    """One line per tool call. outcome: ok | error | refused | declined; approval: asked text or "".

    Secrets are redacted before anything is cut, so a key split at the cut cannot survive.
    """
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "conversation": conversation,
        "tool": tool,
        "outcome": outcome,
    }
    if not _names_only:
        entry["args"] = {k: _cut(v) for k, v in redact_obj(args).items()}
        if approval:
            entry["approval"] = _cut(redact(approval))
    try:
        private_dir(AUDIT_FILE.parent)
        _rotate()
        fd = os.open(str(AUDIT_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        if os.name != "nt":
            os.fchmod(fd, 0o600)  # an older log made with the default mode
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as e:
        logger.warning("Audit log write failed: %s", e)


def _last_lines(path, n: int) -> list[str]:
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - _TAIL_BYTES))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > _TAIL_BYTES:
        lines = lines[1:]  # the first line was cut in the middle
    return lines[-n:]


def tail(n: int = 50) -> list[dict]:
    lines = _last_lines(AUDIT_FILE, n)
    if len(lines) < n:  # just rotated: the rest is in the older file
        lines = _last_lines(AUDIT_FILE.with_name(f"{AUDIT_FILE.name}.1"), n - len(lines)) + lines
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
