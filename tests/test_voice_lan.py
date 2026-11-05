"""Speech servers on another computer: a named provider entry, its key, a short connect timeout, a chat note."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from micropaw.config import Config
from micropaw.core.profile import AgentProfile, agent_config
from micropaw.utils import voice


class Speech(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        type(self).seen.append((self.path, self.headers.get("Authorization", ""), body))
        if self.path.endswith("/audio/transcriptions"):
            out, kind = json.dumps({"text": " hello there "}).encode(), "application/json"
        else:
            out, kind = b"OggS\x00audio", "audio/ogg"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def speech():
    Speech.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Speech)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def cfg(url, key_env="", **voice_kw) -> Config:
    c = Config(providers={"desktop": {"base_url": f"{url}/v1", "api_key_env": key_env}})
    for k, v in voice_kw.items():
        setattr(c.voice, k, v)
    return c


async def test_a_named_provider_is_the_speech_server_and_its_key_is_sent(speech, monkeypatch):
    monkeypatch.setenv("DESK_KEY", "k-1")
    c = cfg(speech, "DESK_KEY", stt="desktop:whisper-large-v3", tts="desktop:kokoro")
    assert await voice.transcribe(b"audio", "audio/ogg", c) == "hello there"
    assert (await voice.synthesize("hi", c))[1] == "audio/ogg"
    paths = [(p, h) for p, h, _ in Speech.seen]
    assert paths == [("/v1/audio/transcriptions", "Bearer k-1"), ("/v1/audio/speech", "Bearer k-1")]
    assert b"whisper-large-v3" in Speech.seen[0][2]


async def test_a_provider_with_a_missing_key_is_not_a_speech_server(speech, monkeypatch):
    monkeypatch.delenv("DESK_KEY", raising=False)
    c = cfg(speech, "DESK_KEY", stt="desktop:m")
    assert voice._endpoint(c) is None and await voice.transcribe(b"a", "audio/ogg", c) is None


async def test_a_closed_port_gives_the_chat_note_within_about_five_seconds():
    c = cfg("http://127.0.0.1:1", stt="desktop:m")
    start = time.monotonic()
    assert await voice.transcribe(b"a", "audio/ogg", c) is None
    assert time.monotonic() - start < 6
    assert voice.unheard_note(c) == ("I could not reach the speech server at 127.0.0.1:1, "
                                     "so I could not hear that voice note.")


def test_the_note_without_a_speech_server_says_it_is_not_set_up():
    assert "isn't set up" in voice.unheard_note(Config())


def test_the_connect_timeout_is_five_seconds_and_the_read_timeout_stays_long():
    assert voice.SPEECH_TIMEOUT.connect == 5.0 and voice.SPEECH_TIMEOUT.read == 120.0


def _private(c: Config) -> Config:
    from unittest.mock import MagicMock
    return agent_config(c, AgentProfile(name="p", main="ollama:x", private=True), MagicMock())


def test_a_private_agent_keeps_a_tailnet_speech_box_and_drops_a_public_one():
    kept = _private(cfg("http://100.101.102.103:8000", stt="desktop:m", tts="desktop:m"))
    assert kept.voice.stt == "desktop:m" and kept.voice.tts == "desktop:m"
    dropped = _private(cfg("http://203.0.113.5:8000", stt="desktop:m", tts="desktop:m"))
    assert dropped.voice.stt == "" and dropped.voice.tts == ""
