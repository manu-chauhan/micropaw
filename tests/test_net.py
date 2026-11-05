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

# Modules that still build their own clients. Each stream moves its own onto net.client; this list only shrinks.
# Owned by the other round 3 streams, so they are listed here until that work is merged:
PENDING = {
    "channels/whatsapp.py", "channels/media.py", "memory/embeddings.py", "utils/voice.py",
    "tools/web_search.py", "tools/web_browse.py", "connector.py", "cli.py",
}


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
    assert net.permit("search.example") is None
    assert asyncio.run(get("https://search.example/q")) == "through"
    with pytest.raises(privacy.EgressBlocked):
        asyncio.run(get("https://other.example/q"))
    set_mode("airgapped")
    assert net.permit("search.example")  # a refusal text


def test_sync_client_has_the_same_rules():
    set_mode("local")
    with net.sync_client("https://example.com", transport=httpx.MockTransport(ok)) as c:
        with pytest.raises(privacy.EgressBlocked):
            c.get("https://example.com")
        assert c.get("http://127.0.0.1:1").text == "through"
