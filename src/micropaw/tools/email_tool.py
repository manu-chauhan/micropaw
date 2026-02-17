"""Email tool — read, send, search Gmail via IMAP/SMTP with App Passwords."""

from __future__ import annotations

import asyncio
import email as email_lib
import email.utils
import imaplib
import logging
import re
import smtplib
from email.header import decode_header
from email.message import EmailMessage
from typing import Any

from micropaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

_MAX_BODY = 10_000
_MAX_INBOX = 50

# Injected at startup
_config: Any = None


def set_email_config(config: Any) -> None:
    global _config
    _config = config


def _decode_header_value(raw: str) -> str:
    """Decode RFC 2047 encoded header value."""
    parts = decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def _strip_html(html: str) -> str:
    """Crude HTML-to-text: strip tags, decode entities."""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_body(msg: email_lib.message.Message) -> str:
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: try HTML
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return _strip_html(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                return _strip_html(text)
            return text
    return "(no text content)"


def _imap_connect() -> imaplib.IMAP4_SSL:
    """Create and authenticate IMAP connection."""
    imap = imaplib.IMAP4_SSL(_config.imap_host, _config.imap_port)
    imap.login(_config.email_address, _config.password)
    imap.select("INBOX")
    return imap


def _imap_inbox(count: int, filter_type: str) -> str:
    """List recent emails from inbox."""
    try:
        imap = _imap_connect()
        try:
            criterion = "UNSEEN" if filter_type == "unread" else "ALL"
            _, data = imap.uid("search", None, criterion)
            uids = data[0].split()
            if not uids:
                return "No messages found." if filter_type == "all" else "No unread messages."

            uids = uids[-count:]  # Most recent
            uids.reverse()

            lines = [f"{'Unread' if filter_type == 'unread' else 'Recent'} emails ({len(uids)}):"]
            for uid in uids:
                _, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)
                frm = _decode_header_value(msg.get("From", "?"))
                subj = _decode_header_value(msg.get("Subject", "(no subject)"))
                date = msg.get("Date", "?")
                lines.append(f"\n- UID: {uid.decode()}\n  From: {frm}\n  Subject: {subj}\n  Date: {date}")
            return "\n".join(lines)
        finally:
            imap.logout()
    except imaplib.IMAP4.error as e:
        return f"Error: IMAP error: {e}"
    except Exception as e:
        return f"Error: {e}"


def _imap_read(uid: str) -> str:
    """Read a specific email by UID."""
    try:
        imap = _imap_connect()
        try:
            _, msg_data = imap.uid("fetch", uid.encode(), "(RFC822)")
            if not msg_data or not msg_data[0]:
                return f"Error: Message UID {uid} not found."
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)

            frm = _decode_header_value(msg.get("From", "?"))
            to = _decode_header_value(msg.get("To", "?"))
            subj = _decode_header_value(msg.get("Subject", "(no subject)"))
            date = msg.get("Date", "?")
            body = _extract_body(msg)
            if len(body) > _MAX_BODY:
                body = body[:_MAX_BODY] + "\n... (truncated)"

            return (
                f"From: {frm}\nTo: {to}\nSubject: {subj}\nDate: {date}\n\n{body}"
            )
        finally:
            imap.logout()
    except imaplib.IMAP4.error as e:
        return f"Error: IMAP error: {e}"
    except Exception as e:
        return f"Error: {e}"


def _imap_search(query: str) -> str:
    """Search emails using IMAP search criteria."""
    try:
        imap = _imap_connect()
        try:
            _, data = imap.uid("search", None, query)
            uids = data[0].split()
            if not uids:
                return f"No messages matching: {query}"

            uids = uids[-20:]  # Last 20 results
            uids.reverse()

            lines = [f"Search results ({len(uids)} messages):"]
            for uid in uids:
                _, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)
                frm = _decode_header_value(msg.get("From", "?"))
                subj = _decode_header_value(msg.get("Subject", "(no subject)"))
                date = msg.get("Date", "?")
                lines.append(f"\n- UID: {uid.decode()}\n  From: {frm}\n  Subject: {subj}\n  Date: {date}")
            return "\n".join(lines)
        finally:
            imap.logout()
    except imaplib.IMAP4.error as e:
        return f"Error: IMAP search error: {e}"
    except Exception as e:
        return f"Error: {e}"


def _smtp_send(to: str, subject: str, body: str) -> str:
    """Send an email via SMTP."""
    try:
        msg = EmailMessage()
        msg["From"] = _config.email_address
        msg["To"] = to
        msg["Subject"] = subject or "(no subject)"
        msg.set_content(body)

        with smtplib.SMTP_SSL(_config.smtp_host, _config.smtp_port, timeout=30) as smtp:
            smtp.login(_config.email_address, _config.password)
            smtp.send_message(msg)

        return f"Email sent to {to} with subject: {subject}"
    except smtplib.SMTPAuthenticationError:
        return "Error: SMTP authentication failed. Check your email address and App Password."
    except Exception as e:
        return f"Error sending email: {e}"


class EmailTool(Tool):
    name = "email"
    description = (
        "Manage email. Actions: 'inbox' (list recent messages), "
        "'read' (read a specific message by UID), 'send' (send an email), "
        "'search' (search messages by IMAP query, e.g. 'FROM john', 'SUBJECT meeting', 'SINCE 01-Feb-2026')."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action to perform",
                "enum": ["inbox", "read", "send", "search"],
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
                "description": "Email UID to read (for read action)",
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
                "description": "IMAP search query (for search). Examples: 'FROM john', 'SUBJECT meeting', 'SINCE 01-Feb-2026', 'UNSEEN'",
            },
        },
        "required": ["action"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if _config is None or not _config.password:
            return "Error: Email not configured. Run 'paw setup' to configure email."

        action = args["action"]

        if action == "inbox":
            count = min(args.get("count", 10), _MAX_INBOX)
            filter_type = args.get("filter", "all")
            return await asyncio.to_thread(_imap_inbox, count, filter_type)

        elif action == "read":
            msg_id = args.get("message_id", "")
            if not msg_id:
                return "Error: 'read' requires 'message_id' (use inbox first to get UIDs)."
            return await asyncio.to_thread(_imap_read, msg_id)

        elif action == "send":
            to = args.get("to", "")
            subject = args.get("subject", "")
            body = args.get("body", "")
            if not to or not body:
                return "Error: 'send' requires 'to' and 'body'."
            return await asyncio.to_thread(_smtp_send, to, subject, body)

        elif action == "search":
            query = args.get("query", "")
            if not query:
                return "Error: 'search' requires 'query'."
            return await asyncio.to_thread(_imap_search, query)

        return f"Unknown action: {action}"
