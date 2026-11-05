"""Large tool outputs: keep the full text on disk (outside the workspace), show the model the ends."""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

from micropaw import config
from micropaw.tools.base import Tool, ToolContext

LIMIT = 12_000   # outputs longer than this are shortened
HEAD = 6_000
TAIL = 2_000
PAGE = 6_000
KEEP_DAYS = 7

_last_purge = 0.0


def _dir(conv_id: str) -> Path:
    return config.CONFIG_DIR / "outputs" / re.sub(r"[^A-Za-z0-9_.-]", "_", conv_id)


def _purge() -> None:
    """Delete saved outputs older than KEEP_DAYS, and folders that end up empty."""
    root = config.CONFIG_DIR / "outputs"
    cutoff = time.time() - KEEP_DAYS * 86400
    for folder in root.glob("*"):
        for f in folder.glob("*.txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass


def _save(conv_id: str, text: str) -> int:
    """Write the text as the next numbered file (mode 0600, folder 0700). Returns its number."""
    global _last_purge
    folder = _dir(conv_id)
    if time.time() - _last_purge > 86400:  # at the first save after start, then daily
        _last_purge = time.time()
        _purge()
    folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.mkdir(exist_ok=True, mode=0o700)
    n = max((int(p.stem) for p in folder.glob("*.txt") if p.stem.isdigit()), default=0) + 1
    while True:
        try:
            fd = os.open(folder / f"{n}.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return n


async def shorten(conv_id: str, text: str) -> str:
    """The text itself when short; otherwise the start and end, with a note about where the rest is."""
    if len(text) <= LIMIT:
        return text
    try:
        n = await asyncio.to_thread(_save, conv_id, text)
    except OSError:
        return f"{text[:HEAD]}\n\n[... {len(text) - HEAD - TAIL:,} characters left out ...]\n\n{text[-TAIL:]}"
    pages = -(-len(text) // PAGE)
    note = (f"[{len(text) - HEAD - TAIL:,} of {len(text):,} characters left out. The full text is kept as "
            f"output {n} ({pages} pages). Read it with the tool_output tool: n={n}, page=1 to {pages}.]")
    return f"{text[:HEAD]}\n\n{note}\n\n{text[-TAIL:]}"


class ToolOutputTool(Tool):
    name = "tool_output"
    marks = True  # the saved text came from outside, so reading it marks the conversation
    description = (
        "Read a saved long tool output, one page at a time. Use it when a result says its full text was kept."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "description": "Output number from the note (default: the latest)"},
            "page": {"type": "integer", "description": "Page to read, starting at 1 (default 1)"},
        },
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        try:
            n, page = int(args.get("n") or 0), max(int(args.get("page") or 1), 1)
        except (TypeError, ValueError):
            return "Error: n and page must be whole numbers."
        folder = _dir(context.conversation_id)

        def read() -> str:
            numbers = sorted(int(p.stem) for p in folder.glob("*.txt") if p.stem.isdigit())
            if not numbers:
                return "No saved outputs in this chat."
            pick = n or numbers[-1]
            if pick not in numbers:
                return f"No saved output {pick}. Saved: {', '.join(map(str, numbers))}."
            text = (folder / f"{pick}.txt").read_text(encoding="utf-8")
            pages = -(-len(text) // PAGE)
            if page > pages:
                return f"Output {pick} has {pages} pages."
            return f"Output {pick}, page {page} of {pages}:\n{text[(page - 1) * PAGE:page * PAGE]}"

        return await asyncio.to_thread(read)
