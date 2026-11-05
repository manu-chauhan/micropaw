"""http_request in local mode with the real URL check and the real guard: only the name lookup is faked."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from micropaw import privacy
from micropaw.config import PrivacyConfig
from micropaw.tools.base import ToolContext
from micropaw.tools.http_request import HttpRequestTool

PUBLIC = "93.184.216.34"


@pytest.fixture(autouse=True)
def local_mode():
    privacy._reset_for_tests()
    privacy.freeze("local")
    privacy.configure(PrivacyConfig(mode="local", allow=["web_fetch"]))
    yield
    privacy._reset_for_tests()


@pytest.fixture
def server():
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"INTERNAL-SERVICE-SECRET"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], hits
    srv.shutdown()


def answers(monkeypatch, *seq):
    """getaddrinfo gives each address in turn, then the last one for ever."""
    calls = iter(seq)
    last = [seq[-1]]

    def fake(host, *a, **k):
        last[0] = next(calls, last[0])
        return [(2, 1, 6, "", (last[0], 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


async def run(url):
    return await HttpRequestTool().execute({"url": url}, ToolContext("c", "u", "terminal"))


@pytest.mark.parametrize("flip", ["127.0.0.1", "10.1.2.3", "192.168.1.1"])
async def test_a_name_that_flips_to_a_private_address_after_the_checks_never_connects(monkeypatch, server, flip):
    port, hits = server
    answers(monkeypatch, PUBLIC, PUBLIC, flip)  # the owner's permit, validate_url, then the connect
    url = f"http://evil.test:{port}/x"
    out = await run(url)
    assert out.startswith("Error:") and "private address" in out and "SECRET" not in out
    assert hits == []


async def test_a_name_that_changes_between_public_addresses_still_connects(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(200, text="ok")
    monkeypatch.setattr("micropaw.tools.http_request._inner_transport", lambda: httpx.MockTransport(handler))
    answers(monkeypatch, PUBLIC, PUBLIC, "93.184.216.35")
    out = await run("http://cdn.test/a")
    assert out.startswith("HTTP 200") and seen == ["93.184.216.35"]


async def test_an_approved_public_ip_literal_works_and_a_private_one_does_not(monkeypatch):
    monkeypatch.setattr("micropaw.tools.http_request._inner_transport",
                        lambda: httpx.MockTransport(lambda r: httpx.Response(200, text="ok")))
    assert (await run(f"http://{PUBLIC}/")).startswith("HTTP 200")
    assert (await run("http://192.168.1.1/")).startswith("Error:")


async def test_a_redirect_to_another_host_is_refused(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"location": "http://other.test/"})
    monkeypatch.setattr("micropaw.tools.http_request._inner_transport", lambda: httpx.MockTransport(handler))
    answers(monkeypatch, PUBLIC)
    out = await run("http://one.test/")
    assert out.startswith("Error:") and "other.test" in out
