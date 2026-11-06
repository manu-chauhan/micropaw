"""The CLI helper that opens and closes a store or scheduler."""

from __future__ import annotations

import pytest

from micropaw.cli import _opened


class _Resource:
    def __init__(self, fail_init=False):
        self.calls = []
        self.fail_init = fail_init

    async def init(self):
        self.calls.append("init")
        if self.fail_init:
            raise RuntimeError("no db")

    async def close(self):
        self.calls.append("close")


async def test_opened_inits_then_closes():
    r = _Resource()
    async with _opened(r) as got:
        assert got is r
        assert r.calls == ["init"]
    assert r.calls == ["init", "close"]


async def test_opened_closes_when_the_block_raises():
    r = _Resource()
    with pytest.raises(ValueError):
        async with _opened(r):
            raise ValueError("boom")
    assert r.calls == ["init", "close"]


async def test_opened_does_not_close_what_failed_to_init():
    r = _Resource(fail_init=True)
    with pytest.raises(RuntimeError):
        async with _opened(r):
            pass
    assert r.calls == ["init"]
