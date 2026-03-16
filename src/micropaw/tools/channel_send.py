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
_MAX_SENDS_PER_TURN = 5
_MAX_MSG_LENGTH = 4096
_send_count: int = 0


def set_channel_send_deps(bus: MessageBus) -> None:
    global _bus, _send_count
    _bus = bus
    _channel_registry.clear()
    _send_count = 0


def update_channel_chat_id(channel: str, chat_id: str) -> None:
    """Record the last-known chat_id for a channel (called by Gateway)."""
    if chat_id:
        _channel_registry[channel] = chat_id


def get_active_channels() -> list[str]:
    """Return names of channels with known chat_ids (+ terminal which needs none)."""
    return sorted(_channel_registry.keys())


def reset_send_count() -> None:
    """Reset per-turn send counter (called by agent at start of each message)."""
    global _send_count
    _send_count = 0


def cleanup() -> None:
    """Clear all state (called by Gateway.stop())."""
    global _bus, _send_count
    _bus = None
    _channel_registry.clear()
    _send_count = 0


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
        global _send_count

        if _bus is None or _bus.closed:
            return "Error: Message bus not available."

        if _send_count >= _MAX_SENDS_PER_TURN:
            return f"Error: Maximum {_MAX_SENDS_PER_TURN} cross-channel sends per turn."

        channel = args["channel"].lower().strip()
        message = args["message"]

        if not message:
            return "Error: Message cannot be empty."

        if len(message) > _MAX_MSG_LENGTH:
            message = message[:_MAX_MSG_LENGTH]

        # Terminal needs no chat_id
        if channel == "terminal":
            if "terminal" not in _channel_registry:
                return "Error: Terminal channel is not active."
            await _bus.publish_outbound(OutboundMessage(channel, "", message))
            _send_count += 1
            return "Message sent to terminal."

        chat_id = _channel_registry.get(channel)
        if not chat_id:
            available = get_active_channels() or ["none"]
            return f"Error: No active session for '{channel}'. Available channels: {', '.join(available)}"

        await _bus.publish_outbound(OutboundMessage(channel, chat_id, message))
        _send_count += 1
        return f"Message sent to {channel}."
