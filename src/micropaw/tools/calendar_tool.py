"""Calendar tool — Google Calendar via REST API v3 (shared OAuth2 with Gmail)."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from micropaw.tools.base import Tool, ToolContext
from micropaw.tools.google_auth import api_delete, api_error, api_get, api_post, authorize, bad_id, ensure_token

logger = logging.getLogger(__name__)

_CAL = "https://www.googleapis.com/calendar/v3"


def _local_zone_name() -> str | None:
    """IANA name of the system timezone, if it can be found (TZ env or /etc/localtime link)."""
    name = os.environ.get("TZ", "").lstrip(":")
    if not name:
        try:
            name = os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1]
        except (IndexError, OSError):
            return None
    return name if "/" in name or name == "UTC" else None


def _local_datetime(value: str) -> dict[str, str]:
    """Event time in the user's local zone; a naive value is read as local, an explicit offset is kept."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive -> system local zone
    out = {"dateTime": dt.isoformat()}
    if zone := _local_zone_name():
        out["timeZone"] = zone
    return out


def _add_date_params(params: dict, args: dict) -> None:
    for key, param, default_time in [("start_date", "timeMin", "T00:00:00Z"), ("end_date", "timeMax", "T23:59:59Z")]:
        if d := args.get(key):
            params[param] = d if "T" in d else f"{d}{default_time}"


def _format_event_list(events: list[dict], title: str, empty_msg: str) -> str:
    if not events:
        return empty_msg
    lines = [f"{title} ({len(events)}):"]
    for ev in events:
        lines.append("")
        lines.append(_format_event(ev))
    return "\n".join(lines)


def _format_event(ev: dict) -> str:
    """Format a single calendar event for display."""
    summary = ev.get("summary", "(no title)")
    start = ev.get("start", {})
    end = ev.get("end", {})
    start_str = start.get("dateTime", start.get("date", "?"))
    end_str = end.get("dateTime", end.get("date", "?"))
    location = ev.get("location", "")
    description = ev.get("description", "")

    parts = [f"- {summary}"]
    parts.append(f"  Start: {start_str}")
    parts.append(f"  End: {end_str}")
    if location:
        parts.append(f"  Location: {location}")
    if description:
        desc = description[:200] + "..." if len(description) > 200 else description
        parts.append(f"  Description: {desc}")
    parts.append(f"  ID: {ev.get('id', '')}")
    link = ev.get("htmlLink", "")
    if link:
        parts.append(f"  Link: {link}")
    return "\n".join(parts)


async def _list_events(token: str, args: dict) -> str:
    params: dict[str, Any] = {"singleEvents": "true", "orderBy": "startTime", "maxResults": 25}
    _add_date_params(params, args)
    try:
        data = await api_get(token, f"{_CAL}/calendars/primary/events", params)
        return _format_event_list(
            data.get("items", []), "Calendar events",
            "No events found for the specified date range.",
        )
    except httpx.HTTPStatusError as e:
        return api_error(e, "Calendar")
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
        if not start_date:
            return "Error: All-day event requires 'start_date'."
        try:
            # Google's all-day end is exclusive, so a single day ends the next day
            first = date.fromisoformat(start_date)
            last = date.fromisoformat(args["end_date"]) if args.get("end_date") else first
        except ValueError:
            return "Error: dates must be ISO format, e.g. '2026-02-20'."
        if last <= first:
            last = first + timedelta(days=1)
        body["start"] = {"date": first.isoformat()}
        body["end"] = {"date": last.isoformat()}
    else:
        start_time = args.get("start_time", "")
        end_time = args.get("end_time", "")
        if not start_time or not end_time:
            return "Error: Timed event requires 'start_time' and 'end_time' in ISO format."
        try:
            body["start"] = _local_datetime(start_time)
            body["end"] = _local_datetime(end_time)
        except ValueError:
            return "Error: Timed event times must be ISO datetimes, e.g. '2026-02-20T10:00:00'."

    try:
        data = await api_post(token, f"{_CAL}/calendars/primary/events", body)
        return (
            f"Event created: {data.get('summary', title)}\n"
            f"ID: {data.get('id', '?')}\n"
            f"Link: {data.get('htmlLink', '?')}"
        )
    except httpx.HTTPStatusError as e:
        return api_error(e, "Calendar")
    except Exception as e:
        return f"Error creating event: {e}"


async def _delete_event(token: str, args: dict) -> str:
    event_id = args.get("event_id", "")
    if not event_id:
        return "Error: 'delete' requires 'event_id'."
    if err := bad_id(event_id, "event_id"):
        return err
    try:
        await api_delete(token, f"{_CAL}/calendars/primary/events/{event_id}")
        return f"Event deleted: {event_id}"
    except httpx.HTTPStatusError as e:
        return api_error(e, "Calendar")
    except Exception as e:
        return f"Error deleting event: {e}"


async def _search_events(token: str, args: dict) -> str:
    query = args.get("query", "")
    if not query:
        return "Error: 'search' requires 'query'."
    params: dict[str, Any] = {"q": query, "singleEvents": "true", "orderBy": "startTime", "maxResults": 20}
    _add_date_params(params, args)
    try:
        data = await api_get(token, f"{_CAL}/calendars/primary/events", params)
        return _format_event_list(data.get("items", []), "Search results", f"No events matching: {query}")
    except httpx.HTTPStatusError as e:
        return api_error(e, "Calendar")
    except Exception as e:
        return f"Error searching events: {e}"


class CalendarTool(Tool):
    name = "calendar"
    description = (
        "Manage Google Calendar. Actions: 'authorize' (one-time Google sign-in), "
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
            return await authorize()

        token, err = await ensure_token("Calendar")
        if err:
            return err

        if action == "list":
            return await _list_events(token, args)
        elif action == "create":
            return await _create_event(token, args)
        elif action == "delete":
            return await _delete_event(token, args)
        elif action == "search":
            return await _search_events(token, args)

        return f"Unknown action: {action}"
