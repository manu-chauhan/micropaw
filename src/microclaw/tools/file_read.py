"""File read tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from microclaw.tools.base import Tool, ToolContext
from microclaw.tools.path_security import validate_file_path

MAX_FILE_SIZE = 1_000_000  # 1MB


class FileReadTool(Tool):
    name = "file_read"
    description = "Read the contents of a file. Returns the text content."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read"},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        path_str = args["path"]

        if error := validate_file_path(path_str):
            return f"Error: {error}"

        path = Path(path_str).resolve()
        if not path.exists():
            return f"Error: File not found: {path}"
        if not path.is_file():
            return f"Error: Not a file: {path}"
        if path.stat().st_size > MAX_FILE_SIZE:
            return f"Error: File too large ({path.stat().st_size:,} bytes, max {MAX_FILE_SIZE:,})"

        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"
