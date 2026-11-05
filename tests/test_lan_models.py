"""Models on another computer: LAN and Tailscale addresses, names that resolve late, plain errors, two Ollama
hosts, and no proxy for local traffic."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from micropaw import net, privacy
from micropaw.config import Config
from micropaw.providers import registry as reg_mod
from micropaw.providers.base import Message
from micropaw.providers.failover import FriendlyError
from micropaw.providers.ollama import OllamaProvider
from micropaw.providers.registry import Registry, _Gate, is_local_url
from micropaw.security import validate_url
from micropaw.tools.base import ToolContext
from micropaw.tools.http_request import HttpRequestTool

USER = [Message(role="user", content="hi")]

LAN_URLS = [
    "http://192.168.1.50:11434", "http://10.0.0.7:8000/v1", "http://172.20.0.5:1234",
    "http://100.101.102.103:11434", "http://[fd7a:115c:a1e0::1]:11434", "http://gpu-box.local:11434",
    "http://gpu.tail1234.ts.net:11434",
]


def answer(addr: str):
    return lambda host, port, *a, **k: [(2, 1, 6, "", (addr, 0))]


@pytest.fixture(autouse=True)
def _fresh_dns():
    reg_mod._dns.clear()
    yield
    reg_mod._dns.clear()


# --- LAN-1: provider URLs may be on the LAN, the agent's own tools may not ------------------------

@pytest.mark.parametrize("url", LAN_URLS)
async def test_lan_addresses_are_local_for_models_and_blocked_for_tools(monkeypatch, url):
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", answer("192.168.1.50"))
    monkeypatch.setattr(socket, "getaddrinfo", answer("100.101.102.103" if "ts.net" in url else "192.168.1.50"))
    assert is_local_url(url) is True
    assert await validate_url(url)  # a block message: LAN stays out of reach of http_request and web_browse


@pytest.mark.parametrize("url,local", [("http://100.101.102.103:11434", True), ("http://100.128.0.1:11434", False),
                                       ("http://100.64.0.1", True), ("http://100.127.255.254", True)])
def test_tailscale_range_is_exactly_100_64_slash_10(url, local):
    assert is_local_url(url) is local


async def test_a_configured_provider_url_is_still_refused_by_the_tools(monkeypatch):
    c = Config(providers={"gpu": {"base_url": "http://192.168.1.50:8000/v1"}})
    assert Registry(c).is_local("gpu:m")
    result = await HttpRequestTool().execute({"url": "http://192.168.1.50:8000/v1/models"},
                                             ToolContext("c", "u", "terminal"))
    assert result.startswith("Error: Blocked")


# --- LAN-3: Tailscale names and lookups that fail -------------------------------------------------

def test_a_tailscale_name_is_checked_for_any_scheme(monkeypatch):
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", answer("100.101.102.103"))
    assert is_local_url("https://gpu.tail1234.ts.net")
    reg_mod._dns.clear()
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", answer("203.0.113.5"))  # Funnel: a public answer
    assert not is_local_url("https://gpu.tail1234.ts.net")
    assert not is_local_url("https://other.example.com")  # an https name that is not a tailnet name: cloud


def test_a_failed_lookup_is_not_remembered(monkeypatch):
    calls = []

    def flaky(host, port, *a, **k):
        calls.append(host)
        if len(calls) == 1:
            raise OSError("temporary failure")
        return [(2, 1, 6, "", ("192.168.1.50", 0))]
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", flaky)
    assert is_local_url("http://gpu-box:11434") is False
    assert is_local_url("http://gpu-box:11434") is True
    assert is_local_url("http://gpu-box:11434") is True and len(calls) == 2  # the finished answer is kept


def test_a_finished_lookup_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(reg_mod, "_now", lambda: now[0])
    calls = []

    def private(host, port, *a, **k):
        calls.append(host)
        return [(2, 1, 6, "", ("192.168.1.50", 0))]
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", private)
    assert is_local_url("http://gpu-box") and is_local_url("http://gpu-box") and len(calls) == 1
    now[0] += reg_mod.DNS_TTL + 1
    assert is_local_url("http://gpu-box") and len(calls) == 2


def test_a_slow_dns_server_cannot_stall_the_check(monkeypatch):
    monkeypatch.setattr(reg_mod, "DNS_TIMEOUT", 0.2)
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", lambda *a, **k: time.sleep(2) or [])
    start = time.monotonic()
    assert is_local_url("http://slow-box") is False
    assert time.monotonic() - start < 1.0


async def test_a_name_that_does_not_resolve_yet_waits_instead_of_being_refused(monkeypatch):
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("asleep")))
    c = Config(providers={"gpubox": {"base_url": "http://gpubox:8000/v1"}})
    reg = Registry(c)
    assert reg.problems == [] and reg.is_local("gpubox:m")
    provider = reg.get("gpubox:m")
    with pytest.raises(FriendlyError, match="Cannot look up gpubox from this computer") as err:
        await provider.chat(USER)
    assert "192.168.1.50" in str(err.value)
    # the box wakes up: the same provider now goes through
    reg_mod._dns.clear()
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", answer("192.168.1.50"))
    inner = provider._inner

    def ok(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                                         "usage": {}})
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(ok))
    assert (await provider.chat(USER)).content == "hello"


async def test_a_pending_name_that_turns_out_public_is_refused_with_the_reason(monkeypatch):
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("asleep")))
    reg = Registry(Config(providers={"gpubox": {"base_url": "http://gpubox:8000/v1"}}))
    provider = reg.get("gpubox:m")
    reg_mod._dns.clear()
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", answer("203.0.113.5"))
    with pytest.raises(FriendlyError, match="resolves to a public address"):
        await provider.chat(USER)


def test_a_name_that_resolves_public_is_refused_with_the_reason(monkeypatch):
    monkeypatch.setattr(reg_mod.socket, "getaddrinfo", answer("203.0.113.5"))
    reg = Registry(Config(providers={"gpubox": {"base_url": "http://gpubox:8000/v1"}}))
    assert any("resolves to a public address" in p and "Use https" in p for p in reg.problems)
    assert reg.get("gpubox:m") is None


# --- LAN-4: plain errors that name the other machine ----------------------------------------------

class Boom:
    """A provider whose calls raise the given error."""

    def __init__(self, error, host):
        self.name, self.model, self.host, self.error = "gpu", "m", host, error

    async def chat(self, messages, tools=None):
        raise self.error

    async def close(self):
        pass


def status(code):
    return httpx.HTTPStatusError("x", request=httpx.Request("POST", "http://h"), response=httpx.Response(code))


async def message(error, host, key_env="", local="lmstudio"):
    import asyncio
    gate = _Gate(Boom(error, host), asyncio.Semaphore(1), local, key_env)
    with pytest.raises(Exception) as ei:
        await gate.chat(USER)
    return str(ei.value)


async def test_errors_name_the_remote_machine_but_keep_the_old_text_for_this_one():
    assert "start the Ollama app" in await message(httpx.ConnectError("x"), "http://127.0.0.1:11434", local="ollama")
    remote = await message(httpx.ConnectError("x"), "http://192.168.1.50:11434", local="ollama")
    assert remote.startswith("Cannot reach 192.168.1.50:11434. Is that computer on and awake") and "listen" in remote
    assert "firewall" not in remote
    timeout = await message(httpx.ConnectTimeout("x"), "http://192.168.1.50:11434", local="ollama")
    assert "firewall on that computer may be blocking port 11434" in timeout
    slow = await message(httpx.ReadTimeout("x"), "http://192.168.1.50:11434", local="ollama")
    assert slow.startswith("The model at 192.168.1.50:11434 did not answer within 5 minutes")
    assert "did not answer within 5 minutes. It may still be loading. Try again, or use a smaller" in \
        await message(httpx.ReadTimeout("x"), "http://127.0.0.1:1234", local="lmstudio")


async def test_a_rejected_key_names_the_variable_never_its_value(monkeypatch):
    monkeypatch.setenv("GPUBOX_KEY", "s3cret-value")
    with_key = await message(status(401), "http://192.168.1.50:8000/v1", key_env="GPUBOX_KEY", local="gpubox")
    assert "rejected the key in GPUBOX_KEY. Check it in ~/.micropaw/.env" in with_key and "s3cret" not in with_key
    no_key = await message(status(403), "http://192.168.1.50:8000/v1", local="gpubox")
    assert "needs a key. Run: paw models add gpubox <url> --key-env <NAME>" in no_key


async def test_an_address_with_a_password_does_not_leak_into_the_message():
    text = await message(httpx.ConnectError("x"), "http://user:hunter2@192.168.1.50:11434", local="ollama")
    assert "hunter2" not in text and "192.168.1.50:11434" in text


# --- LAN-5: named endpoints that speak native Ollama ----------------------------------------------

class OllamaFake(BaseHTTPRequestHandler):
    seen: list[tuple[str, dict, str]] = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, body, self.headers.get("Authorization", "")))
        out = ({"capabilities": ["completion", "tools"], "model_info": {"x.context_length": 4096}}
               if self.path == "/api/show" else {"message": {"content": "ok"}})
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def two_ollamas():
    OllamaFake.seen = []
    servers = [HTTPServer(("127.0.0.1", 0), OllamaFake) for _ in range(2)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield [f"http://127.0.0.1:{s.server_port}" for s in servers]
    for s in servers:
        s.shutdown()
        s.server_close()


async def test_two_ollama_hosts_work_side_by_side(two_ollamas, monkeypatch):
    monkeypatch.setenv("DESKTOP_KEY", "tok")
    a, b = two_ollamas
    c = Config(providers={"desktop": {"base_url": b, "kind": "ollama", "num_ctx": 6000, "keep_alive": "5m",
                                      "api_key_env": "DESKTOP_KEY"}})
    c.provider_config.ollama.host = a
    reg = Registry(c)
    pi, desktop = reg.get("ollama:small"), reg.get("desktop:big")
    inner_a, inner_b = pi._inner, desktop._inner
    assert isinstance(inner_a, OllamaProvider) and isinstance(inner_b, OllamaProvider)
    assert inner_a.host == a and inner_b.host == b and pi._sem is not desktop._sem
    info = await inner_b.load_capabilities()  # the gateway does this at start
    assert info and info.tools
    assert (await desktop.chat(USER)).content == "ok"
    path, body, auth = [x for x in OllamaFake.seen if x[0] == "/api/chat"][0]
    assert body["options"]["num_ctx"] == 4096  # the model's own maximum caps the 6000 asked for
    assert body["keep_alive"] == "5m" and auth == "Bearer tok"
    assert any(p == "/api/show" and a == "Bearer tok" for p, _, a in OllamaFake.seen)


def test_a_remote_ollama_does_not_take_its_context_from_this_machine():
    c = Config(providers={"desk": {"base_url": "http://192.168.1.50:11434", "kind": "ollama"}})
    desk = Registry(c).get("desk:m")._inner
    assert desk.num_ctx == 8192
    assert Registry(Config()).get("ollama:m")._inner.num_ctx > 0  # this machine: sized by its own memory


def test_a_missing_key_variable_is_reported_for_an_ollama_entry(monkeypatch):
    monkeypatch.delenv("DESKTOP_KEY", raising=False)
    reg = Registry(Config(providers={"desk": {"base_url": "http://127.0.0.1:1", "kind": "ollama",
                                              "api_key_env": "DESKTOP_KEY"}}))
    assert reg.get("desk:m") is None and "DESKTOP_KEY" in reg.problems[-1]


def test_one_gate_per_server_however_its_address_is_written():
    c = Config(providers={"alt": {"base_url": "http://192.168.1.50:11434/v1"}})
    c.provider_config.ollama.host = "http://192.168.1.50:11434"
    reg = Registry(c)
    assert reg._sem(reg.resolve("ollama:m")) is reg._sem(reg.resolve("alt:m"))


# --- LAN-11 and PI P16: proxies, TLS context, connection limits -----------------------------------

def test_local_clients_skip_proxies_and_cloud_clients_keep_them():
    assert net.client("http://192.168.1.50:8000")._trust_env is False
    assert net.client("http://localhost:11434")._trust_env is False
    assert net.client("https://api.anthropic.com")._trust_env is True
    assert net.client("https://api.anthropic.com", local=True)._trust_env is False


def test_strict_modes_never_use_environment_proxies():
    privacy._reset_for_tests()
    privacy.freeze("local")
    from micropaw.config import PrivacyConfig
    privacy.configure(PrivacyConfig(mode="local"))
    assert net.client("https://anything.example")._trust_env is False


async def test_a_dead_proxy_in_the_environment_does_not_break_a_local_server(monkeypatch, two_ollamas):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    c = Config(providers={"desk": {"base_url": two_ollamas[0], "kind": "ollama"}})
    assert (await Registry(c).get("desk:m").chat(USER)).content == "ok"


def test_provider_clients_share_one_tls_context_and_keep_connections_warm():
    assert net.ssl_context() is net.ssl_context()
    a, b = net.client("https://api.anthropic.com", limits=net.LIMITS), net.client("https://api.openai.com",
                                                                                  limits=net.LIMITS)
    pool = a._transport._pool
    assert (pool._max_connections, pool._max_keepalive_connections, pool._keepalive_expiry) == (4, 2, 120.0)
    assert a._transport._pool._ssl_context is b._transport._pool._ssl_context
    from micropaw.providers.claude import ClaudeProvider
    assert ClaudeProvider("k")._client._transport._pool._keepalive_expiry == 120.0
    assert Registry(Config(providers={"x": {"base_url": "http://127.0.0.1:1"}})).get("x:m")._inner._client \
        ._transport._pool._max_connections == 4
