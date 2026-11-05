"""Approval buttons, Discord and Slack media, voice replies. Channel libraries are faked."""

from __future__ import annotations

import asyncio
import sys
import types
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from micropaw.channels import media as media_mod
from micropaw.channels.base import Channel, approval_buttons, approval_hint
from micropaw.channels.discord import DiscordChannel
from micropaw.channels.media import Attachment, download, fetch_media
from micropaw.channels.slack import SlackChannel
from micropaw.channels.telegram import TelegramChannel, _download_media
from micropaw.config import Config
from micropaw.core.profile import build_runtimes
from micropaw.gateway import Gateway
from micropaw.providers.base import MediaItem
from micropaw.providers.registry import Registry
from micropaw.utils import voice

pytestmark = pytest.mark.asyncio
_REAL_CLIENT = httpx.AsyncClient  # the patched one replaces httpx.AsyncClient for every module


def _gateway(owner: str = "42") -> Gateway:
    c = Config()
    for platform in ("telegram", "discord", "slack"):
        getattr(c.channels, platform).allowed_users = [owner]
    gw = Gateway(c, MagicMock(), MagicMock(), [])
    gw.agent = MagicMock()
    gw.agent.conversations.get_conversation_id = lambda platform, user, channel: f"{platform}:{channel or 'dm'}"
    gw.agent.config = c
    return gw


class _Plain(Channel):
    name = "telegram"

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, str]] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


def _wired(gw: Gateway, channel: Channel) -> Channel:
    channel._approval_answer = partial(gw._approval_answer_for, channel.name)
    return channel


async def _ask(gw: Gateway, channel: Channel, chat: str = "telegram:42", marked: bool = False):
    """Start an approval on the channel; returns (task, ask_id)."""
    sent: dict = {}

    async def send(text, ask_id, always):
        sent.update(text=text, id=ask_id, always=always)

    task = asyncio.create_task(gw._approvals.ask(send, chat, "shell", "Run: ls", marked))
    await asyncio.sleep(0)
    return task, sent


# --- approval text and buttons ---

async def test_base_channel_sends_the_text_with_the_reply_hint():
    ch = _Plain()
    await ch.send_approval("telegram:1", "Approval needed [ab12]: shell\n\nRun: ls", "ab12", True)
    text = ch.sent[0][1]
    assert text.startswith("Approval needed [ab12]: shell")
    assert "Reply 'yes ab12' or 'no ab12', or 'always ab12' to allow this exact action from now on." in text
    assert text.endswith("This expires in 10 minutes.")
    assert "always" not in approval_hint("ab12", False)


async def test_always_button_only_when_allowed():
    assert [d for _, d in approval_buttons("ab12", True)] == ["ap:ab12:yes", "ap:ab12:no", "ap:ab12:always"]
    assert [d for _, d in approval_buttons("ab12", False)] == ["ap:ab12:yes", "ap:ab12:no"]


async def test_telegram_keyboard_hides_always(monkeypatch):
    mod = types.ModuleType("telegram")
    mod.InlineKeyboardButton = lambda label, callback_data: (label, callback_data)
    mod.InlineKeyboardMarkup = lambda rows: rows
    monkeypatch.setitem(sys.modules, "telegram", mod)
    ch = TelegramChannel("t")
    ch._app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await ch.send_approval("telegram:7", "ask", "ab12", False)
    kw = ch._app.bot.send_message.await_args.kwargs
    assert kw["chat_id"] == 7 and kw["text"] == "ask"
    assert kw["reply_markup"] == [[("Yes", "ap:ab12:yes"), ("No", "ap:ab12:no")]]
    await ch.send_approval("telegram:7", "ask", "ab12", True)
    assert ch._app.bot.send_message.await_args.kwargs["reply_markup"][0][-1] == ("Always", "ap:ab12:always")


