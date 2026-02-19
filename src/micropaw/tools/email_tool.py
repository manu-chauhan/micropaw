"""Email tool — Gmail REST API v1 via OAuth2 (shared with calendar)."""

from __future__ import annotations

import base64
import logging
import re
from email.message import EmailMessage
from typing import Any

import httpx

from micropaw.tools.base import Tool, ToolContext
from micropaw.tools.google_auth import authorize, ensure_token, api_get, api_post, api_error

logger = logging.getLogger(__name__)

_GMAIL = "https://gmail.googleapis.com/gmail/v1"
_MAX_BODY = 10_000
_MAX_INBOX = 50


def _strip_html(html: str) -> str:
    """Crude HTML-to-text: strip tags, decode entities."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in [("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">")]:
        text = text.replace(entity, char)
    text = re.sub(r"&#\d+;", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _b64_decode(data: str) -> str:
    """Decode Gmail's base64url-encoded body data."""
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """Extract text body from Gmail message payload."""
    mime = payload.get("mimeType", "")

    # Single-part message
    if mime in ("text/plain", "text/html"):
        data = payload.get("body", {}).get("data", "")
        if data:
            text = _b64_decode(data)
            return _strip_html(text) if mime == "text/html" else text

    # Multipart — prefer text/plain, fallback to text/html
    for preferred in ("text/plain", "text/html"):
        for part in payload.get("parts", []):
            if part.get("mimeType") == preferred:
                data = part.get("body", {}).get("data", "")
                if data:
                    text = _b64_decode(data)
                    return _strip_html(text) if preferred == "text/html" else text
            # Nested multipart
            for sub in part.get("parts", []):
                if sub.get("mimeType") == preferred:
                    data = sub.get("body", {}).get("data", "")
                    if data:
                        text = _b64_decode(data)
                        return _strip_html(text) if preferred == "text/html" else text

    return "(no text content)"


def _get_header(headers: list[dict], name: str) -> str:
    """Get header value by name from Gmail headers list."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


async def _fetch_headers(token: str, msg_ids: list[str]) -> list[dict]:
    """Batch-fetch message headers (metadata only)."""
    results = []
    for mid in msg_ids:
        try:
            data = await api_get(token, f"{_GMAIL}/users/me/messages/{mid}", {"format": "metadata", "metadataHeaders": "From,Subject,Date"})
            hdrs = data.get("payload", {}).get("headers", [])
            results.append({
                "id": mid,
                "from": _get_header(hdrs, "From"),
                "subject": _get_header(hdrs, "Subject") or "(no subject)",
                "date": _get_header(hdrs, "Date"),
                "snippet": data.get("snippet", ""),
            })
        except Exception as e:
            logger.debug("Failed to fetch message %s: %s", mid, e)
    return results


async def _inbox(token: str, count: int, filter_type: str) -> str:
    q = "in:inbox is:unread" if filter_type == "unread" else "in:inbox"
    try:
        data = await api_get(token, f"{_GMAIL}/users/me/messages", {"q": q, "maxResults": count})
    except httpx.HTTPStatusError as e:
        return api_error(e, "Gmail")

    messages = data.get("messages", [])
    if not messages:
        return "No unread messages." if filter_type == "unread" else "No messages found."

    headers = await _fetch_headers(token, [m["id"] for m in messages])
    label = "Unread" if filter_type == "unread" else "Recent"
    lines = [f"{label} emails ({len(headers)}):"]
    for h in headers:
        lines.append(f"\n- ID: {h['id']}\n  From: {h['from']}\n  Subject: {h['subject']}\n  Date: {h['date']}")
    return "\n".join(lines)


async def _read(token: str, message_id: str) -> str:
    try:
        data = await api_get(token, f"{_GMAIL}/users/me/messages/{message_id}", {"format": "full"})
    except httpx.HTTPStatusError as e:
        return f"Error: Message {message_id} not found." if e.response.status_code == 404 else api_error(e, "Gmail")

    hdrs = data.get("payload", {}).get("headers", [])
    body = _extract_body(data.get("payload", {}))
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY] + "\n... (truncated)"

    return (
        f"From: {_get_header(hdrs, 'From')}\n"
        f"To: {_get_header(hdrs, 'To')}\n"
        f"Subject: {_get_header(hdrs, 'Subject')}\n"
        f"Date: {_get_header(hdrs, 'Date')}\n\n{body}"
    )


async def _send(token: str, to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject or "(no subject)"
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    try:
        result = await api_post(token, f"{_GMAIL}/users/me/messages/send", {"raw": raw})
        return f"Email sent to {to} (ID: {result.get('id', '?')})"
    except httpx.HTTPStatusError as e:
        return api_error(e, "Gmail")
    except Exception as e:
        return f"Error sending email: {e}"


async def _search(token: str, query: str) -> str:
    try:
        data = await api_get(token, f"{_GMAIL}/users/me/messages", {"q": query, "maxResults": 20})
    except httpx.HTTPStatusError as e:
        return api_error(e, "Gmail")

    messages = data.get("messages", [])
    if not messages:
        return f"No messages matching: {query}"

    headers = await _fetch_headers(token, [m["id"] for m in messages])
    lines = [f"Search results ({len(headers)} messages):"]
    for h in headers:
        lines.append(f"\n- ID: {h['id']}\n  From: {h['from']}\n  Subject: {h['subject']}\n  Date: {h['date']}")
    return "\n".join(lines)


class EmailTool(Tool):
    name = "email"
    description = (
        "Manage email (Gmail). Actions: 'authorize' (one-time Google sign-in), "
        "'inbox' (list recent messages), 'read' (read a message by ID), "
        "'send' (send an email), 'search' (Gmail search syntax, "
        "e.g. 'from:alice subject:meeting after:2026/02/01')."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action to perform",
                "enum": ["authorize", "inbox", "read", "send", "search"],
            },
            "count": {
                "type": "integer",
                "description": "Number of messages to list (for inbox, default 10, max 50)",
            },
            "filter": {
                "type": "string",
                "description": "Filter: 'all' or 'unread' (for inbox)",
                "enum": ["all", "unread"],
            },
            "message_id": {
                "type": "string",
                "description": "Message ID to read (from inbox/search results)",
            },
            "to": {
                "type": "string",
                "description": "Recipient email address (for send)",
            },
            "subject": {
                "type": "string",
                "description": "Email subject (for send)",
            },
            "body": {
                "type": "string",
                "description": "Email body text (for send)",
            },
            "query": {
                "type": "string",
                "description": "Gmail search query (for search). Examples: 'from:alice', 'subject:meeting', 'is:unread after:2026/02/01'",
            },
        },
        "required": ["action"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        action = args["action"]

        if action == "authorize":
            return await authorize()

        token, err = await ensure_token("Gmail")
        if err:
            return err

        if action == "inbox":
            return await _inbox(token, min(args.get("count", 10), _MAX_INBOX), args.get("filter", "all"))
        elif action == "read":
            mid = args.get("message_id", "")
            if not mid:
                return "Error: 'read' requires 'message_id' (use inbox first to get IDs)."
            return await _read(token, mid)
        elif action == "send":
            to, subject, body = args.get("to", ""), args.get("subject", ""), args.get("body", "")
            if not to or not body:
                return "Error: 'send' requires 'to' and 'body'."
            return await _send(token, to, subject, body)
        elif action == "search":
            query = args.get("query", "")
            if not query:
                return "Error: 'search' requires 'query'."
            return await _search(token, query)

        return f"Unknown action: {action}"
