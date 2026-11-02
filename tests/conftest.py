"""Shared fixtures."""

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _event_loop_present():
    # Sync tests call asyncio.get_event_loop(); earlier asyncio.run() calls leave none set.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield
