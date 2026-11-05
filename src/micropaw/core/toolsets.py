"""Which tools exist, and which a model is shown. Every tool definition is sent with every request, and on a
small machine with a local model the prompt is the slow part: the default set for a local model is lean."""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

from micropaw.config import Config
from micropaw.tools.base import ToolRegistry
from micropaw.utils.prices import is_local_provider

if TYPE_CHECKING:
    from micropaw.core.profile import AgentRuntime

logger = logging.getLogger(__name__)

# What a local model gets unless toolset is "all". tool_output stays: a long result tells the model to read the
# rest with it. MCP tools stay: the owner added those on purpose.
LEAN = ("shell", "file_read", "file_write", "memory", "web_search", "web_browse", "http_request", "skill", "cron",
        "tool_output", "mcp_*")

# name -> (module, class), in the order they are registered. A module is imported only when its tool is used.
_TOOLS = {
    "web_search": ("web_search", "WebSearchTool"), "web_browse": ("web_browse", "WebBrowseTool"),
    "file_read": ("file_read", "FileReadTool"), "file_write": ("file_write", "FileWriteTool"),
    "shell": ("shell", "ShellTool"), "http_request": ("http_request", "HttpRequestTool"),
    "memory": ("memory_tool", "MemoryTool"), "pdf_read": ("pdf_read", "PdfReadTool"),
    "vision": ("vision", "VisionTool"), "sessions": ("sessions", "SessionsTool"),
    "delegate": ("delegate", "DelegateTool"), "cron": ("cron_tool", "CronTool"),
    "email": ("email_tool", "EmailTool"), "calendar": ("calendar_tool", "CalendarTool"),
    "contacts": ("contacts_tool", "ContactsTool"), "files": ("files_tool", "FilesTool"),
    "channel_send": ("channel_send", "ChannelSendTool"), "inspect": ("inspect_tool", "InspectTool"),
    "har": ("har_tool", "HarTool"), "tool_output": ("tool_output", "ToolOutputTool"),
    "skill": ("skill_tool", "SkillTool"),
}


def _wanted(name: str, config: Config) -> bool:
    """Gmail and Calendar tools exist only when Google is set up: without it they could only fail."""
    if name == "email":
        return config.google.enabled and config.google.gmail_enabled
    if name == "calendar":
        return config.google.enabled and config.google.calendar_enabled
    return True


def build_registry(config: Config) -> ToolRegistry:
    """The shared tool registry, honoring permissions.disabled_tools and the Google settings."""
    disabled = set(config.permissions.disabled_tools)
    if disabled:
        logger.info("Disabled tools: %s", ", ".join(sorted(disabled)))
    registry = ToolRegistry()
    for name, (module, cls) in _TOOLS.items():
        if name not in disabled and _wanted(name, config):
            registry.register(getattr(importlib.import_module(f"micropaw.tools.{module}"), cls)())
    return registry


def default_allow(runtime: AgentRuntime) -> list[str]:
    """The allow list a model gets when the agent has none of its own: [] means every tool.

    toolset "auto" (default) is lean for a local model and everything for a cloud model; "lean" and "all" force it.
    """
    mode = runtime.config.toolset
    if mode == "all" or (mode == "auto" and not is_local_provider(runtime.provider)):
        return []
    return [*LEAN, *(["vision"] if runtime.vision else [])]
