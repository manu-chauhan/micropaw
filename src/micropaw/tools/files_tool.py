"""Files tool — save, read, list, delete user files in ~/.micropaw/files/."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from micropaw.config import CONFIG_DIR
from micropaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

_FILES_DIR = CONFIG_DIR / "files"
_DISPLAY_LIMIT = 50 * 1024  # 50 KB truncation for display


def _safe_path(rel: str) -> Path | None:
    """Resolve a relative path and ensure it stays within _FILES_DIR."""
    try:
        target = (_FILES_DIR / rel).resolve()
        if _FILES_DIR.resolve() in target.parents or target == _FILES_DIR.resolve():
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
            target = _safe_path(rel)
            if not target:
                return "Error: Invalid path (must stay within files directory)."
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Saved: {rel} ({_human_size(len(content.encode('utf-8')))})"

        elif action == "read":
            rel = args.get("path", "")
            if not rel:
                return "Error: 'read' requires 'path'."
            target = _safe_path(rel)
            if not target or not target.is_file():
                return f"Error: File not found: {rel}"
            data = target.read_text(encoding="utf-8", errors="replace")
            if len(data) > _DISPLAY_LIMIT:
                return data[:_DISPLAY_LIMIT] + f"\n\n... (truncated, full file is {_human_size(target.stat().st_size)})"
            return data

        elif action == "list":
            rel = args.get("directory", "")
            base = _safe_path(rel) if rel else _FILES_DIR.resolve()
            if not base:
                return "Error: Invalid directory path."
            _FILES_DIR.mkdir(parents=True, exist_ok=True)
            if not base.is_dir():
                return f"Error: Directory not found: {rel or '/'}"
            entries = sorted(base.iterdir())
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
            target = _safe_path(rel)
            if not target or not target.is_file():
                return f"Error: File not found: {rel}"
            target.unlink()
            return f"Deleted: {rel}"

        return f"Unknown action: {action}"
