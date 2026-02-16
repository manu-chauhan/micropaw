"""Image analysis tool using the AI provider."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from microclaw.providers.base import AIProvider, MediaItem, Message
from microclaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

MAX_IMAGE_SIZE = 20_000_000  # 20MB
SUPPORTED_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Injected at startup
_provider: AIProvider | None = None


def set_provider(provider: AIProvider) -> None:
    global _provider
    _provider = provider


class VisionTool(Tool):
    name = "vision"
    description = "Analyze an image file and describe its contents."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the image file"},
            "question": {"type": "string", "description": "Question to ask about the image (default: describe it)"},
        },
        "required": ["path"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if _provider is None:
            return "Error: Vision provider not configured."

        path_str = args["path"]
        question = args.get("question", "Describe this image in detail.")

        path = Path(path_str).resolve()
        if not path.exists():
            return f"Error: File not found: {path}"

        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_TYPES:
            return f"Error: Unsupported image format: {suffix}. Supported: {', '.join(SUPPORTED_TYPES)}"

        if path.stat().st_size > MAX_IMAGE_SIZE:
            return f"Error: Image too large ({path.stat().st_size:,} bytes, max {MAX_IMAGE_SIZE:,})"

        try:
            image_data = path.read_bytes()
            media = MediaItem(type="image", data=image_data, mime_type=SUPPORTED_TYPES[suffix])
            response = await _provider.chat([
                Message(role="user", content=question, media=[media]),
            ])
            return response.content or "Could not analyze the image."
        except Exception as e:
            return f"Error analyzing image: {e}"
