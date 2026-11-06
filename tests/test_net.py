"""The client factory: one way to make an HTTP client, and the guard in front of it."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import httpx
import pytest

from micropaw import net, privacy
from micropaw.config import PrivacyConfig

SRC = Path(__file__).parent.parent / "src" / "micropaw"
BANNED_HTTPX = {"AsyncClient", "Client", "get", "post", "put", "delete", "head", "patch", "request", "stream"}

# Modules allowed to build their own clients. Empty: everything goes through net.client. The scan below stays
# so that a new module cannot go around it.
PENDING: set[str] = set()


def direct_calls(tree: ast.AST) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        base = node.func.value
        name = ast.unparse(base)
        if name == "httpx" and node.func.attr in BANNED_HTTPX:
            found.append((node.lineno, f"httpx.{node.func.attr}"))
        elif name == "aiohttp" and node.func.attr == "ClientSession":
            found.append((node.lineno, "aiohttp.ClientSession"))
        elif name in ("urllib.request", "request") and node.func.attr == "urlopen":
            found.append((node.lineno, "urlopen"))
        elif name == "requests":
            found.append((node.lineno, f"requests.{node.func.attr}"))
    return found


def test_no_new_code_builds_its_own_http_client():
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "net.py" or rel in PENDING:
            continue
        if calls := direct_calls(ast.parse(path.read_text(encoding="utf-8"))):
            offenders[rel] = calls
    assert not offenders, f"build clients with micropaw.net.client: {offenders}"


def test_the_pending_list_only_names_files_that_still_need_it():
    stale = [rel for rel in PENDING if not direct_calls(ast.parse((SRC / rel).read_text(encoding="utf-8")))]
    assert not stale, f"these no longer build their own client, remove them from PENDING: {stale}"


def test_the_scan_sees_the_shapes_it_is_meant_to_catch():
    code = ("import httpx, aiohttp, requests\nhttpx.get('x')\nhttpx.AsyncClient()\n"
            "aiohttp.ClientSession()\nrequests.post('x')")
    assert [c[1] for c in direct_calls(ast.parse(code))] == [
        "httpx.get", "httpx.AsyncClient", "aiohttp.ClientSession", "requests.post"]


def set_mode(name: str, **kw) -> None:
    privacy._reset_for_tests()
    privacy.freeze(name)
    privacy.configure(PrivacyConfig(mode=name, **kw))


def ok(request):
    return httpx.Response(200, text="through")


async def get(url: str) -> str:
    async with net.client(url, transport=httpx.MockTransport(ok)) as c:
        return (await c.get(url)).text


def test_normal_mode_lets_requests_through():
    set_mode("normal")
    assert asyncio.run(get("https://example.com")) == "through"


@pytest.mark.parametrize("mode,word", [("local", "local"), ("airgapped", "air-gapped")])
def test_strict_modes_refuse_an_outside_host_before_any_lookup(mode, word):
    set_mode(mode)
    with pytest.raises(privacy.EgressBlocked) as err:
        asyncio.run(get("https://example.com/x"))
    assert str(err.value) == f"{word} mode blocked a connection to example.com"
    assert asyncio.run(get("http://127.0.0.1:11434")) == "through"
    assert asyncio.run(get("http://192.168.1.5:8000")) == "through"  # a LAN address, allowed by default


def test_a_permit_opens_one_host_for_a_short_time(monkeypatch):
    set_mode("local")
    monkeypatch.setattr(privacy.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert privacy.permit("search.example") is None
    assert asyncio.run(get("https://search.example/q")) == "through"
    with pytest.raises(privacy.EgressBlocked):
        asyncio.run(get("https://other.example/q"))
    set_mode("airgapped")
    assert privacy.permit("search.example")  # a refusal text


def test_sync_client_has_the_same_rules():
    set_mode("local")
    with net.sync_client("https://example.com", transport=httpx.MockTransport(ok)) as c:
        with pytest.raises(privacy.EgressBlocked):
            c.get("https://example.com")
        assert c.get("http://127.0.0.1:1").text == "through"


# --- the modules that used to build their own client now stop at the guard -------------------------------

def test_the_connector_checks_stop_at_the_guard_in_airgapped_mode():
    from micropaw import connector
    set_mode("airgapped")
    ok_, detail = connector.validate_telegram_token("123:abc")
    assert not ok_ and detail == "air-gapped mode blocked a connection to api.telegram.org"
    assert connector.validate_ollama("http://127.0.0.1:1")[1] != detail  # a local server is still tried


def test_a_whatsapp_send_stops_at_the_guard_in_airgapped_mode(caplog):
    from micropaw.channels.whatsapp import WhatsAppChannel
    set_mode("airgapped")
    asyncio.run(WhatsAppChannel("t", "1")._send("to", "hi"))
    assert "air-gapped mode blocked a connection to graph.facebook.com" in caplog.text


def test_an_embedding_server_outside_the_policy_is_skipped_not_raised():
    from micropaw.memory.embeddings import OpenAIEmbedding
    set_mode("local")
    assert asyncio.run(OpenAIEmbedding("k", base_url="https://api.openai.com").embed("text")) == []


def test_public_searxng_instances_and_the_other_providers_stop_at_the_guard(caplog):
    import logging

    from micropaw.tools import web_search
    caplog.set_level(logging.DEBUG)
    set_mode("airgapped")
    assert asyncio.run(web_search._search_searxng("q", 3)) is None
    assert asyncio.run(web_search._search_brave("q", 3, "key")) is None
    assert caplog.text.count("EgressBlocked") >= 2
    found = asyncio.run(web_search._search_duckduckgo("q", 3))
    assert "air-gapped mode blocked a connection to lite.duckduckgo.com" in found


def test_a_searxng_proxy_is_passed_to_the_client(monkeypatch):
    from micropaw.tools import web_search
    seen = {}

    def fake(url="", **kw):
        seen.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(web_search.net, "client", fake)
    set_mode("normal")
    assert asyncio.run(web_search._search_searxng("q", 3, "http://localhost:8888", "socks5h://127.0.0.1:9050")) is None
    assert seen["proxy"] == "socks5h://127.0.0.1:9050"
    seen.clear()
    asyncio.run(web_search._search_searxng("q", 3))
    assert "proxy" not in seen
