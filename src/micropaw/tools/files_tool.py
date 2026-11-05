"""Files tool — save, read, list, delete user files in ~/.micropaw/files/."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from micropaw import privacy
from micropaw.config import CONFIG_DIR
from micropaw.tools.base import Tool, ToolContext
from micropaw.tools.outbound_scan import question, refusal, remote_hosts

logger = logging.getLogger(__name__)

_FILES_DIR = CONFIG_DIR / "files"
_DISPLAY_LIMIT = 50 * 1024  # 50 KB truncation for display


def local_dir() -> Path:
    """Where files saved in local or airgapped mode go: a cloud model never sees this folder."""
    return _FILES_DIR.parent / "files-local"


def _roots(context: ToolContext) -> list[Path]:
    return [local_dir(), _FILES_DIR] if context.local_data else [_FILES_DIR]


def _safe_path(rel: str, root: Path | None = None) -> Path | None:
    """Resolve a relative path and ensure it stays within the files folder (or `root`)."""
    root = root or _FILES_DIR
    try:
        target = (root / rel).resolve()
        if root.resolve() in target.parents or target == root.resolve():
            return target
    except (ValueError, OSError):
        pass
    return None


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class FilesTool(Tool):
    name = "files"
    marks = True
    description = (
        "Save, read, list, or delete user files. Files persist across sessions in ~/.micropaw/files/. "
        "Actions: 'save' (write content to a file), 'read' (read a file), "
        "'list' (list files in a directory), 'delete' (remove a file). "
        "Use for notes, documents, code snippets, or any data the user wants to keep."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action to perform",
                "enum": ["save", "read", "list", "delete"],
            },
            "path": {
                "type": "string",
                "description": "Relative file path, e.g. 'notes/meeting.md' (for save/read/delete)",
            },
            "content": {
                "type": "string",
                "description": "File content to write (for save)",
            },
            "directory": {
                "type": "string",
                "description": "Relative directory to list (for list, default: root)",
            },
        },
        "required": ["action"],
    }

    def needs_approval(self, args: dict[str, Any], context: ToolContext) -> str | None:
        if args.get("action") == "save" and (hosts := remote_hosts(str(args.get("path", "")), args.get("content", ""))):
            return question(str(args.get("path", "")), hosts)
        return None

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        action = args["action"]

        if action == "save":
            rel = args.get("path", "")
            content = args.get("content", "")
            if not rel:
                return "Error: 'save' requires 'path'."
            if not content:
                return "Error: 'save' requires 'content'."
            if len(content) > 10_000_000:
                return f"Error: Content too large ({len(content):,} chars, max 10,000,000)."
            target = _safe_path(rel, local_dir() if privacy.local_only() else _FILES_DIR)
            if not target:
                return "Error: Invalid path (must stay within files directory)."
            if (hosts := remote_hosts(rel, content)) and privacy.airgapped():
                return refusal(hosts)
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_text, content, encoding="utf-8")
            other = _safe_path(rel, _FILES_DIR if privacy.local_only() else local_dir())
            if other and other.is_file():
                other.unlink()  # one copy per name, so the older one cannot hide the new one
            return f"Saved: {rel} ({_human_size(len(content.encode('utf-8')))})"

        elif action == "read":
            rel = args.get("path", "")
            if not rel:
                return "Error: 'read' requires 'path'."
            target = next((t for r in _roots(context) if (t := _safe_path(rel, r)) and t.is_file()), None)
            if not target:
                return f"Error: File not found: {rel}"
            context.tainted = context.tainted or local_dir().resolve() in target.parents
            data = await asyncio.to_thread(target.read_text, encoding="utf-8", errors="replace")
            if len(data) > _DISPLAY_LIMIT:
                return data[:_DISPLAY_LIMIT] + f"\n\n... (truncated, full file is {_human_size(target.stat().st_size)})"
            return data

        elif action == "list":
            rel = args.get("directory", "")
            _FILES_DIR.mkdir(parents=True, exist_ok=True)
            bases = [b for r in _roots(context) if (b := _safe_path(rel, r) if rel else r.resolve()) and b.is_dir()]
            if rel and not _safe_path(rel):
                return "Error: Invalid directory path."
            if not bases:
                return f"Error: Directory not found: {rel or '/'}"
            context.tainted = context.tainted or any(b == local_dir().resolve() for b in bases)
            entries = sorted({e.name: e for b in bases for e in b.iterdir()}.values(), key=lambda e: e.name)
            if not entries:
                return "No files saved yet." if not rel else f"Empty directory: {rel}"
            lines = [f"Files ({len(entries)}):"]
            for e in entries:
                if e.is_dir():
                    lines.append(f"  {e.name}/")
                else:
                    lines.append(f"  {e.name}  ({_human_size(e.stat().st_size)})")
            return "\n".join(lines)

        elif action == "delete":
            rel = args.get("path", "")
            if not rel:
                return "Error: 'delete' requires 'path'."
            target = next((t for r in _roots(context) if (t := _safe_path(rel, r)) and t.is_file()), None)
            if not target:
                return f"Error: File not found: {rel}"
            target.unlink()
            return f"Deleted: {rel}"

        return f"Unknown action: {action}"
