"""web_browse preview: which files may be served, the local server, and the action with a fake browser."""

from __future__ import annotations

import http.client
import os
import sys
from types import SimpleNamespace

import pytest

from micropaw.tools import web_browse
from micropaw.tools.base import ToolContext, ToolResult
from micropaw.tools.web_browse import WebBrowseTool, preview_file

needs_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="creating symlinks needs a privilege on Windows")


@pytest.fixture
def ws(tmp_path):
    path = tmp_path / "ws"
    path.mkdir()
    (path / "game.html").write_text("<html><body>game</body></html>")
    (path / "sub").mkdir()
    (path / "sub" / "g2.html").write_text("<html></html>")
    (tmp_path / "secret.html").write_text("<html>secret</html>")
    return path


# --- which files may be previewed ---

def test_workspace_html_is_allowed(ws):
    assert preview_file("game.html", str(ws)) == ws / "game.html"
    assert preview_file("sub/g2.html", str(ws)) == ws / "sub" / "g2.html"
    assert preview_file(str(ws / "game.html"), str(ws)) == ws / "game.html"


@pytest.mark.parametrize("raw", [
    "../secret.html", "sub/../../secret.html", "/etc/passwd", "game.txt", "missing.html", "", "sub", ".env",
])
def test_everything_else_is_refused(ws, raw):
    assert isinstance(preview_file(raw, str(ws)), str)


def test_no_workspace_means_no_preview(ws):
    assert isinstance(preview_file("game.html", ""), str)


def test_secret_looking_names_are_refused(ws):
    (ws / ".env.html").write_text("<html>KEY=1</html>")
    (ws / ".ssh").mkdir()
    (ws / ".ssh" / "x.html").write_text("<html></html>")
    assert isinstance(preview_file(".ssh/x.html", str(ws)), str)


@needs_symlinks
def test_symlinks_are_refused(ws, tmp_path):
    os.symlink(tmp_path / "secret.html", ws / "linked.html")
    os.symlink(tmp_path, ws / "up")
    assert isinstance(preview_file("linked.html", str(ws)), str)
    assert isinstance(preview_file("up/secret.html", str(ws)), str)


# --- the server ---

def fetch(server, path):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    return resp.status, resp.read()


@pytest.fixture
def server(ws):
    srv = web_browse._preview_server(ws)
    yield srv
    srv.shutdown()
    srv.server_close()


def test_server_binds_loopback_on_a_random_port_and_serves_files(server):
    assert server.server_address[0] == "127.0.0.1" and server.server_address[1] > 0
    status, body = fetch(server, "/game.html")
    assert status == 200 and b"game" in body
    assert fetch(server, "/sub/g2.html")[0] == 200
    assert fetch(server, "/game.html?x=1#y")[0] == 200


@pytest.mark.parametrize("path", [
    "/../secret.html", "/sub/../../secret.html", "/%2e%2e/secret.html", "/..%2fsecret.html", "/nope.html",
    "/sub", "/", "/..\\secret.html", "/C:/Windows/win.ini",
])
def test_server_refuses_traversal_and_missing(server, path):
    status, body = fetch(server, path)
    assert status == 404 and b"secret" not in body


def test_server_refuses_secret_names(server, ws):
    (ws / ".env").write_text("KEY=1")
    (ws / "id_rsa").write_text("key")
    assert fetch(server, "/.env")[0] == 404 and fetch(server, "/id_rsa")[0] == 404


@needs_symlinks
def test_server_refuses_symlinks(server, ws, tmp_path):
    os.symlink(tmp_path / "secret.html", ws / "linked.html")
    os.symlink(tmp_path, ws / "up")
    assert fetch(server, "/linked.html")[0] == 404
    assert fetch(server, "/up/secret.html")[0] == 404


# --- the action, with a fake browser ---

class FakePage:
    def __init__(self, state, console, shots):
        self.handlers = {}
        self.state, self.console, self.shots = state, console, shots
        self.url = ""

    def on(self, event, cb):
        self.handlers[event] = cb

    async def goto(self, url, **kw):
        self.url = url
        for msg in self.console:
            self.handlers["console"](SimpleNamespace(type="error", text=msg))
        self.handlers["console"](SimpleNamespace(type="log", text="just a log"))
        self.handlers["pageerror"]("ReferenceError: x is not defined")

    async def wait_for_timeout(self, ms):
        pass

    async def evaluate(self, js):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state

    async def screenshot(self, **kw):
        self.shots.append(kw)
        return b"png-bytes"


class FakeContext:
    def __init__(self, page):
        self.page, self.routes, self.scripts, self.closed = page, [], [], False

    async def new_page(self):
        return self.page

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def add_init_script(self, js):
        self.scripts.append(js)

    async def close(self):
        self.closed = True


with_playwright = SimpleNamespace(util=SimpleNamespace(find_spec=lambda n: object()))


