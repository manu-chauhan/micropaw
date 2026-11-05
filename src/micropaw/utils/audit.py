"""Append-only log of tool calls and approvals: ~/.micropaw/audit.jsonl (read with `paw log`)."""

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
_MAX_RESULT = 200

logger = logging.getLogger(__name__)


def _cut(v: Any) -> Any:
    return (v[:_MAX_VALUE] + "...") if isinstance(v, str) and len(v) > _MAX_VALUE else v


def record(
    tool: str, args: dict[str, Any], outcome: str, conversation: str, approval: str = "", result: str = "",
) -> None:
    """One line per tool call. outcome: ok | error | refused | declined; approval: asked text or "".

    Secrets are redacted before anything is cut, so a key split at the cut cannot survive.
    """
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "conversation": conversation,
        "tool": tool,
        "args": {k: _cut(v) for k, v in redact_obj(args).items()},
        "outcome": outcome,
    }
    if approval:
        entry["approval"] = redact(approval)
    if result:
        preview = redact(result)
        entry["result"] = preview[:_MAX_RESULT] + ("..." if len(preview) > _MAX_RESULT else "")
    try:
        private_dir(AUDIT_FILE.parent)
        fd = os.open(str(AUDIT_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as e:
        logger.warning("Audit log write failed: %s", e)


def tail(n: int = 50) -> list[dict]:
    try:
        lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]