async def test_telegram_long_ask_is_whole_and_keyboard_is_on_the_last_part(monkeypatch):
    mod = types.ModuleType("telegram")
    mod.InlineKeyboardButton = lambda label, callback_data: (label, callback_data)
    mod.InlineKeyboardMarkup = lambda rows: rows
    monkeypatch.setitem(sys.modules, "telegram", mod)
    ch = TelegramChannel("t")
    ch._app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    body = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
    await ch.send_approval("telegram:7", body, "ab12", False)
    calls = [c.kwargs for c in ch._app.bot.send_message.await_args_list]
    assert len(calls) > 1 and "reply_markup" not in calls[0] and "reply_markup" in calls[-1]
    assert "\n".join(c["text"] for c in calls) == body


async def test_slack_blocks_hide_always():
    ch = SlackChannel("xoxb-1", "xapp-1")
    ch._app = SimpleNamespace(client=SimpleNamespace(chat_postMessage=AsyncMock()))
    await ch.send_approval("slack:C1", "ask", "ab12", False)
    kw = ch._app.client.chat_postMessage.await_args.kwargs
    assert kw["channel"] == "C1" and kw["blocks"][0]["text"]["type"] == "plain_text"
    assert [e["action_id"] for e in kw["blocks"][1]["elements"]] == ["ap:ab12:yes", "ap:ab12:no"]
    await ch.send_approval("slack:C1", "ask", "ab12", True)
    elements = ch._app.client.chat_postMessage.await_args.kwargs["blocks"][1]["elements"]
    assert elements[-1]["action_id"] == "ap:ab12:always"


async def test_discord_view_hides_always(monkeypatch):
    class Button:
        def __init__(self, label, custom_id, style):
            self.label, self.custom_id, self.style, self.callback = label, custom_id, style, None

    class View:
        def __init__(self, timeout):
            self.timeout, self.children = timeout, []

        def add_item(self, item):
            self.children.append(item)

    mod = types.ModuleType("discord")
    mod.ButtonStyle = SimpleNamespace(success=1, danger=2, secondary=3)
    mod.ui = SimpleNamespace(View=View, Button=Button)
    monkeypatch.setitem(sys.modules, "discord", mod)
    ch = DiscordChannel("t")
    channel = SimpleNamespace(send=AsyncMock())
    ch._client = SimpleNamespace(get_channel=lambda _id: channel)
    await ch.send_approval("discord:55", "ask", "ab12", False)
    view = channel.send.await_args.kwargs["view"]
    assert view.timeout == 600 and [b.custom_id for b in view.children] == ["ap:ab12:yes", "ap:ab12:no"]
    await ch.send_approval("discord:55", "ask", "ab12", True)
    assert channel.send.await_args.kwargs["view"].children[-1].custom_id == "ap:ab12:always"


# --- button presses: owner, chat binding, single use, lock ---

async def test_owner_press_approves_and_edits_the_message():
    gw = _gateway()
    ch = _wired(gw, TelegramChannel("t"))
    task, sent = await _ask(gw, ch)
    q = SimpleNamespace(
        data=f"ap:{sent['id']}:yes", from_user=SimpleNamespace(id=42),
        answer=AsyncMock(), edit_message_text=AsyncMock(),
        message=SimpleNamespace(text="Approval needed", chat=SimpleNamespace(id=42, type="private")),
    )
    await ch._on_press(SimpleNamespace(callback_query=q), None)
    assert await task is True
    q.answer.assert_awaited_once_with("Approved.")
    assert q.edit_message_text.await_args.args[0] == "Approval needed\n\nApproved."


async def test_non_owner_press_is_refused_and_ask_stays_open():
    gw = _gateway()
    ch = _wired(gw, TelegramChannel("t"))
    task, sent = await _ask(gw, ch, chat="telegram:-100")
    q = SimpleNamespace(
        data=f"ap:{sent['id']}:yes", from_user=SimpleNamespace(id=99),
        answer=AsyncMock(), edit_message_text=AsyncMock(),
        message=SimpleNamespace(text="x", chat=SimpleNamespace(id=-100, type="supergroup")),
    )
    await ch._on_press(SimpleNamespace(callback_query=q), None)
    assert q.answer.await_args.kwargs.get("show_alert") is True and "owner" in q.answer.await_args.args[0]
    q.edit_message_text.assert_not_awaited()
    await asyncio.sleep(0)
    assert not task.done()
    # the owner pressing in the group does bind to the group chat
    assert ch.press_approval(f"ap:{sent['id']}:no", "42", "-100") == "Declined."
    assert await task is False


