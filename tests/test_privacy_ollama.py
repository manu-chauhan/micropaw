"""An Ollama that serves a cloud model under a local name must not get a chat in local or airgapped mode, and a
server that cannot be read fails closed."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from micropaw import egress, privacy
from micropaw.config import Config, PrivacyConfig
from micropaw.providers.base import Message
from micropaw.providers.ollama import OllamaProvider
from micropaw.providers.registry import Registry

USER = [Message(role="user", content="hi")]


class Fake(BaseHTTPRequestHandler):
    show = "ok"   # ok | remote | fail | missing
    tags = True
    hits: list[str] = []

    def log_message(self, *a):
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path == "/api/tags" and self.tags:
            return self._send(200, {"models": [{"name": "mine"}]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).hits.append(self.path)
        if self.path == "/api/show":
            if self.show == "remote":
                return self._send(200, {"capabilities": ["completion"], "remote_host": "https://ollama.com:443"})
            if self.show == "ok":
                return self._send(200, {"capabilities": ["completion", "tools"]})
            return self._send(500 if self.show == "fail" else 404, {"error": "no"})
        if self.path == "/v1/chat/completions":
            return self._send(200, {"choices": [{"message": {"role": "assistant", "content": "CLOUD ANSWER"},
                                                 "finish_reason": "stop"}], "usage": {}})
        self._send(200, {"message": {"role": "assistant", "content": "CLOUD ANSWER"}})


@pytest.fixture
def server():
    Fake.show, Fake.tags, Fake.hits = "ok", True, []
    httpd = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def strict(name="airgapped"):
    privacy._reset_for_tests()
    privacy.freeze(name)
    privacy.configure(PrivacyConfig(mode=name))


def custom(port: int, kind: str = "openai") -> Config:
    box = {"base_url": f"http://127.0.0.1:{port}/v1", "kind": kind}
    return Config(provider="ollama", models={"fast": "box:mine"}, providers={"box": box},
                  privacy=PrivacyConfig(mode="airgapped"))


@pytest.mark.parametrize("name", ["airgapped", "local"])
@pytest.mark.parametrize("show,expected", [("remote", "runs on ollama.com"), ("fail", "no usable answer"),
                                           ("missing", "no usable answer")])
async def test_native_ollama_refuses_a_remote_or_unreadable_model(server, name, show, expected):
    strict(name)
    Fake.show = show
    p = OllamaProvider(f"http://127.0.0.1:{server}", "mine")
    with pytest.raises(egress.EgressBlocked, match=expected):
        await p.chat(USER)
    assert "/api/chat" not in Fake.hits
    await p.close()


async def test_native_ollama_still_chats_when_show_is_fine_and_normal_mode_asks_nothing(server):
    strict()
    p = OllamaProvider(f"http://127.0.0.1:{server}", "mine")
    assert (await p.chat(USER)).content == "CLOUD ANSWER"
    Fake.show = "fail"
    strict("normal")
    p2 = OllamaProvider(f"http://127.0.0.1:{server}", "mine")
    assert (await p2.chat(USER)).content == "CLOUD ANSWER"
    await p.close()
    await p2.close()


async def test_a_stopped_ollama_gives_the_usual_error_not_a_privacy_refusal():
    strict()
    p = OllamaProvider("http://127.0.0.1:9", "mine")  # nothing listens on the discard port
    assert p.show_state == ""
    with pytest.raises(Exception) as err:
        await p.chat(USER)
    assert not privacy.blocked(err.value)
    assert p.show_state == "down"
    await p.close()


@pytest.mark.parametrize("show,refused", [("remote", True), ("fail", True), ("missing", True), ("ok", False)])
async def test_an_openai_style_address_that_is_an_ollama_is_asked_before_the_first_chat(server, show, refused):
    strict()
    Fake.show = show
    cfg = custom(server)
    provider = Registry(cfg).get("box:mine")
    assert provider is not None  # the name rule alone lets it build: it is the first chat that asks
    if refused:
        with pytest.raises(Exception, match="Blocked by privacy mode: privacy mode blocked the model mine"):
            await provider.chat(USER)
        assert "/v1/chat/completions" not in Fake.hits
    else:
        assert (await provider.chat(USER)).content == "CLOUD ANSWER"
        assert (await provider.chat(USER)).content == "CLOUD ANSWER"
        assert Fake.hits.count("/api/show") == 1  # asked once, then remembered
    await provider.close()


async def test_a_server_that_is_not_an_ollama_is_left_alone(server):
    strict()
    Fake.show, Fake.tags = "missing", False  # a llama.cpp style server: no /api/show, no /api/tags
    provider = Registry(custom(server)).get("box:mine")
    assert (await provider.chat(USER)).content == "CLOUD ANSWER"
    await provider.close()


async def test_start_lists_a_remote_model_behind_either_kind_of_address(server, monkeypatch):
    monkeypatch.setattr(privacy, "ensure_guard", lambda config=None: None)  # never install a real hook here
    strict()
    Fake.show = "remote"
    for kind in ("openai", "ollama"):
        cfg = custom(server, kind)
        found = await privacy.remote_model_refusals(cfg)
        assert len(found) == 1 and "box:mine" in found[0] and "runs on ollama.com" in found[0]
        with pytest.raises(privacy.PrivacyError, match="runs on ollama.com"):
            privacy.enforce(cfg, extra=found)
    Fake.show = "ok"
    assert await privacy.remote_model_refusals(custom(server)) == []
    Fake.show = "fail"
    assert await privacy.remote_model_refusals(custom(server)) == []  # unreadable: refused at the first chat


async def test_start_asks_nothing_in_normal_mode(server):
    strict("normal")
    Fake.show = "remote"
    assert await privacy.remote_model_refusals(custom(server)) == [] and Fake.hits == []
