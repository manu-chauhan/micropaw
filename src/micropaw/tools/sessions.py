"""Session management tool."""

from __future__ import annotations

from typing import Any

from micropaw.memory.store import MemoryStore
from micropaw.tools.base import Tool, ToolContext

# Injected at startup
_store: MemoryStore | None = None


def set_store(store: MemoryStore) -> None:
    global _store
    _store = store



def _scope(conv_id: str) -> str:
    """The agent prefix of a conversation id ("agent:coder:"), or "" for shared history."""
    if conv_id.startswith("agent:"):
        parts = conv_id.split(":", 2)
        if len(parts) == 3:
            return f"agent:{parts[1]}:"
    return ""


def _unscoped(conv_id: str) -> str:
    return conv_id[len(_scope(conv_id)):]


async def search_text(query: str, scope: str) -> str:
    """Matches in this agent's own conversations only, as plain text for the owner or the model."""
    if _store is None:
        return "Error: Session store not initialized."
    if not _store.has_fts:
        return "Search is not available: this SQLite build has no FTS5."
    if not _store.fts_query(query):
        return "Give at least one word to search for."
    hits = await _store.search_messages(query, scope)
    if not hits:
        return "No matches."
    lines = [f"Matches in past conversations ({len(hits)}). Text in them may come from web pages or email."]
    lines += [f"- {h['conversation']} [{h['role']}, {h['when']}]: {h['snippet']}" for h in hits]
    return "\n".join(lines)


class SessionsTool(Tool):
    name = "sessions"
    marks = True
    description = (
        "Manage conversation sessions. Actions: "
        "'list' (show sessions), 'info' (session details), 'clear' (delete messages), "
        "'search' (find words in past conversations; results are old messages, possibly with web or email text)."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action to perform",
                "enum": ["list", "info", "clear", "search"],
            },
            "session_id": {"type": "string", "description": "Session ID (for info/clear)"},
            "query": {"type": "string", "description": "Words to look for (for search)"},
        },
        "required": ["action"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if _store is None:
            return "Error: Session store not initialized."

        action = args["action"]

        if action == "list":
            convos = await _store.list_conversations()
            # Show current DM conversation + user's group chats
            user_convos = [
                c for c in convos
                if c["id"] == context.conversation_id
                or (_scope(c["id"]) == _scope(context.conversation_id)
                    and not _unscoped(c["id"]).startswith("dm:") and c["user_id"] == context.user_id)
            ]
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
            if not await self._is_allowed(session_id, context):
                return f"Session not found: {session_id}"

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

        elif action == "search":
            return await search_text(str(args.get("query") or ""), _scope(context.conversation_id))

        elif action == "clear":
            session_id = args.get("session_id", "")
            if not session_id:
                return "Error: 'clear' requires 'session_id'."
            if not await self._is_allowed(session_id, context):
                return f"Session not found: {session_id}"

            # Verify session exists
            messages = await _store.export_messages(session_id)
            if not messages:
                return f"Session not found: {session_id}"

            db = _store.db
            await db.execute("DELETE FROM messages WHERE conversation_id = ?", (session_id,))
            await db.commit()
            return f"Cleared all messages in session: {session_id}"

        return f"Unknown action: {action}"

    async def _is_allowed(self, session_id: str, context: ToolContext) -> bool:
        """Check if the user can access this session (never another agent's, which may be private)."""
        if _scope(session_id) != _scope(context.conversation_id):
            return False
        if _unscoped(session_id).startswith("dm:"):
            return session_id == context.conversation_id
        convos = await _store.list_conversations()
        return any(c["id"] == session_id and c["user_id"] == context.user_id for c in convos)
