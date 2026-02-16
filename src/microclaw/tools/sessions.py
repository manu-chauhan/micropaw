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
        db = _store.db

        if action == "list":
            async with db.execute(
                "SELECT c.id, c.platform, c.created_at, c.updated_at, "
                "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) as msg_count "
                "FROM conversations c WHERE c.user_id = ? "
                "ORDER BY c.updated_at DESC LIMIT 20",
                (context.user_id,),
            ) as cursor:
                rows = await cursor.fetchall()

            if not rows:
                return "No sessions found."

            lines = [f"Sessions ({len(rows)}):"]
            for cid, platform, created, updated, count in rows:
                lines.append(f"- {cid} ({platform}, {count} messages, updated: {updated})")
            return "\n".join(lines)

        elif action == "info":
            session_id = args.get("session_id", "")
            if not session_id:
                return "Error: 'info' requires 'session_id'."

            # Verify ownership
            async with db.execute(
                "SELECT platform, created_at, updated_at FROM conversations WHERE id = ? AND user_id = ?",
                (session_id, context.user_id),
            ) as cursor:
                row = await cursor.fetchone()

            if not row:
                return f"Session not found: {session_id}"

            platform, created, updated = row

            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (session_id,)
            ) as cursor:
                count = (await cursor.fetchone())[0]

            # Recent messages
            async with db.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 5",
                (session_id,),
            ) as cursor:
                recent = await cursor.fetchall()

            lines = [
                f"Session: {session_id}",
                f"Platform: {platform}",
                f"Created: {created}",
                f"Updated: {updated}",
                f"Messages: {count}",
                "\nRecent messages:",
            ]
            for role, content in reversed(recent):
                preview = content[:100] + "..." if len(content) > 100 else content
                lines.append(f"  [{role}] {preview}")
            return "\n".join(lines)

        elif action == "clear":
            session_id = args.get("session_id", "")
            if not session_id:
                return "Error: 'clear' requires 'session_id'."

            # Verify ownership
            async with db.execute(
                "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                (session_id, context.user_id),
            ) as cursor:
                if not await cursor.fetchone():
                    return f"Session not found: {session_id}"

            await db.execute("DELETE FROM messages WHERE conversation_id = ?", (session_id,))
            await db.commit()
            return f"Cleared all messages in session: {session_id}"

        return f"Unknown action: {action}"
