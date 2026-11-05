"""Long-term memory tool — save, search, list, delete memories."""

from __future__ import annotations

import logging
from typing import Any

from micropaw.memory.store import MemoryStore
from micropaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

# Injected at startup
_store: MemoryStore | None = None


def set_store(store: MemoryStore) -> None:
    global _store
    _store = store


class MemoryTool(Tool):
    name = "memory"
    description = (
        "Long-term memory. Actions: save (key and value), search (query), list, delete (key)."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action to perform",
                "enum": ["save", "search", "list", "delete"],
            },
            "key": {"type": "string", "description": "Memory key (for save/delete)"},
            "value": {"type": "string", "description": "Memory value (for save)"},
            "query": {"type": "string", "description": "Search query (for search)"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags (for save)",
            },
        },
        "required": ["action"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if _store is None:
            return "Error: Memory store not initialized."

        action = args["action"]
        # Shared across channels; an agent with its own history (private agents) has its own memories
        user_id = context.memory_user

        if action == "save":
            key = args.get("key", "")
            value = args.get("value", "")
            if not key or not value:
                return "Error: 'save' requires both 'key' and 'value'."
            tags = args.get("tags")
            await _store.save_memory(key, value, tags=tags, user_id=user_id, marked=context.marked)
            return f"Memory saved: {key}"

        elif action == "search":
            query = args.get("query", "")
            if not query:
                return "Error: 'search' requires 'query'."
            results = await _store.search_memories(query, limit=10, user_id=user_id, include_local=context.local_data)
            if not results:
                return f"No memories found for: {query}"
            if await _store.any_marked_memory([r["key"] for r in results], user_id=user_id):
                context.marked = True
            lines = [f"Found {len(results)} memories:"]
            for r in results:
                tags = f" [tags: {r['tags']}]" if r.get("tags") else ""
                lines.append(f"- {r['key']}: {r['value']}{tags}")
            return "\n".join(lines)

        elif action == "list":
            memories = await _store.get_all_memories(user_id=user_id, include_local=context.local_data)
            if not memories:
                return "No memories stored yet."
            if any(m.get("marked") for m in memories):
                context.marked = True
            lines = [f"All memories ({len(memories)}):"]
            for m in memories:
                tags = f" [tags: {m['tags']}]" if m.get("tags") else ""
                lines.append(f"- {m['key']}: {m['value']}{tags}")
            return "\n".join(lines)

        elif action == "delete":
            key = args.get("key", "")
            if not key:
                return "Error: 'delete' requires 'key'."
            deleted = await _store.delete_memory(key, user_id=user_id)
            return f"Memory deleted: {key}" if deleted else f"Memory not found: {key}"

        return f"Unknown action: {action}"