async def test_press_from_another_chat_and_reused_id_do_nothing():
    gw = _gateway()
    ch = _wired(gw, _Plain())
    task, sent = await _ask(gw, ch, chat="telegram:42")
    data = f"ap:{sent['id']}:yes"
    assert "No pending approval" in ch.press_approval(data, "42", "-555")  # owner, wrong chat
    await asyncio.sleep(0)
    assert not task.done()
    assert ch.press_approval(data, "42", None) == "Approved."
    assert await task is True
    assert "No pending approval" in ch.press_approval(data, "42", None)  # single use
    assert ch.press_approval("ap:zzzz:yes", "42", None) is None  # not our button format
    assert ch.press_approval("hello", "42", None) is None


async def test_always_is_refused_when_the_ask_has_no_rule():
    gw = _gateway()
    ch = _wired(gw, _Plain())
    task, sent = await _ask(gw, ch, marked=True)  # outside content was read: no saved rules
    reply = ch.press_approval(f"ap:{sent['id']}:always", "42", None)
    assert reply.startswith("'always'")
    ch.press_approval(f"ap:{sent['id']}:no", "42", None)
    assert await task is False


async def test_press_is_answered_while_the_chat_lock_is_held():
    gw = _gateway()
    ch = _wired(gw, _Plain())
    gw.channels = [ch]
    release = asyncio.Event()
    seen: dict = {}

    async def turn(text, user_id, platform, channel_id, media=None):
        # the agent waits for the owner while it holds the per-chat lock
        seen["ok"] = await gw._approve(platform, f"{platform}:{user_id}", "shell", "Run: ls", False)
        await release.wait()
        return "done"

    gw.agent.handle_message = turn
    run = asyncio.create_task(gw._handle_message("do it", "42", "telegram", None))
    await asyncio.sleep(0.05)
    assert gw._chat_lock("telegram", "42", None).locked()
    ask_id = next(iter(gw._approvals._pending))
    assert ch.press_approval(f"ap:{ask_id}:yes", "42", None) == "Approved."  # no wait on the lock
    await asyncio.sleep(0.05)
    assert seen["ok"] is True
    release.set()
    assert await run == "done"
    assert "Reply 'yes" in ch.sent[0][1]  # a plain channel still gets the typed-reply hint


async def test_discord_press_checks_owner_and_binds_dm_to_user():
    gw = _gateway()
    ch = _wired(gw, DiscordChannel("t"))
    ch.name = "discord"
    task, sent = await _ask(gw, ch, chat="discord:42")

    def press(user: int, guild):
        resp = SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock())
        return SimpleNamespace(
            data={"custom_id": f"ap:{sent['id']}:yes"}, user=SimpleNamespace(id=user), guild=guild, channel_id=777,
            message=SimpleNamespace(content="ask"), response=resp,
        )

    stranger = press(99, None)
    await ch._on_press(stranger)
    assert stranger.response.send_message.await_args.kwargs == {"ephemeral": True}
    await asyncio.sleep(0)
    assert not task.done()
    owner = press(42, None)  # a DM: no guild, bound to the owner's user id like a typed reply
    await ch._on_press(owner)
    owner.response.edit_message.assert_awaited_once_with(content="ask\n\nApproved.", view=None)
    assert await task is True


async def test_slack_press_checks_owner():
    gw = _gateway()
    ch = _wired(gw, SlackChannel("xoxb-1", "xapp-1"))
    ch.name = "slack"
    task, sent = await _ask(gw, ch, chat="slack:C9")

    def body(user: str):
        return {"user": {"id": user}, "channel": {"id": "C9"}, "message": {"text": "ask", "ts": "1.2"},
                "actions": [{"action_id": f"ap:{sent['id']}:no"}]}

    client = SimpleNamespace(chat_postEphemeral=AsyncMock(), chat_update=AsyncMock())
    ack = AsyncMock()
    await ch._on_press(ack, body("U_other"), client)
    ack.assert_awaited_once()
    assert "owner" in client.chat_postEphemeral.await_args.kwargs["text"] and not client.chat_update.await_count
    await asyncio.sleep(0)
    assert not task.done()
    await ch._on_press(ack, body("42"), client)
    assert client.chat_update.await_args.kwargs["text"] == "ask\n\nDeclined."
    assert await task is False


