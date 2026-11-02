"""Tests for the WhatsApp webhook: fast ack, background work, de-duplication."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import types
from unittest.mock import AsyncMock

import pytest

from micropaw.channels.whatsapp import SEEN_IDS, WhatsAppChannel

_SECRET = "app-secret"


class _Request:
    """A webhook POST signed the way Meta signs them."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()
        sig = hmac.new(_SECRET.encode(), self._body, hashlib.sha256).hexdigest()
        self.headers: dict = {"X-Hub-Signature-256": f"sha256={sig}"}

    async def read(self) -> bytes:
        return self._body


def _payload(*ids: str) -> dict:
    msgs = [{"id": i, "type": "text", "from": "123", "text": {"body": f"hi {i}"}} for i in ids]
    return {"entry": [{"changes": [{"value": {"messages": msgs}}]}]}


@pytest.fixture(autouse=True)
def _fake_aiohttp(monkeypatch):
    # aiohttp is an optional extra; the handler only needs web.Response
    web = types.SimpleNamespace(Response=lambda text="", status=200: types.SimpleNamespace(text=text, status=status))
    mod = types.ModuleType("aiohttp")
    mod.web = web
    monkeypatch.setitem(sys.modules, "aiohttp", mod)


def _channel(handler) -> WhatsAppChannel:
    ch = WhatsAppChannel(token="t", phone_id="p", app_secret=_SECRET)
    ch._handler = handler
    ch.set_access(lambda platform, user_id: None)  # sender is the owner
    ch._send = AsyncMock()
    return ch


@pytest.mark.asyncio
async def test_returns_before_handler_finishes():
    gate = asyncio.Event()

    async def slow(text, sender, channel, extra):
        await gate.wait()
        return "done"

    ch = _channel(slow)
    resp = await asyncio.wait_for(ch._incoming(_Request(_payload("m1"))), timeout=1)
    assert resp.status == 200
    ch._send.assert_not_called()
    gate.set()
    await asyncio.gather(*ch._tasks)
    ch._send.assert_awaited_once_with("123", "done")


@pytest.mark.asyncio
async def test_retried_message_processed_once():
    handler = AsyncMock(return_value="ok")
    ch = _channel(handler)
    await ch._incoming(_Request(_payload("m1")))
    await ch._incoming(_Request(_payload("m1")))
    await asyncio.gather(*ch._tasks)
    assert handler.await_count == 1


def test_seen_ids_bounded():
    ch = WhatsAppChannel(token="t", phone_id="p")
    for i in range(SEEN_IDS + 50):
        ch._is_duplicate(f"id{i}")
    assert len(ch._seen) == SEEN_IDS
    assert not ch._is_duplicate("id0")  # evicted


@pytest.mark.asyncio
async def test_unsigned_request_rejected():
    ch = _channel(AsyncMock(return_value="ok"))
    req = _Request(_payload("m9"))
    req.headers = {}
    assert (await ch._incoming(req)).status == 401


@pytest.mark.asyncio
async def test_stranger_never_reaches_handler():
    handler = AsyncMock(return_value="ok")
    ch = _channel(handler)
    ch.set_access(lambda platform, user_id: "This assistant is private.")
    await ch._incoming(_Request(_payload("m10")))
    await asyncio.gather(*ch._tasks)
    handler.assert_not_called()
    ch._send.assert_awaited_once_with("123", "This assistant is private.")
