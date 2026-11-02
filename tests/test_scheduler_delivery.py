"""Tests for scheduler delivery when no terminal channel is running."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from micropaw.scheduler import Scheduler


def _sched(*channel_names: str) -> tuple[Scheduler, AsyncMock]:
    bus = SimpleNamespace(closed=False, publish_outbound=AsyncMock())
    channels = [SimpleNamespace(name=n) for n in channel_names]
    return Scheduler(":memory:", handler=None, bus=bus, channels=channels), bus.publish_outbound


@pytest.mark.asyncio
async def test_headless_heartbeat_goes_to_last_used_chat():
    s, publish = _sched("telegram", "discord")
    s.record_activity("telegram", "111")
    s.record_activity("discord", "222")
    await s._deliver_heartbeat("hello")
    msg = publish.call_args.args[0]
    assert (msg.channel, msg.chat_id, msg.content) == ("discord", "222", "hello")


@pytest.mark.asyncio
async def test_terminal_job_falls_back_to_active_channel_chat():
    s, publish = _sched("telegram")
    s.record_activity("telegram", "111")
    await s._deliver("terminal", "", "job result")
    msg = publish.call_args.args[0]
    assert (msg.channel, msg.chat_id) == ("telegram", "111")


@pytest.mark.asyncio
async def test_inactive_channel_chat_ignored_and_no_chat_logs(caplog):
    s, publish = _sched("telegram")
    s.record_activity("slack", "999")  # slack is not running
    await s._deliver("terminal", "", "x")
    publish.assert_not_called()
    assert "No compatible channel" in caplog.text


@pytest.mark.asyncio
async def test_terminal_still_preferred_when_running():
    s, publish = _sched("terminal", "telegram")
    s.record_activity("telegram", "111")
    await s._deliver("terminal", "", "x")
    assert publish.call_args.args[0].channel == "terminal"
