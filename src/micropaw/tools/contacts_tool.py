"""Contacts tool — store and search people (name, email, phone, company)."""

from __future__ import annotations

import logging
from typing import Any

from micropaw.memory.store import MemoryStore
from micropaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

_store: MemoryStore | None = None


def set_store(store: MemoryStore) -> None:
    global _store
    _store = store


def _fmt(c: dict) -> str:
    parts = [f"- {c['name']} (ID: {c['id']})"]
    if c.get("email"):
        parts.append(f"  Email: {c['email']}")
    if c.get("phone"):
        parts.append(f"  Phone: {c['phone']}")
    if c.get("company"):
        parts.append(f"  Company: {c['company']}")
    if c.get("notes"):
        parts.append(f"  Notes: {c['notes']}")
    return "\n".join(parts)


class ContactsTool(Tool):
    name = "contacts"
    description = (
        "Manage contacts (people). Actions: 'add', 'search', 'list', 'update', 'delete'. "
        "Search contacts to find email addresses before sending email. "
        "If multiple contacts match or a person has multiple emails, present options to the user."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action to perform",
                "enum": ["add", "search", "list", "update", "delete"],
            },
            "name": {"type": "string", "description": "Contact name (for add)"},
            "email": {"type": "string", "description": "Email address (for add/update)"},
            "phone": {"type": "string", "description": "Phone number (for add/update)"},
            "company": {"type": "string", "description": "Company name (for add/update)"},
            "notes": {"type": "string", "description": "Notes about this contact (for add/update)"},
            "query": {"type": "string", "description": "Search query — matches name, email, company (for search)"},
            "contact_id": {"type": "integer", "description": "Contact ID (for update/delete)"},
        },
        "required": ["action"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if _store is None:
            return "Error: Contact store not initialized."

        action = args["action"]

        if action == "add":
            name = args.get("name", "")
            if not name:
                return "Error: 'add' requires 'name'."
            cid = await _store.add_contact(
                name, args.get("email", ""), args.get("phone", ""),
                args.get("company", ""), args.get("notes", ""),
            )
            return f"Contact added: {name} (ID: {cid})"

        elif action == "search":
            query = args.get("query", "")
            if not query:
                return "Error: 'search' requires 'query'."
            results = await _store.search_contacts(query, include_local=context.local_data)
            if not results:
                return f"No contacts found for: {query}"
            lines = [f"Found {len(results)} contact(s):"]
            for c in results:
                lines.append(_fmt(c))
            return "\n".join(lines)

        elif action == "list":
            contacts = await _store.get_all_contacts(include_local=context.local_data)
            if not contacts:
                return "No contacts stored yet."
            lines = [f"All contacts ({len(contacts)}):"]
            for c in contacts:
                lines.append(_fmt(c))
            return "\n".join(lines)

        elif action == "update":
            cid = args.get("contact_id")
            if not cid:
                return "Error: 'update' requires 'contact_id'."
            fields = {k: args[k] for k in ("name", "email", "phone", "company", "notes") if k in args}
            if not fields:
                return "Error: Provide at least one field to update."
            ok = await _store.update_contact(cid, **fields)
            return f"Contact {cid} updated." if ok else f"Contact {cid} not found."

        elif action == "delete":
            cid = args.get("contact_id")
            if not cid:
                return "Error: 'delete' requires 'contact_id'."
            ok = await _store.delete_contact(cid)
            return f"Contact {cid} deleted." if ok else f"Contact {cid} not found."

        return f"Unknown action: {action}"
