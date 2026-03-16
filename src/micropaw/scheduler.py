"""Async scheduler — cron jobs + HEARTBEAT.md proactive mode."""

from __future__ import annotations

import asyncio
import logging
import time

import aiosqlite

from micropaw.bus import OutboundMessage
from micropaw.config import CONFIG_DIR

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cron_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    next_run_at REAL NOT NULL,
    one_shot INTEGER DEFAULT 0,
    user_id TEXT DEFAULT 'terminal-user',
    platform TEXT DEFAULT 'terminal',
    chat_id TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    created_at REAL NOT NULL
);
"""


class Scheduler:
    """Checks DB for due cron jobs, processes HEARTBEAT.md, fires tasks through the agent."""

    def __init__(self, db_path: str, handler, bus, channels, *, heartbeat_interval: int = 1800, max_jobs: int = 50):
        self.db_path = db_path
        self._handler = handler
        self._bus = bus
        self._channels = channels
        self._heartbeat_interval = heartbeat_interval
        self._max_jobs = max_jobs
        self._running = False
        self._db: aiosqlite.Connection | None = None
        self._last_heartbeat: float = 0.0
        self._last_chat_ids: dict[str, str] = {}  # channel_name → last-known chat_id

    def record_activity(self, channel: str, chat_id: str) -> None:
        """Record the last-known chat_id for a channel (called by Gateway on every message)."""
        if chat_id:
            self._last_chat_ids[channel] = chat_id

    async def init(self) -> None:
        from pathlib import Path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None
        return self._db

    async def run(self) -> None:
        """Main loop: check for due jobs every 60s, process heartbeat on interval."""
        self._running = True
        self._last_heartbeat = time.time()
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Scheduler tick error: %s", e)
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return

    async def _tick(self) -> None:
        now = time.time()

        # Process due cron jobs
        async with self.db.execute(
            "SELECT id, description, interval_seconds, one_shot, user_id, platform, chat_id "
            "FROM cron_jobs WHERE enabled = 1 AND next_run_at <= ?",
            (now,),
        ) as cur:
            jobs = await cur.fetchall()

        for job_id, description, interval, one_shot, user_id, platform, chat_id in jobs:
            try:
                if one_shot:
                    # One-shot jobs are reminders — deliver directly to ALL active channels
                    await self._deliver_all(platform, chat_id, f"Reminder: {description}")
                else:
                    # Recurring jobs need agent processing (e.g., "check inbox for new emails")
                    # Pass None as channel_id so DM jobs use the unified conversation,
                    # not a double-prefixed orphan like "telegram:telegram:12345".
                    response = await self._handler(
                        f"You have a scheduled task to perform. Do it now and report the result: {description}",
                        user_id, platform, None,
                    )
                    await self._deliver(platform, chat_id, f"[Cron #{job_id}] {response}")
            except Exception as e:
                logger.error("Cron job #%d failed: %s", job_id, e)

            if one_shot:
                await self.db.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
            else:
                await self.db.execute(
                    "UPDATE cron_jobs SET next_run_at = ? WHERE id = ?",
                    (now + interval, job_id),
                )
            await self.db.commit()

        # Heartbeat check
        if self._heartbeat_interval > 0 and now - self._last_heartbeat >= self._heartbeat_interval:
            self._last_heartbeat = now
            await self._process_heartbeat()

    async def _deliver(self, channel: str, chat_id: str, content: str) -> None:
        """Deliver to stored channel, fall back to terminal, then first active."""
        if not self._bus or self._bus.closed:
            logger.warning("Bus closed, cannot deliver cron result")
            return

        # Try originating channel
        active_names = {ch.name for ch in self._channels}
        if channel in active_names:
            await self._bus.publish_outbound(OutboundMessage(channel, chat_id, content))
            return

        # Fall back to terminal
        if "terminal" in active_names:
            await self._bus.publish_outbound(OutboundMessage("terminal", "", content))
            return

        # No compatible channel available — log and drop
        logger.warning("No compatible channel for delivery (origin=%s)", channel)

    async def _deliver_all(self, origin_channel: str, origin_chat_id: str, content: str) -> None:
        """Deliver to ALL active channels — for reminders and urgent notifications."""
        if not self._bus or self._bus.closed:
            logger.warning("Bus closed, cannot deliver notification")
            return

        delivered = set()
        for ch in self._channels:
            if ch.name == origin_channel:
                await self._bus.publish_outbound(OutboundMessage(ch.name, origin_chat_id, content))
                delivered.add(ch.name)
            elif ch.name == "terminal":
                await self._bus.publish_outbound(OutboundMessage("terminal", "", content))
                delivered.add(ch.name)
            else:
                # Use last-known chat_id from user activity on this channel
                chat_id = self._last_chat_ids.get(ch.name)
                if chat_id:
                    await self._bus.publish_outbound(OutboundMessage(ch.name, chat_id, content))
                    delivered.add(ch.name)
                else:
                    logger.debug("No known chat_id for channel [%s], skipping", ch.name)

        if not delivered:
            logger.warning("No channels available for notification delivery")

    async def _process_heartbeat(self) -> None:
        heartbeat_file = CONFIG_DIR / "HEARTBEAT.md"
        if not heartbeat_file.exists():
            return

        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(None, heartbeat_file.read_text)
        except OSError as e:
            logger.error("Cannot read HEARTBEAT.md: %s", e)
            return

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- [ ]"):
                task = stripped[5:].strip()
                if not task:
                    continue
                try:
                    response = await self._handler(
                        f"[Heartbeat task] {task}", "heartbeat", "heartbeat", "heartbeat",
                    )
                    await self._deliver_heartbeat(f"[Heartbeat] {response}")
                except Exception as e:
                    logger.error("Heartbeat task failed: %s", e)

    async def _deliver_heartbeat(self, content: str) -> None:
        await self._deliver("terminal", "", content)

    # -- CRUD (used by both CLI and LLM tool) --

    async def add_job(
        self, description: str, interval_seconds: int,
        user_id: str = "terminal-user", platform: str = "terminal", chat_id: str = "",
        one_shot: bool = False,
    ) -> int:
        now = time.time()
        count = await self._job_count()
        if count >= self._max_jobs:
            raise ValueError(f"Maximum of {self._max_jobs} cron jobs reached.")

        cur = await self.db.execute(
            "INSERT INTO cron_jobs (description, interval_seconds, next_run_at, one_shot, "
            "user_id, platform, chat_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (description, interval_seconds, now + interval_seconds, int(one_shot),
             user_id, platform, chat_id, now),
        )
        await self.db.commit()
        return cur.lastrowid  # type: ignore[return-value]

    async def list_jobs(self) -> list[dict]:
        async with self.db.execute(
            "SELECT id, description, interval_seconds, next_run_at, one_shot, enabled, created_at "
            "FROM cron_jobs ORDER BY id",
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r[0], "description": r[1], "interval_seconds": r[2],
             "next_run_at": r[3], "one_shot": bool(r[4]), "enabled": bool(r[5]),
             "created_at": r[6]}
            for r in rows
        ]

    async def remove_job(self, job_id: int) -> bool:
        cur = await self.db.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
        await self.db.commit()
        return cur.rowcount > 0

    async def _job_count(self) -> int:
        async with self.db.execute("SELECT COUNT(*) FROM cron_jobs") as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def close(self) -> None:
        self._running = False
        if self._db:
            await self._db.close()
            self._db = None
