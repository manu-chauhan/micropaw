"""File write tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from microclaw.tools.base import Tool, ToolContext
from microclaw.tools.path_security import validate_write_path

MAX_CONTENT_SIZE = 10_000_000  # 10MB


class FileWriteTool(Tool):
    name = "file_write"
    description = "Write content to a file. Creates the file if it doesn't exist."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write"},
            "content": {"type": "string", "description": "Content to write to the file"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if not context.allow_file_write:
            return "Error: File writing is not enabled. Ask the user to enable it in settings."

        path_str = args["path"]
        content = args["content"]

        if error := validate_write_path(path_str):
            return f"Error: {error}"

        if len(content) > MAX_CONTENT_SIZE:
            return f"Error: Content too large ({len(content):,} chars, max {MAX_CONTENT_SIZE:,})"

        path = Path(path_str).resolve()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"File written successfully: {path} ({len(content):,} chars)"
        except Exception as e:
            return f"Error writing file: {e}"
