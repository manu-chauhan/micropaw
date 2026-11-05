"""File write tool."""

from __future__ import annotations

from typing import Any

from micropaw import privacy
from micropaw.tools.base import Tool, ToolContext
from micropaw.tools.outbound_scan import question, refusal, remote_hosts
from micropaw.tools.path_security import in_workspace, resolve_path, runs_code_later, validate_write_path

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

    def needs_approval(self, args: dict[str, Any], context: ToolContext) -> str | None:
        if not context.allow_file_write:
            return None  # execute() refuses anyway
        path = resolve_path(args.get("path", ""), context.workspace)
        if hosts := remote_hosts(str(path), str(args.get("content", ""))):
            return question(str(path), hosts)  # even inside the workspace
        if in_workspace(path, context.workspace) and not runs_code_later(path):
            return None
        return f"Write {len(args.get('content', '')):,} characters to:\n{path}"

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if not context.allow_file_write:
            return "Error: File writing is not enabled. Ask the user to enable it in settings."

        content = args["content"]
        path = resolve_path(args["path"], context.workspace)

        if error := validate_write_path(str(path)):
            return f"Error: {error}"
        if (hosts := remote_hosts(str(path), content)) and privacy.airgapped():
            return refusal(hosts)

        if len(content) > MAX_CONTENT_SIZE:
            return f"Error: Content too large ({len(content):,} chars, max {MAX_CONTENT_SIZE:,})"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"File written successfully: {path} ({len(content):,} chars)"
        except Exception as e:
            return f"Error writing file: {e}"
