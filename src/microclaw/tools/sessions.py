"""Session management tool."""

from __future__ import annotations

from typing import Any

from microclaw.memory.store import MemoryStore
from microclaw.tools.base import Tool, ToolContext

# Injected at startup
_store: MemoryStore | None = None


def set_store(store: MemoryStore) -> None:
    global _store
    _store = store


class SessionsTool(Tool):
    name = "sessions"
    description = "Manage conversation sessions. Actions: 'list' (show sessions), 'info' (session details), 'clear' (delete messages)."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action to perform",
                "enum": ["list", "info", "clear"],
            },
            "session_id": {"type": "string", "description": "Session ID (for info/clear)"},
        },
        "required": ["action"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if _store is None:
            return "Error: Session store not initialized."

        action = args["action"]

        if action == "list":
            convos = await _store.list_conversations()
            # Filter to user's conversations
            user_convos = [c for c in convos if c["user_id"] == context.user_id]
            if not user_convos:
                return "No sessions found."

            lines = [f"Sessions ({len(user_convos)}):"]
            for c in user_convos[:20]:
                lines.append(f"- {c['id']} ({c['platform']}, updated: {c['updated_at']})")
            return "\n".join(lines)

        elif action == "info":
            session_id = args.get("session_id", "")
            if not session_id:
                return "Error: 'info' requires 'session_id'."

            messages = await _store.export_messages(session_id)
            if not messages:
                return f"Session not found: {session_id}"

            lines = [
                f"Session: {session_id}",
                f"Messages: {len(messages)}",
                "\nRecent messages:",
            ]
            for m in messages[-5:]:
                preview = m["content"][:100] + "..." if len(m["content"]) > 100 else m["content"]
                lines.append(f"  [{m['role']}] {preview}")
            return "\n".join(lines)

        elif action == "clear":
            session_id = args.get("session_id", "")
            if not session_id:
                return "Error: 'clear' requires 'session_id'."

            # Verify session exists
            messages = await _store.export_messages(session_id)
            if not messages:
                return f"Session not found: {session_id}"

            db = _store.db
            await db.execute("DELETE FROM messages WHERE conversation_id = ?", (session_id,))
            await db.commit()
            return f"Cleared all messages in session: {session_id}"

        return f"Unknown action: {action}"
