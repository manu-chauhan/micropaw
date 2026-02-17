"""Calendar tool — Google Calendar via REST API v3 with OAuth2."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, parse_qs, urlparse

import httpx

from micropaw.config import CONFIG_DIR
from micropaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

_SCOPES = "https://www.googleapis.com/auth/calendar"
_TOKEN_FILE = CONFIG_DIR / "google_tokens.json"
_REDIRECT_PORT = 8089
_REDIRECT_URI = f"http://localhost:{_REDIRECT_PORT}"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_CALENDAR_API = "https://www.googleapis.com/calendar/v3"

# Injected at startup
_config: Any = None


def set_calendar_config(config: Any) -> None:
    global _config
    _config = config


# ---------------------------------------------------------------------------
# OAuth2 token management
# ---------------------------------------------------------------------------

def _load_tokens() -> dict | None:
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_tokens(tokens: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    try:
        _TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


async def _get_access_token() -> str | None:
    """Get a valid access token, refreshing if expired."""
    tokens = _load_tokens()
    if not tokens:
        return None

    if tokens.get("expires_at", 0) > time.time() + 60:
        return tokens["access_token"]

    # Refresh
    refresh_token = tokens.get("refresh_token")
    if not refresh_token or not _config:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(_TOKEN_URL, data={
                "client_id": _config.client_id,
                "client_secret": _config.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
            if resp.status_code != 200:
                logger.error("Calendar token refresh failed: %s", resp.text)
                return None
            data = resp.json()
            tokens["access_token"] = data["access_token"]
            tokens["expires_at"] = time.time() + data.get("expires_in", 3600)
            _save_tokens(tokens)
            return tokens["access_token"]
    except Exception as e:
        logger.error("Calendar token refresh error: %s", e)
        return None


def _run_auth_server() -> str | None:
    """Run a one-shot local HTTP server to capture the OAuth2 redirect code."""
    auth_code = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            qs = parse_qs(urlparse(self.path).query)
            auth_code = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Authorization successful!</h2><p>You can close this window.</p>")

        def log_message(self, *args):
            pass  # Suppress HTTP server logs

    server = HTTPServer(("localhost", _REDIRECT_PORT), Handler)
    server.timeout = 120  # Wait up to 2 minutes
    server.handle_request()
    server.server_close()
    return auth_code


async def _authorize() -> str:
    """Run one-time OAuth2 authorization flow."""
    if not _config or not _config.client_id or not _config.client_secret:
        return (
            "Error: Calendar not configured. Run 'paw setup' to set "
            "calendar.client_id and calendar.client_secret."
        )

    params = urlencode({
        "client_id": _config.client_id,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    })
    auth_url = f"{_AUTH_URL}?{params}"

    logger.info("Opening browser for Google Calendar authorization...")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass  # Browser may not be available

    # Capture the redirect code via local server
    auth_code = await asyncio.to_thread(_run_auth_server)
    if not auth_code:
        return (
            f"Error: Authorization timed out or was cancelled.\n"
            f"Visit this URL manually if needed:\n{auth_url}"
        )

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_TOKEN_URL, data={
                "client_id": _config.client_id,
                "client_secret": _config.client_secret,
                "code": auth_code,
                "redirect_uri": _REDIRECT_URI,
                "grant_type": "authorization_code",
            })
            if resp.status_code != 200:
                return f"Error: Token exchange failed: {resp.text}"

            data = resp.json()
            tokens = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", ""),
                "expires_at": time.time() + data.get("expires_in", 3600),
            }
            _save_tokens(tokens)
            return "Google Calendar authorized successfully. Tokens saved."
    except Exception as e:
        return f"Error during authorization: {e}"


# ---------------------------------------------------------------------------
# Calendar API helpers
# ---------------------------------------------------------------------------

async def _api_get(token: str, path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_CALENDAR_API}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def _api_post(token: str, path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_CALENDAR_API}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


async def _api_delete(token: str, path: str) -> int:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(
            f"{_CALENDAR_API}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.status_code


def _format_event(ev: dict) -> str:
    """Format a single calendar event for display."""
    summary = ev.get("summary", "(no title)")
    start = ev.get("start", {})
    end = ev.get("end", {})
    start_str = start.get("dateTime", start.get("date", "?"))
    end_str = end.get("dateTime", end.get("date", "?"))
    location = ev.get("location", "")
    description = ev.get("description", "")
    event_id = ev.get("id", "")
    link = ev.get("htmlLink", "")

    parts = [f"- {summary}"]
    parts.append(f"  Start: {start_str}")
    parts.append(f"  End: {end_str}")
    if location:
        parts.append(f"  Location: {location}")
    if description:
        desc = description[:200] + "..." if len(description) > 200 else description
        parts.append(f"  Description: {desc}")
    parts.append(f"  ID: {event_id}")
    if link:
        parts.append(f"  Link: {link}")
    return "\n".join(parts)


async def _list_events(token: str, args: dict) -> str:
    start_date = args.get("start_date", "")
    end_date = args.get("end_date", "")

    params: dict[str, Any] = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 25,
    }
    if start_date:
        params["timeMin"] = f"{start_date}T00:00:00Z" if "T" not in start_date else start_date
    if end_date:
        params["timeMax"] = f"{end_date}T23:59:59Z" if "T" not in end_date else end_date

    try:
        data = await _api_get(token, "/calendars/primary/events", params)
        events = data.get("items", [])
        if not events:
            return "No events found for the specified date range."
        lines = [f"Calendar events ({len(events)}):"]
        for ev in events:
            lines.append("")
            lines.append(_format_event(ev))
        return "\n".join(lines)
    except httpx.HTTPStatusError as e:
        return f"Error: Calendar API returned {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"Error listing events: {e}"


async def _create_event(token: str, args: dict) -> str:
    title = args.get("title", "")
    if not title:
        return "Error: 'create' requires 'title'."

    body: dict[str, Any] = {"summary": title}
    if args.get("description"):
        body["description"] = args["description"]

    if args.get("all_day"):
        start_date = args.get("start_date", "")
        end_date = args.get("end_date", start_date)
        if not start_date:
            return "Error: All-day event requires 'start_date'."
        body["start"] = {"date": start_date}
        body["end"] = {"date": end_date}
    else:
        start_time = args.get("start_time", "")
        end_time = args.get("end_time", "")
        if not start_time or not end_time:
            return "Error: Timed event requires 'start_time' and 'end_time' in ISO format."
        body["start"] = {"dateTime": start_time, "timeZone": "UTC"}
        body["end"] = {"dateTime": end_time, "timeZone": "UTC"}

    try:
        data = await _api_post(token, "/calendars/primary/events", body)
        return (
            f"Event created: {data.get('summary', title)}\n"
            f"ID: {data.get('id', '?')}\n"
            f"Link: {data.get('htmlLink', '?')}"
        )
    except httpx.HTTPStatusError as e:
        return f"Error: Calendar API returned {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"Error creating event: {e}"


async def _delete_event(token: str, args: dict) -> str:
    event_id = args.get("event_id", "")
    if not event_id:
        return "Error: 'delete' requires 'event_id'."

    try:
        await _api_delete(token, f"/calendars/primary/events/{event_id}")
        return f"Event deleted: {event_id}"
    except httpx.HTTPStatusError as e:
        return f"Error: Calendar API returned {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"Error deleting event: {e}"


async def _search_events(token: str, args: dict) -> str:
    query = args.get("query", "")
    if not query:
        return "Error: 'search' requires 'query'."

    params: dict[str, Any] = {
        "q": query,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 20,
    }
    if args.get("start_date"):
        sd = args["start_date"]
        params["timeMin"] = f"{sd}T00:00:00Z" if "T" not in sd else sd
    if args.get("end_date"):
        ed = args["end_date"]
        params["timeMax"] = f"{ed}T23:59:59Z" if "T" not in ed else ed

    try:
        data = await _api_get(token, "/calendars/primary/events", params)
        events = data.get("items", [])
        if not events:
            return f"No events matching: {query}"
        lines = [f"Search results ({len(events)} events):"]
        for ev in events:
            lines.append("")
            lines.append(_format_event(ev))
        return "\n".join(lines)
    except httpx.HTTPStatusError as e:
        return f"Error: Calendar API returned {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"Error searching events: {e}"


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------

class CalendarTool(Tool):
    name = "calendar"
    description = (
        "Manage Google Calendar. Actions: 'authorize' (one-time OAuth2 setup), "
        "'list' (events by date range), 'create' (new event), "
        "'delete' (remove event by ID), 'search' (find events by text)."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action to perform",
                "enum": ["authorize", "list", "create", "delete", "search"],
            },
            "start_date": {
                "type": "string",
                "description": "Start date in ISO format, e.g. '2026-02-20' (for list/search/all-day create)",
            },
            "end_date": {
                "type": "string",
                "description": "End date in ISO format, e.g. '2026-02-28' (for list/search/all-day create)",
            },
            "title": {
                "type": "string",
                "description": "Event title (for create)",
            },
            "description": {
                "type": "string",
                "description": "Event description (for create)",
            },
            "start_time": {
                "type": "string",
                "description": "Event start as ISO datetime, e.g. '2026-02-20T10:00:00' (for create)",
            },
            "end_time": {
                "type": "string",
                "description": "Event end as ISO datetime, e.g. '2026-02-20T11:00:00' (for create)",
            },
            "all_day": {
                "type": "boolean",
                "description": "If true, create an all-day event using start_date/end_date (for create)",
            },
            "event_id": {
                "type": "string",
                "description": "Event ID to delete (for delete)",
            },
            "query": {
                "type": "string",
                "description": "Text to search for in event titles/descriptions (for search)",
            },
        },
        "required": ["action"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        action = args["action"]

        if action == "authorize":
            return await _authorize()

        token = await _get_access_token()
        if not token:
            return (
                "Error: Not authorized. Ask the user to run the 'authorize' action first, "
                "or check that calendar credentials are configured."
            )

        if action == "list":
            return await _list_events(token, args)
        elif action == "create":
            return await _create_event(token, args)
        elif action == "delete":
            return await _delete_event(token, args)
        elif action == "search":
            return await _search_events(token, args)

        return f"Unknown action: {action}"