# --- Discord and Slack media ---

def _http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(media_mod.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=transport, **kw))

    async def fine(url):
        return None

    monkeypatch.setattr(media_mod, "validate_url", fine)


class _Counting(httpx.AsyncByteStream):
    def __init__(self, chunks: int, size: int) -> None:
        self.chunks, self.size, self.read = chunks, size, 0

    async def __aiter__(self):
        for _ in range(self.chunks):
            self.read += 1
            yield b"x" * self.size


async def test_download_stops_at_the_cap_while_streaming(monkeypatch):
    stream = _Counting(chunks=1000, size=10)
    _http(monkeypatch, lambda req: httpx.Response(200, stream=stream))  # no content-length header
    with pytest.raises(media_mod.MediaRefusedError, match="too large"):
        await download("https://cdn.example.com/a.png", limit=100)
    assert stream.read < 20


async def test_declared_size_over_the_cap_is_refused_before_reading(monkeypatch):
    stream = _Counting(chunks=5, size=10)
    _http(monkeypatch, lambda req: httpx.Response(200, headers={"content-length": "999"}, stream=stream))
    with pytest.raises(media_mod.MediaRefusedError):
        await download("https://cdn.example.com/a.png", limit=100)
    assert stream.read == 0


async def test_redirect_to_another_host_is_refused_and_token_stays_home(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})

    _http(monkeypatch, handler)
    with pytest.raises(media_mod.MediaRefusedError, match="another host"):
        await download("https://files.slack.com/x", {"Authorization": "Bearer xoxb-secret"})
    assert [r.url.host for r in seen] == ["files.slack.com"]


async def test_same_host_redirect_is_followed_with_the_token(monkeypatch):
    def handler(req):
        if req.url.path == "/x":
            return httpx.Response(302, headers={"location": "/y"})
        assert req.headers["authorization"] == "Bearer xoxb-secret"
        return httpx.Response(200, content=b"png")

    _http(monkeypatch, handler)
    assert await download("https://files.slack.com/x", {"Authorization": "Bearer xoxb-secret"}) == b"png"


async def test_fetch_media_takes_images_and_audio_only(monkeypatch):
    _http(monkeypatch, lambda req: httpx.Response(200, content=b"data"))
    files = [
        Attachment("https://cdn.example.com/a.png", "image/png", 4, "a.png", False),
        Attachment("https://cdn.example.com/b.ogg", "audio/ogg; codecs=opus", 4, "b.ogg", False),
        Attachment("https://cdn.example.com/c.pdf", "application/pdf", 4, "c.pdf", False),
        Attachment("https://cdn.example.com/d.png", "image/png", 50, "d.png", False),
    ]
    items, notes = await fetch_media(files, limit=10)
    assert [(m.type, m.mime_type) for m in items] == [("image", "image/png"), ("audio", "audio/ogg")]
    assert any("c.pdf" in n and "images and audio" in n for n in notes)
    assert any("d.png" in n and "too large" in n for n in notes)


async def test_slack_token_only_goes_to_slack_hosts(monkeypatch):
    seen: list[str] = []

    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(200, content=b"png")

    _http(monkeypatch, handler)
    files = [Attachment("https://files.slack.com/f", "image/png", 3, "ok.png", False),
             Attachment("https://evil.example.com/f", "image/png", 3, "bad.png", False)]
    items, notes = await fetch_media(files, token="xoxb-1", token_host="slack.com")
    assert len(items) == 1 and seen == ["files.slack.com"] and "bad.png" in notes[0]


