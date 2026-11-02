"""Tests for calendar event creation payloads."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import micropaw.tools.calendar_tool as cal


async def _create(args: dict) -> dict:
    with patch.object(cal, "api_post", AsyncMock(return_value={})) as post:
        await cal._create_event("tok", {"title": "T", **args})
    return post.call_args.args[2]


@pytest.mark.asyncio
async def test_all_day_without_end_ends_next_day():
    body = await _create({"all_day": True, "start_date": "2026-02-28"})
    assert body["start"] == {"date": "2026-02-28"}
    assert body["end"] == {"date": "2026-03-01"}


@pytest.mark.asyncio
async def test_all_day_same_end_date_ends_next_day_and_range_kept():
    body = await _create({"all_day": True, "start_date": "2026-02-20", "end_date": "2026-02-20"})
    assert body["end"] == {"date": "2026-02-21"}
    body = await _create({"all_day": True, "start_date": "2026-02-20", "end_date": "2026-02-23"})
    assert body["end"] == {"date": "2026-02-23"}


@pytest.mark.asyncio
async def test_timed_event_uses_local_zone(monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    body = await _create({"start_time": "2026-02-20T10:00:00", "end_time": "2026-02-20T11:00:00"})
    assert body["start"]["timeZone"] == "America/Chicago"
    assert body["start"]["dateTime"].startswith("2026-02-20T10:00:00")
    assert body["start"]["dateTime"] != "2026-02-20T10:00:00"  # carries a UTC offset


@pytest.mark.asyncio
async def test_timed_event_keeps_explicit_offset():
    body = await _create({"start_time": "2026-02-20T10:00:00+05:30", "end_time": "2026-02-20T11:00:00+05:30"})
    assert body["start"]["dateTime"] == "2026-02-20T10:00:00+05:30"
