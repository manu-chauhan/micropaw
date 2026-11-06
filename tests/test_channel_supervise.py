"""The shared background-task supervision in Channel (used by Discord and Slack)."""

from __future__ import annotations

import asyncio

from micropaw.channels.base import Channel


class _Chan(Channel):
    name = "demo"

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_message(self, chat_id: str, text: str) -> None: ...


async def test_a_crashed_task_is_restarted_after_five_seconds(monkeypatch, caplog):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(n):
        sleeps.append(n)
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    ch = _Chan()
    ran = asyncio.Event()

    async def crash():
        raise RuntimeError("socket gone")

    async def ok():
        ran.set()

    async def relaunch():
        return ok()

    ch._task = asyncio.create_task(crash())
    ch._supervise(relaunch)
    await asyncio.wait_for(ran.wait(), 2)
    await ch._watcher
    assert sleeps == [5]
    assert "Demo task crashed: socket gone" in caplog.text


async def test_stop_closes_the_connection_and_cancels_both_tasks():
    ch = _Chan()
    closed = []

    async def forever():
        await asyncio.sleep(3600)

    async def close():
        closed.append(True)

    ch._task = asyncio.create_task(forever())
    ch._supervise(lambda: None)
    await asyncio.sleep(0)
    await ch._stop_task(close())
    assert closed == [True]
    assert ch._task.cancelled() and ch._watcher.done()


async def test_stop_without_a_connection_is_fine():
    await _Chan()._stop_task(None)


async def test_the_crash_is_logged_under_the_channel_own_logger(monkeypatch, caplog):
    async def no_sleep(n):
        pass
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    ch = _Chan()

    async def crash():
        raise RuntimeError("socket gone")

    async def ok():
        pass

    async def relaunch():
        return ok()

    ch._task = asyncio.create_task(crash())
    ch._supervise(relaunch)
    await ch._watcher
    assert [r.name for r in caplog.records if "crashed" in r.getMessage()] == ["micropaw.channels.demo"]
