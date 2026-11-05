"""web_browse and the tool list by privacy mode, the idle close, and the local-mode page limits."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from micropaw import privacy, webfetch
from micropaw.cli import _create_registry
from micropaw.config import Config
from micropaw.tools import web_browse
from micropaw.tools.base import ToolContext
from micropaw.tools.web_browse import WebBrowseTool


@pytest.fixture
def mode():
    def set_mode(m, allow=(), proxy=""):
        privacy._reset_for_tests()
        privacy.freeze(m)
        webfetch.set_privacy_config(SimpleNamespace(allow=list(allow)), proxy)

    yield set_mode
    privacy._reset_for_tests()
    webfetch.set_privacy_config(None)
    web_browse._approved_hosts.clear()
    web_browse._expired.clear()


def ctx(marked=False, conv="c1") -> ToolContext:
    return ToolContext(conversation_id=conv, user_id="u", platform="terminal", marked=marked)


async def test_airgapped_refuses_browse_and_local_needs_allow(mode):
    mode("airgapped", allow=["web_browse"])
    assert "air-gapped" in await WebBrowseTool().execute({"action": "browse", "url": "https://example.com"}, ctx())
    mode("local")
    out = await WebBrowseTool().execute({"action": "browse", "url": "https://example.com"}, ctx())
    assert "off in local mode" in out and "privacy.allow" in out


def test_local_mode_asks_for_every_page_with_the_cleaned_address_and_route(mode):
    mode("local", allow=["web_browse"], proxy="socks5h://user:pw@127.0.0.1:9050")
    ask = WebBrowseTool().needs_approval(
        {"action": "browse", "url": "https://example.com/a?utm_source=x&id=7#frag"}, ctx())
    assert ask.startswith("Open https://example.com/a?id=7 in the browser (through 127.0.0.1:9050)")
    assert "pw" not in ask and "utm_source" not in ask
    long = "https://example.com/" + "a" * 60
    assert "Long parts" in WebBrowseTool().needs_approval({"action": "browse", "url": long}, ctx())
    assert WebBrowseTool().needs_approval({"action": "click", "selector": "a"}, ctx()) is None


def test_normal_mode_asks_before_browsing_only_in_a_marked_chat(mode):
    mode("normal")
    args = {"action": "browse", "url": "https://example.com"}
    assert WebBrowseTool().needs_approval(args, ctx()) is None
    assert "Open https://example.com" in WebBrowseTool().needs_approval(args, ctx(marked=True))


def test_the_tool_list_follows_the_mode(mode, monkeypatch):
    import micropaw.cli as cli

    def names(m, allow=()):
        mode(m)
        monkeypatch.setattr(cli, "_privacy_allow", lambda config: list(allow))
        return set(_create_registry(Config()).names)

    assert {"web_browse", "http_request", "web_search"} <= names("normal")
    air = names("airgapped")
    assert "web_browse" not in air and "http_request" not in air and "web_search" in air
    local = names("local")
    assert "web_browse" not in local and "http_request" not in local and "web_search" in local
    allowed = names("local", ["web_browse", "web_fetch"])
    assert {"web_browse", "http_request"} <= allowed
    assert "web_browse" not in names("airgapped", ["web_browse", "web_fetch"])


# --- the page limits and the idle close, with a fake browser ---

class FakeCtx:
    def __init__(self):
        self.handler = None
        self.closed = 0

    async def route(self, pattern, handler):
        self.handler = handler

    async def new_page(self):
        return SimpleNamespace(url="about:blank", goto=self._goto)

    async def _goto(self, *a, **k):
        pass

    async def close(self):
        self.closed += 1


class FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.closed = 0

    async def new_context(self):
        self.contexts.append(FakeCtx())
        return self.contexts[-1]

    async def close(self):
        self.closed += 1


class FakeRoute:
    def __init__(self, url):
        self.request = SimpleNamespace(url=url)
        self.outcome = ""

    async def continue_(self):
        self.outcome = "continue"

    async def abort(self):
        self.outcome = "abort"


@pytest.fixture
def fake_browser(monkeypatch):
    browser = FakeBrowser()

    async def get():
        return browser

    monkeypatch.setattr(web_browse, "_get_browser", get)
    web_browse._sessions.clear()
    yield browser
    web_browse._sessions.clear()


async def test_local_pages_load_only_from_the_approved_host(mode, fake_browser):
    mode("local", allow=["web_browse"])
    web_browse._approved_hosts["c1"] = {"example.com"}
    await web_browse._get_session("c1", "https://example.com/a")
    handler = fake_browser.contexts[0].handler
    results = {}
    for url in ("https://example.com/x.js", "https://tracker.example.net/p.gif", "https://cdn.example.com/a.css"):
        route = FakeRoute(url)
        await handler(route)
        results[url] = route.outcome
    assert results == {"https://example.com/x.js": "continue", "https://tracker.example.net/p.gif": "abort",
                       "https://cdn.example.com/a.css": "abort"}


async def test_normal_mode_pages_are_not_limited(mode, fake_browser):
    mode("normal")
    await web_browse._get_session("c1", "https://example.com/a")
    assert fake_browser.contexts[0].handler is None


async def test_local_mode_does_not_use_the_youtube_helper(mode, monkeypatch):
    mode("local", allow=["web_browse"])
    called = []

    async def helper(video_id):
        called.append(video_id)
        return "transcript"

    async def ok(url):
        return None

    class Page:
        url = "https://www.youtube.com/watch?v=abcdefghijk"

        async def title(self):
            return "t"

    async def get_session(conv, url=None):
        return Page()

    monkeypatch.setattr(web_browse, "fetch_youtube_transcript", helper)
    monkeypatch.setattr(web_browse, "validate_url", ok)
    monkeypatch.setattr(web_browse, "_get_session", get_session)
    out = await WebBrowseTool()._action_browse(
        {"url": "https://www.youtube.com/watch?v=abcdefghijk", "extract": "title"}, "c1")
    assert not called and "Page title: t" in out


async def test_the_browser_and_pages_close_after_idling_and_use_restarts_the_clock(monkeypatch, fake_browser, mode):
    mode("normal")
    monkeypatch.setattr(web_browse, "IDLE_SECONDS", 0.1)
    closed_pw = []
    monkeypatch.setattr(web_browse, "_browser", fake_browser)
    monkeypatch.setattr(web_browse, "_playwright", SimpleNamespace(stop=lambda: _async(closed_pw.append(1))))
    ctx_ = FakeCtx()
    web_browse._sessions["c1"] = (ctx_, None)
    web_browse._touch()
    await asyncio.sleep(0.06)
    web_browse._touch()  # use inside the window: the clock restarts
    await asyncio.sleep(0.06)
    assert fake_browser.closed == 0
    await asyncio.sleep(0.2)
    assert fake_browser.closed == 1 and ctx_.closed == 1 and closed_pw == [1]
    assert "c1" in web_browse._expired and not web_browse._sessions
    out = await WebBrowseTool().execute({"action": "click", "selector": "a"}, ctx())
    assert "expired" in out and "'browse'" in out
    web_browse._touch()
    await web_browse.cleanup_browser()  # shutdown cancels the timer


async def _async(value):
    return value