async def test_slack_files_from_other_people_are_outside_content(monkeypatch):
    _http(monkeypatch, lambda req: httpx.Response(200, content=b"png"))
    ch = SlackChannel("xoxb-1", "xapp-1")
    say = AsyncMock()
    event = {"files": [
        {"url_private": "https://files.slack.com/own", "mimetype": "image/png", "size": 3, "name": "own.png",
         "user": "42"},
        {"url_private": "https://files.slack.com/theirs", "mimetype": "image/png", "size": 3, "name": "t.png",
         "user": "U_other"},
    ]}
    media = await ch._read_files(event, "42", say, "1.0")
    assert [m.outside for m in media] == [False, True]


async def test_discord_forwarded_files_are_outside_content(monkeypatch):
    _http(monkeypatch, lambda req: httpx.Response(200, content=b"png"))
    ch = DiscordChannel("t")

    def att(name):
        url = f"https://cdn.discordapp.com/{name}"
        return SimpleNamespace(url=url, content_type="image/png", size=3, filename=name)

    message = SimpleNamespace(
        attachments=[att("own.png")], message_snapshots=[SimpleNamespace(attachments=[att("fwd.png")])],
        channel=SimpleNamespace(send=AsyncMock()),
    )
    media = await ch._read_attachments(message)
    assert [m.outside for m in media] == [False, True]


async def test_telegram_forwarded_photo_is_outside_content():
    async def get_file():
        return SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"jpg")))

    def msg(forward):
        photo = [SimpleNamespace(get_file=get_file)]
        return SimpleNamespace(photo=photo, voice=None, audio=None, forward_origin=forward)

    assert (await _download_media(msg(None)))[0].outside is False
    assert (await _download_media(msg(object())))[0].outside is True


async def test_outside_media_marks_the_conversation():
    gw = _gateway()
    gw.agent.store.set_marked = AsyncMock()
    gw.agent.handle_message = AsyncMock(return_value="ok")
    own = MediaItem(type="image", data=b"x", mime_type="image/png")
    await gw._handle_message("look", "42", "discord", None, media=[own])
    gw.agent.store.set_marked.assert_not_awaited()
    theirs = MediaItem(type="image", data=b"x", mime_type="image/png", outside=True)
    await gw._handle_message("look", "42", "discord", "chan", media=[own, theirs])
    gw.agent.store.set_marked.assert_awaited_once_with("discord:chan")


async def test_outside_media_marks_on_the_stream_path_too():
    gw = _gateway()
    gw.agent.store.set_marked = AsyncMock()
    gw.agent.handle_message_stream = AsyncMock(return_value="ok")
    theirs = MediaItem(type="image", data=b"x", mime_type="image/png", outside=True)
    await gw._handle_message_stream("look", "42", "slack", None, media=[theirs])
    gw.agent.store.set_marked.assert_awaited_once_with("slack:dm")


# --- voice replies ---

def _tts_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=transport, **kw))


def _tts_config(spec="local:kokoro", **kw) -> Config:
    c = Config()
    c.voice.tts = spec
    for k, v in kw.items():
        setattr(c.voice, k, v)
    return c


async def test_tts_asks_for_opus_and_returns_ogg(monkeypatch):
    seen = {}

    def handler(req):
        import json
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, content=b"OggS\x00data")

    _tts_client(monkeypatch, handler)
    out = await voice.synthesize("hi " * 3000, _tts_config(tts_url="http://localhost:8880"))
    assert out == (b"OggS\x00data", "audio/ogg")
    assert seen["url"] == "http://localhost:8880/v1/audio/speech"
    assert seen["body"]["response_format"] == "opus" and seen["body"]["model"] == "kokoro"
    assert len(seen["body"]["input"]) == 4000 and seen["body"]["voice"] == "af_heart"


async def test_tts_openai_uses_the_key_and_default_voice(monkeypatch):
    seen = {}

    def handler(req):
        seen["auth"], seen["host"] = req.headers.get("authorization"), req.url.host
        return httpx.Response(200, content=b"OggS")

    _tts_client(monkeypatch, handler)
    c = _tts_config("openai:gpt-4o-mini-tts")
    assert await voice.synthesize("hi", c) is None  # no key yet
    c.provider_config.openai.api_key = "sk-1"
    assert await voice.synthesize("hi", c) is not None
    assert seen == {"auth": "Bearer sk-1", "host": "api.openai.com"}


