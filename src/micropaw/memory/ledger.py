"""Usage ledger: one row per model call in data.db, plus today's totals and in-flight holds."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import aiosqlite

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS usage (
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    chat TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'main',
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    usd REAL
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
"""


@dataclass(slots=True)
class Totals:
    usd: float = 0.0
    tokens: int = 0
    unpriced_tokens: int = 0  # tokens of non-local models with no known price
    calls: int = 0


def day_start(days_ago: int = 0) -> float:
    """Epoch seconds of local midnight, `days_ago` days back."""
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - timedelta(days=days_ago)).timestamp()


class Ledger:
    """Takes the store's aiosqlite connection. Holds are in-memory and shared by every meter on this ledger."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db
        self.lock = asyncio.Lock()  # makes check + hold atomic
        self._holds: dict[int, tuple[str, float, int]] = {}
        self._next_hold = 0
        self._warned: set[tuple[float, str]] = set()
        self.warning: str | None = None  # one-line note for the next reply

    async def init(self) -> None:
        await self.db.executescript(_SCHEMA)
        await self.db.commit()

    async def record(
        self, *, agent: str, chat: str, role: str, provider: str, model: str,
        input: int, cached: int, output: int, usd: float | None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO usage (ts, agent, chat, role, provider, model, input, cached, output, usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), agent, chat, role, provider, model, input, cached, output, usd),
        )
        await self.db.commit()

    async def today(self) -> dict[str, Totals]:
        """Today's totals by agent (local day)."""
        out: dict[str, Totals] = {}
        async with self.db.execute(
            "SELECT agent, COALESCE(SUM(usd), 0), SUM(input + output), "
            "SUM(CASE WHEN usd IS NULL THEN input + output ELSE 0 END), COUNT(*) "
            "FROM usage WHERE ts >= ? GROUP BY agent",
            (day_start(),),
        ) as cur:
            for agent, usd, tokens, unpriced, calls in await cur.fetchall():
                out[agent] = Totals(usd, tokens or 0, unpriced or 0, calls)
        return out

    async def breakdown(self, days: int = 1) -> list[tuple[str, str, str, Totals]]:
        """(local day, agent, model, totals) for the last `days` days, newest day first."""
        async with self.db.execute(
            "SELECT date(ts, 'unixepoch', 'localtime'), agent, model, COALESCE(SUM(usd), 0), SUM(input + output), "
            "SUM(CASE WHEN usd IS NULL THEN input + output ELSE 0 END), COUNT(*) "
            "FROM usage WHERE ts >= ? GROUP BY 1, agent, model ORDER BY 1 DESC, 4 DESC",
            (day_start(max(days, 1) - 1),),
        ) as cur:
            rows = await cur.fetchall()
        return [(d, a, m, Totals(u, t or 0, up or 0, c)) for d, a, m, u, t, up, c in rows]

    # --- in-flight reservations (estimates held until the call settles) ---

    def hold(self, agent: str, usd: float, tokens: int) -> int:
        self._next_hold += 1
        self._holds[self._next_hold] = (agent, usd, tokens)
        return self._next_hold

    def release(self, hold_id: int) -> None:
        self._holds.pop(hold_id, None)

    def held(self, agent: str | None = None) -> tuple[float, int]:
        rows = [h for h in self._holds.values() if agent is None or h[0] == agent]
        return sum(h[1] for h in rows), sum(h[2] for h in rows)

    # --- warning note ---

    def warn_once(self, scope: str, note: str) -> None:
        """Queue `note` for the next reply, the first time `scope` crosses its warning line today."""
        key = (day_start(), scope)
        if key not in self._warned:
            self._warned.add(key)
            self.warning = note

    def take_warning(self) -> str | None:
        note, self.warning = self.warning, None
        return note
