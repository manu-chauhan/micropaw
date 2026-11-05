"""Link previews are off on every channel, and markdown images never go out."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from micropaw.channels.base import chunk_text, strip_images
from micropaw.channels.discord import DiscordChannel
from micropaw.channels.slack import SlackChannel
from micropaw.channels.telegram import TelegramChannel
from micropaw.channels.whatsapp import WhatsAppChannel

URL = "see https://evil.example/?m=secret"


def test_markdown_images_become_their_alt_text():
    assert strip_images("a ![logo](https://x.example/p.png?d=secret) b") == "a logo b"
    assert strip_images("![](https://x.example/p.png)") == "image"
    assert strip_images("[a link](https://x.example) and plain text") == "[a link](https://x.example) and plain text"


def test_every_chunked_reply_has_its_images_stripped():
    text = "intro ![x](http://e.example/a.png)\n" * 3 + "tail" * 2000
    assert "http://e.example" not in "".join(chunk_text(text, 100))
    assert chunk_text("![alt](http://e.example/a.png)", 4096) == ["alt"]


async def test_telegram_turns_previews_off_and_long_polls_for_50_seconds(monkeypatch):
    built = {}

    class Builder:
        def token(self, t): return self
        def concurrent_updates(self, v): return self

        def defaults(self, d):
            built["defaults"] = d
            return self

        def build(self):
            app = SimpleNamespace(
                add_handler=lambda *a, **k: None, initialize=AsyncMock(), start=AsyncMock(),
                updater=SimpleNamespace(start_polling=AsyncMock()),
                bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="paw"))),
            )
            built["app"] = app
            return app

    telegram = types.ModuleType("telegram")
    telegram.Update = object
    telegram.LinkPreviewOptions = lambda is_disabled: ("preview", is_disabled)
    ext = types.ModuleType("telegram.ext")
    ext.Application = SimpleNamespace(builder=lambda: Builder())
    ext.CallbackQueryHandler = lambda *a, **k: None
    ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    ext.Defaults = lambda link_preview_options: SimpleNamespace(link_preview_options=link_preview_options)
    ext.filters = SimpleNamespace(TEXT=1, PHOTO=2, VOICE=4, AUDIO=8)
    ext.MessageHandler = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "telegram", telegram)
    monkeypatch.setitem(sys.modules, "telegram.ext", ext)
    await TelegramChannel("t").start()
    assert built["defaults"].link_preview_options == ("preview", True)
    kwargs = built["app"].updater.start_polling.await_args.kwargs
    assert kwargs["timeout"] == 50 and kwargs["drop_pending_updates"] is True


async def test_slack_sends_with_unfurling_off():
    ch = SlackChannel("xoxb-1", "xapp-1")
    ch._app = SimpleNamespace(client=SimpleNamespace(chat_postMessage=AsyncMock()))
    await ch.send_message("slack:C1", URL)
    await ch.send_approval("slack:C1", "ask", "ab12", True)
    for call in ch._app.client.chat_postMessage.await_args_list:
        assert call.kwargs["unfurl_links"] is False and call.kwargs["unfurl_media"] is False


async def test_slack_say_and_edits_have_unfurling_off(monkeypatch):
    """The streamed placeholder, its edits and the extra chunks all go through say / chat_update."""
    import inspect

    import micropaw.channels.slack as mod
    source = inspect.getsource(mod)
    assert source.count("_NO_UNFURL") >= 10
    for needle in ("say(text=", "chat_update(", "chat_postMessage("):
        for line in (ln for ln in source.splitlines() if needle in ln):
            assert "_NO_UNFURL" in line or "def " in line or line.rstrip().endswith("("), line


async def test_discord_sends_with_embeds_suppressed(monkeypatch):
    mod = types.ModuleType("discord")
    mod.ButtonStyle = SimpleNamespace(success=1, danger=2, secondary=3)
    mod.ui = SimpleNamespace(View=lambda timeout: SimpleNamespace(add_item=lambda i: None),
                             Button=lambda **k: SimpleNamespace(**k))
    monkeypatch.setitem(sys.modules, "discord", mod)
    ch = DiscordChannel("t")
    channel = SimpleNamespace(send=AsyncMock())
    ch._client = SimpleNamespace(get_channel=lambda _id: channel)
    await ch.send_message("discord:55", URL)
    await ch.send_approval("discord:55", "ask", "ab12", True)
    assert all(c.kwargs["suppress_embeds"] is True for c in channel.send.await_args_list)


async def test_whatsapp_asks_for_no_preview(monkeypatch):
    seen = {}

    def handler(request):
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    real = httpx.AsyncClient.post

    async def post(self, url, **kw):
        seen["body"] = kw["json"]
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    await WhatsAppChannel(token="t", phone_id="p", app_secret="s")._send("123", URL)
    assert seen["body"]["text"] == {"body": URL, "preview_url": False}
    assert real is not None


async def test_a_reply_with_an_image_is_sent_without_it():
    ch = SlackChannel("xoxb-1", "xapp-1")
    ch._app = SimpleNamespace(client=SimpleNamespace(chat_postMessage=AsyncMock()))
    await ch.send_message("slack:C1", "result ![chart](https://evil.example/?d=secret)")
    assert ch._app.client.chat_postMessage.await_args.kwargs["text"] == "result chart"


@pytest.mark.parametrize("name", ["telegram", "slack", "discord", "whatsapp"])
def test_vendor_channels_route_replies_through_the_chunker(name):
    import importlib
    import inspect
    assert "chunk_text(" in inspect.getsource(importlib.import_module(f"micropaw.channels.{name}"))
