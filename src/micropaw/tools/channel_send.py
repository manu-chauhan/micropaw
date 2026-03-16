"""Channel send tool — lets the AI send messages to any active channel."""

from __future__ import annotations

import logging
from typing import Any

from micropaw.bus import MessageBus, OutboundMessage
from micropaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

# Injected at startup by the gateway
_bus: MessageBus | None = None
_channel_registry: dict[str, str] = {}  # channel_name → last-known chat_id


def set_channel_send_deps(bus: MessageBus) -> None:
    global _bus
    _bus = bus


def update_channel_chat_id(channel: str, chat_id: str) -> None:
    """Record the last-known chat_id for a channel (called by Gateway)."""
    if chat_id:
        _channel_registry[channel] = chat_id


def get_active_channels() -> list[str]:
    """Return names of channels with known chat_ids (+ terminal which needs none)."""
    return sorted(_channel_registry.keys())


class ChannelSendTool(Tool):
    name = "channel_send"
    description = (
        "Send a message to a specific channel (e.g. telegram, discord, terminal). "
        "Use this to forward information, send reminders, or relay messages across channels."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": "Target channel name (e.g. 'telegram', 'discord', 'slack', 'terminal').",
            },
            "message": {
                "type": "string",
                "description": "The message content to send.",
            },
        },
        "required": ["channel", "message"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if _bus is None or _bus.closed:
            return "Error: Message bus not available."

        channel = args["channel"].lower().strip()
        message = args["message"]

        if not message:
            return "Error: Message cannot be empty."

        # Terminal needs no chat_id
        if channel == "terminal":
            await _bus.publish_outbound(OutboundMessage(channel, "", message))
            return f"Message sent to terminal."

        chat_id = _channel_registry.get(channel)
        if not chat_id:
            available = get_active_channels() or ["none"]
            return f"Error: No active session for '{channel}'. Available channels: {', '.join(available)}"

        await _bus.publish_outbound(OutboundMessage(channel, chat_id, message))
        return f"Message sent to {channel}."
