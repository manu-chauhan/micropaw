"""Tests for photos and voice notes: transcription endpoints, vision routing, fallbacks."""

from __future__ import annotations

import httpx
import pytest

from micropaw.config import Config
from micropaw.providers.base import AIResponse, MediaItem
from micropaw.utils import voice
from tests.test_agent import MockProvider, _make_agent

_PNG = MediaItem(type="image", data=b"\x89PNG", mime_type="image/png")
_OGG = MediaItem(type="audio", data=b"OggS", mime_type="audio/ogg")


def test_stt_endpoint_resolution(monkeypatch):
    c = Config()
    assert voice._endpoint(c) is None
    c.voice.stt = "local:whisper-small"
    assert voice._endpoint(c) == ("http://localhost:8000", "", "whisper-small")
    c.voice.stt = "groq:whisper-large-v3-turbo"
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert voice._endpoint(c) is None
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert voice._endpoint(c)[0] == "https://api.groq.com/openai"


@pytest.mark.asyncio
async def test_transcribe_posts_openai_style_form(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"text": " remind me to call mum "})

    real = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    c = Config()
    c.voice.stt = "local:whisper-small"
    assert await voice.transcribe(b"OggS", "audio/ogg", c) == "remind me to call mum"
    assert seen["url"] == "http://localhost:8000/v1/audio/transcriptions"
    assert b"whisper-small" in seen["body"] and b"voice.ogg" in seen["body"]


class _Recorder(MockProvider):
    def __init__(self, reply="ok"):
        super().__init__([AIResponse(content=reply, tool_calls=[], usage={})])
        self.seen = []

    async def chat(self, messages, tools=None):
        self.seen.append(messages)
        return await super().chat(messages, tools)


@pytest.mark.asyncio
async def test_photo_goes_to_main_model_without_vision_model():
    main = _Recorder()
    agent = _make_agent(main)
    await agent.handle_message("what is this?", "u", "terminal", media=[_PNG])
    assert main.seen[0][-1].media == [_PNG]


@pytest.mark.asyncio
async def test_vision_model_describes_photo_for_text_only_main():
    main, eye = _Recorder(), _Recorder("a cat on a sofa")
    agent = _make_agent(main)
    agent.vision_provider = eye
    await agent.handle_message("", "u", "terminal", media=[_PNG])
    last = main.seen[0][-1]
    assert last.media == [] and "a cat on a sofa" in last.content


@pytest.mark.asyncio
async def test_voice_note_without_stt_explains_instead_of_calling_model():
    main = _Recorder()
    reply = await _make_agent(main).handle_message("", "u", "terminal", media=[_OGG])
    assert "speech-to-text" in reply and main.seen == []