async def test_tts_mp3_is_reported_as_mpeg(monkeypatch):
    _tts_client(monkeypatch, lambda req: httpx.Response(200, content=b"ID3\x04rest"))
    assert (await voice.synthesize("hi", _tts_config()))[1] == "audio/mpeg"
    _tts_client(monkeypatch, lambda req: httpx.Response(200, content=b"\xff\xfb\x90rest"))
    assert (await voice.synthesize("hi", _tts_config()))[1] == "audio/mpeg"


async def test_tts_errors_give_none(monkeypatch):
    _tts_client(monkeypatch, lambda req: httpx.Response(500))
    assert await voice.synthesize("hi", _tts_config()) is None
    _tts_client(monkeypatch, lambda req: httpx.Response(200, content=b"RIFFwav"))
    assert await voice.synthesize("hi", _tts_config()) is None

    def boom(req):
        raise httpx.ConnectError("down")

    _tts_client(monkeypatch, boom)
    assert await voice.synthesize("hi", _tts_config()) is None
    assert await voice.synthesize("hi", Config()) is None  # not set up


def _telegram_with_bot(monkeypatch, spoken):
    mod = types.ModuleType("telegram")
    mod.InputFile = lambda data, filename: (filename, data)
    monkeypatch.setitem(sys.modules, "telegram", mod)
    ch = TelegramChannel("t")
    ch._app = SimpleNamespace(bot=SimpleNamespace(send_voice=AsyncMock(), send_audio=AsyncMock()))
    ch._voice_say = AsyncMock(return_value=spoken)
    return ch


async def test_telegram_sends_opus_as_a_voice_note(monkeypatch):
    ch = _telegram_with_bot(monkeypatch, (b"OggS", "audio/ogg"))
    assert await ch._send_voice(5, "hello", "42", None, None) is True
    assert ch._app.bot.send_voice.await_args.kwargs["voice"] == ("reply.ogg", b"OggS")
    ch._app.bot.send_audio.assert_not_awaited()


async def test_telegram_sends_mp3_as_audio(monkeypatch):
    ch = _telegram_with_bot(monkeypatch, (b"ID3", "audio/mpeg"))
    assert await ch._send_voice(5, "hello", "42", None, 9) is True
    assert ch._app.bot.send_audio.await_args.kwargs["audio"] == ("reply.mp3", b"ID3")
    ch._app.bot.send_voice.assert_not_awaited()


async def test_telegram_falls_back_to_text_when_speech_fails(monkeypatch):
    ch = _telegram_with_bot(monkeypatch, None)  # synthesis failed
    assert await ch._send_voice(5, "hello", "42", None, None) is False
    ch = _telegram_with_bot(monkeypatch, (b"OggS", "audio/ogg"))
    ch._app.bot.send_voice.side_effect = RuntimeError("upload failed")
    assert await ch._send_voice(5, "hello", "42", None, None) is False
    ch._voice_say = None
    assert await ch._send_voice(5, "hello", "42", None, None) is False


async def test_voice_replies_need_both_settings_and_default_off():
    assert Config().voice.reply_with_voice is False
    gw = _gateway()
    assert gw._voice_on("telegram", "42", None) is False
    gw.agent.config.voice.tts = "local:kokoro"
    assert gw._voice_on("telegram", "42", None) is False
    gw.agent.config.voice.reply_with_voice = True
    assert gw._voice_on("telegram", "42", None) is True


async def test_private_agent_skips_cloud_tts_but_keeps_local():
    def runtimes(spec, **kw):
        c = Config(agents={"family": {"model": "ollama:qwen3.5:9b"}}, voice={"tts": spec, **kw})
        c.provider_config.claude.api_key = "sk-test"
        c.provider_config.openai.api_key = "k"
        return build_runtimes(c, Registry(c))

    rt = runtimes("openai:gpt-4o-mini-tts")
    assert rt["family"].config.voice.tts == "" and rt["main"].config.voice.tts == "openai:gpt-4o-mini-tts"
    assert runtimes("local:kokoro")["family"].config.voice.tts == "local:kokoro"
    far = runtimes("local:kokoro", tts_url="https://tts.example.com")
    assert far["family"].config.voice.tts == ""