@pytest.fixture
def browser(monkeypatch):
    made = SimpleNamespace(page=None, ctx=None, shots=[])

    def build(state=None, console=()):
        made.page = FakePage(state, list(console), made.shots)
        made.ctx = FakeContext(made.page)

        async def new_context():
            return made.ctx

        async def get_browser():
            return SimpleNamespace(new_context=new_context)

        monkeypatch.setattr(web_browse, "_get_browser", get_browser)
        monkeypatch.setattr(web_browse, "importlib", with_playwright)
        return made

    yield build
    for conv in list(web_browse._previews):
        srv = web_browse._previews.pop(conv)
        srv.shutdown()
        srv.server_close()
    web_browse._sessions.clear()


def context(ws) -> ToolContext:
    return ToolContext(conversation_id="conv1", user_id="u", platform="terminal", workspace=str(ws))


async def test_preview_without_playwright_says_how_to_get_it(ws, monkeypatch):
    monkeypatch.setattr(web_browse, "importlib", SimpleNamespace(util=SimpleNamespace(find_spec=lambda n: None)))
    out = await WebBrowseTool().execute({"action": "preview", "path": "game.html"}, context(ws))
    assert "paw extras add browser" in out and not web_browse._previews


async def test_preview_reports_errors_state_and_screenshot(ws, browser):
    made = browser(state={"score": 3, "over": False}, console=["TypeError: nope"])
    result = await WebBrowseTool().execute({"action": "preview", "path": "game.html"}, context(ws))
    assert isinstance(result, ToolResult)
    assert "console: TypeError: nope" in result.text and "uncaught: ReferenceError" in result.text
    assert "just a log" not in result.text
    assert '{"score": 3, "over": false}' in result.text
    assert result.media and result.media[0].data == b"png-bytes"
    assert made.page.url.startswith("http://127.0.0.1:") and made.page.url.endswith("/game.html")
    assert "conv1" in web_browse._previews


async def test_preview_without_state_or_errors(ws, browser):
    made = browser(state=None)
    made.page.console = []
    out = await WebBrowseTool().execute({"action": "preview", "path": "game.html", "screenshot": False}, context(ws))
    assert isinstance(out, str) and "not defined by the page" in out and not made.shots


async def test_a_failing_state_hook_is_reported_not_raised(ws, browser):
    browser(state=RuntimeError("hook broke"))
    result = await WebBrowseTool().execute({"action": "preview", "path": "game.html"}, context(ws))
    assert "hook broke" in result.text


async def test_the_page_can_only_load_from_the_preview_server(ws, browser):
    made = browser()
    await WebBrowseTool().execute({"action": "preview", "path": "game.html", "screenshot": False}, context(ws))
    (pattern, handler), = made.ctx.routes
    assert pattern == "**/*" and "delete window.WebSocket" in made.ctx.scripts[0]
    origin = made.page.url.rsplit("/", 1)[0]
    verdicts = []

    def route(url):
        async def cont():
            verdicts.append((url, "continue"))

        async def abort():
            verdicts.append((url, "abort"))

        return SimpleNamespace(request=SimpleNamespace(url=url), continue_=cont, abort=abort)

    for url in (origin + "/game.html", "http://169.254.169.254/latest", "http://127.0.0.1:11434/api/tags",
                origin + ".evil.com/x", "https://example.com/x.js"):
        await handler(route(url))
    assert verdicts == [(origin + "/game.html", "continue"), ("http://169.254.169.254/latest", "abort"),
                        ("http://127.0.0.1:11434/api/tags", "abort"), (origin + ".evil.com/x", "abort"),
                        ("https://example.com/x.js", "abort")]


@pytest.mark.parametrize("raw", ["../secret.html", "/etc/passwd", "notes.txt", ""])
async def test_preview_path_refusal_starts_nothing(ws, browser, raw):
    browser()
    out = await WebBrowseTool().execute({"action": "preview", "path": raw}, context(ws))
    assert out.startswith("Error:") and not web_browse._previews and not web_browse._sessions


async def test_closing_the_session_stops_the_server_and_the_page(ws, browser):
    made = browser()
    await WebBrowseTool().execute({"action": "preview", "path": "game.html", "screenshot": False}, context(ws))
    port = web_browse._previews["conv1"].server_address[1]
    await web_browse.close_session("conv1")
    assert made.ctx.closed and "conv1" not in web_browse._previews
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", port, timeout=1).request("GET", "/game.html")


async def test_page_on_the_preview_server_is_not_thrown_out_as_local(ws, browser):
    made = browser()
    await WebBrowseTool().execute({"action": "preview", "path": "game.html", "screenshot": False}, context(ws))
    assert await web_browse._left_public_web(made.page) is None
    other = SimpleNamespace(url="http://127.0.0.1:1/x")

    async def goto(url):
        other.url = url

    other.goto = goto
    assert "blocked" in (await web_browse._left_public_web(other) or "")
