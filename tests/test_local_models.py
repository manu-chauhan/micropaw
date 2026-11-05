"""Local model care: scan, /api/show capabilities, num_ctx by RAM, size-aware picks, Ollama body."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from micropaw.providers import ollama
from micropaw.providers.base import Message, ToolDef
from micropaw.providers.ollama import OllamaProvider, ShowInfo, default_num_ctx, parse_show
from micropaw.providers.registry import (
    LocalModel,
    pick_local,
    scan_local,
    suggest_pull,
)

GB = 10**9
GIB = 2**30

SHOW = {
    "big": {"capabilities": ["completion", "tools"], "model_info": {"llama.context_length": 131072}},
    "small": {"capabilities": ["completion", "tools", "vision"]},
    "plain": {"capabilities": ["completion"]},
    "emb": {"capabilities": ["embedding"]},
    "old": {"details": {}},  # an Ollama that does not list capabilities
}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(200, {"models": [{"name": n, "size": s} for n, s in
                                        (("big", 9 * GB), ("small", 3 * GB), ("plain", GB), ("emb", GB // 2))]})
        elif self.path == "/v1/models":
            self._send(200, {"data": [{"id": "qwen-27b"}, {"id": "nomic-embed"}]})
        else:
            self._send(404, {})

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        model = json.loads(self.rfile.read(n))["model"]
        if self.path == "/api/show" and model in SHOW:
            self._send(200, SHOW[model])
        else:
            self._send(404, {"error": "model not found"})


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


async def test_scan_finds_servers_models_and_capabilities(server):
    dead = "http://127.0.0.1:1"  # nothing listens here
    found = await scan_local({"ollama": ("ollama", server), "lmstudio": ("openai", server),
                              "vllm": ("openai", dead)})
    assert [s.name for s in found] == ["ollama", "lmstudio"]
    models = {m.name: m for m in found[0].models}
    assert models["big"].tools and not models["big"].vision and models["big"].size == 9 * GB
    assert models["small"].vision and not models["plain"].tools and models["emb"].embedding
    assert [m.name for m in found[1].models] == ["qwen-27b", "nomic-embed"]
    assert found[1].models[1].embedding


async def test_scan_with_nothing_running_returns_empty():
    assert await scan_local({"ollama": ("ollama", "http://127.0.0.1:1")}) == []


# --- /api/show ---

def test_parse_show_reads_capabilities_and_context():
    info = parse_show(SHOW["big"])
    assert info.caps == {"completion", "tools"} and info.ctx == 131072 and info.tools and not info.vision
    assert parse_show(SHOW["small"]).vision
    assert not parse_show(SHOW["plain"]).tools


def test_old_ollama_without_capabilities_is_assumed_to_support_tools():
    info = parse_show(SHOW["old"])
    assert info.caps is None and info.tools and not info.vision


async def test_load_capabilities_is_cached_and_sets_tool_support(server):
    p = OllamaProvider(server, "plain")
    info = await p.load_capabilities()
    assert info and not info.tools and p._tools_supported is False
    body = p._build_body([Message(role="user", content="hi")], [ToolDef("t", "d", {})])
    assert "tools" not in body  # a no-tools model is sent no tools
    p._client = None  # a second call must not touch the network
    assert await p.load_capabilities() is info


async def test_missing_model_is_flagged_not_raised(server):
    p = OllamaProvider(server, "ghost")
    assert await p.load_capabilities() is None and p.missing


async def test_unreachable_server_is_flagged_not_raised():
    p = OllamaProvider("http://127.0.0.1:1", "x")
    assert await p.load_capabilities() is None


async def test_probe_never_chats(server, monkeypatch):
    p = OllamaProvider(server, "big")
    calls = []
    real = httpx.AsyncClient.post

    async def spy(self, url, **kw):
        calls.append(str(url))
        return await real(self, url, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", spy)
    await p.load_capabilities()
    assert calls == [f"{server}/api/show"]
    assert not hasattr(p, "check_tool_support")


# --- num_ctx and keep_alive ---

def test_num_ctx_by_ram():
    assert default_num_ctx(8 * GIB) == 8192
    assert default_num_ctx(16 * GIB) == 8192
    assert default_num_ctx(32 * GIB) == 16384
    assert default_num_ctx(0) == 8192  # unknown RAM: the safe size


def test_ram_probe_returns_a_number_or_none():
    r = ollama.ram_bytes()
    assert r is None or r > 0


def test_macos_ram_comes_from_sysctl_once(monkeypatch):
    import subprocess as sp

    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return sp.CompletedProcess(argv, 0, stdout="17179869184\n")

    ollama._mac_ram.cache_clear()
    monkeypatch.setattr(ollama.sys, "platform", "darwin")
    monkeypatch.setattr(ollama.os, "name", "posix")
    monkeypatch.setattr(ollama.subprocess, "run", fake_run)
    monkeypatch.setattr(ollama.os, "sysconf", lambda name: (_ for _ in ()).throw(ValueError(name)))
    assert ollama.ram_bytes() == 16 * GIB and ollama.ram_bytes() == 16 * GIB
    assert calls == [["sysctl", "-n", "hw.memsize"]]
    ollama._mac_ram.cache_clear()
    monkeypatch.setattr(ollama.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert ollama.ram_bytes() is None
    ollama._mac_ram.cache_clear()


def test_every_chat_sends_num_ctx_and_keep_alive():
    p = OllamaProvider(model="m", num_ctx=4096, keep_alive="10m")
    body = p._build_body([Message(role="user", content="hi")])
    assert body["options"]["num_ctx"] == 4096 and body["keep_alive"] == "10m"
    assert OllamaProvider()._build_body([Message(role="user", content="x")])["keep_alive"] == "30m"


def test_num_ctx_is_capped_by_the_models_own_maximum():
    p = OllamaProvider(model="m", num_ctx=16384)
    p.info = ShowInfo(frozenset({"tools"}), 4096)
    assert p._build_body([Message(role="user", content="x")])["options"]["num_ctx"] == 4096


def test_config_num_ctx_reaches_the_provider():
    from micropaw.config import Config
    from micropaw.providers.registry import Registry
    c = Config()
    c.provider_config.ollama.num_ctx = 12000
    c.provider_config.ollama.keep_alive = "1h"
    p = Registry(c).get("ollama:m")._inner
    assert p.num_ctx == 12000 and p.keep_alive == "1h"


# --- size-aware picks ---

def lm(name, gb, **kw):
    return LocalModel(name, int(gb * GB), **kw)


def test_main_is_the_largest_tools_model_that_fits_in_60_percent_of_ram():
    models = [lm("a", 4), lm("b", 9), lm("c", 14), lm("emb", 1, embedding=True), lm("nt", 8, tools=False)]
    pick = pick_local(models, 16 * GIB)  # 60% = 10.3 GB
    assert pick.main.name == "b" and pick.fast.name == "a"
    assert pick_local(models, 32 * GIB).main.name == "c"
    assert pick_local(models, 4 * GIB).main is None  # nothing fits: the wizard offers a pull


def test_embedding_and_no_tool_models_are_never_main():
    pick = pick_local([lm("emb", 1, embedding=True), lm("nt", 2, tools=False)], 16 * GIB)
    assert pick.main is None


def test_vision_pick_only_when_main_cannot_see():
    pick = pick_local([lm("big", 9), lm("eye", 3, vision=True)], 32 * GIB)
    assert pick.vision.name == "eye"
    assert pick_local([lm("big", 9, vision=True), lm("eye", 3, vision=True)], 32 * GIB).vision is None


def test_pull_suggestion_follows_ram():
    assert suggest_pull(8 * GIB) == "qwen3.5:4b"
    assert suggest_pull(16 * GIB) == "qwen3.5:9b"
    assert suggest_pull(32 * GIB) == "qwen3.8:27b"
    assert suggest_pull(64 * GIB) == "qwen3.6:35b"
    assert suggest_pull(4 * GIB) == "granite4.1:3b"
    assert suggest_pull(None) == "qwen3.5:4b"


async def test_gateway_probes_every_ollama_role_not_only_main(server, caplog):
    import logging
    from unittest.mock import MagicMock

    from micropaw.config import Config
    from micropaw.core.profile import build_runtimes
    from micropaw.gateway import Gateway
    from micropaw.providers.registry import Registry

    c = Config(provider="ollama", models={"fast": "ollama:plain", "vision": "ollama:small"},
               fallback_providers=["ollama:ghost"])
    c.provider_config.ollama.host = server
    c.provider_config.ollama.model = "big"
    reg = Registry(c)
    rts = build_runtimes(c, reg)  # main is a failover chain, so the old name == "ollama" check would skip it
    assert rts["main"].provider.name.startswith("failover(")
    gw = Gateway(c, rts["main"].provider, MagicMock(), [], runtimes=rts, models=reg)
    with caplog.at_level(logging.INFO):
        await gw._probe_models(rts)
    text = caplog.text
    assert "Ollama model big: tools ok" in text
    assert "'plain' cannot use tools" in text
    assert "small: tools ok, vision ok" in text
    assert "'ghost' is not available" in text and "ollama pull ghost" in text
