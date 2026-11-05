"""Search by privacy mode: Kiwix and your own documents offline, your own SearXNG in local mode, open by id."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from click.testing import CliRunner

from micropaw import privacy
from micropaw.cli import main
from micropaw.config import SearchConfig
from micropaw.memory.store import MemoryStore
from micropaw.tools import web_search
from micropaw.tools.base import ToolContext, run_tool
from micropaw.tools.web_search import WebSearchTool

# Written by hand in the shape kiwix-serve gives for /search?format=xml (RSS items). TODO: replace with a reply
# recorded from a real kiwix-serve; the research notes do not describe the fields.
KIWIX_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
<title>Search: python</title><link>/search?pattern=python</link><description>Search results for python</description>
<opensearch:totalResults>3</opensearch:totalResults><opensearch:startIndex>0</opensearch:startIndex>
<item><title>Python (programming language)</title>
<link>/content/wikipedia_en_all_nopic_2026-06/A/Python_(programming_language)</link>
<description>&lt;b&gt;Python&lt;/b&gt; is a high-level language.</description></item>
<item><title>Other port</title><link>http://127.0.0.1:{other}/content/book/A/X</link><description>bad</description></item>
<item><title>Not content</title><link>/search?pattern=secret</link><description>bad</description></item>
</channel></rss>"""


class Kiwix(BaseHTTPRequestHandler):
    log: list[str] = []
    other = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).log.append(self.path)
        if self.path.startswith("/search"):
            body = KIWIX_XML.replace("{other}", str(type(self).other)).encode()
            kind = "application/xml"
        elif self.path.startswith("/content/redirect"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{type(self).other}/content/x")
            self.end_headers()
            return
        elif self.path.startswith("/content/"):
            body, kind = b"<html><head><style>p{}</style></head><body><h1>Python</h1><p>A snake &amp; a language.</p>" \
                         b"<script>evil()</script></body></html>", "text/html"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv


@pytest.fixture
def kiwix():
    Kiwix.log = []
    other = serve(Kiwix)  # a second loopback listener on another port: must never be reached
    Kiwix.other = other.server_port
    srv = serve(Kiwix)
    Kiwix.log = []
    yield SimpleNamespace(url=f"http://127.0.0.1:{srv.server_port}", other=other.server_port, port=srv.server_port)
    for s in (srv, other):
        s.shutdown()
        s.server_close()


@pytest.fixture
def mode():
    def set_mode(m):
        privacy._reset_for_tests()
        privacy.freeze(m)

    yield set_mode
    privacy._reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_tool():
    yield
    web_search.set_search_config(None)
    web_search._kiwix_ids.clear()


def ctx(conv="c1", platform="terminal", approve=True, marked=False) -> ToolContext:
    async def yes(tool, text):
        return True

    return ToolContext(conversation_id=conv, user_id="u", platform=platform, marked=marked,
                       approve=yes if approve else None)


def configure(**kw):
    cfg = SearchConfig(**kw)
    web_search.set_search_config(cfg)
    return cfg


# --- Kiwix ---

async def test_kiwix_request_has_pattern_book_xml_and_a_capped_page_length(kiwix, mode):
    mode("airgapped")
    configure(provider="offline", kiwix_url=kiwix.url, kiwix_books=["wikipedia_en_all_nopic_2026-06"])
    out = await WebSearchTool().execute({"query": "python", "max_results": 500}, ctx())
    q = parse_qs(urlparse(Kiwix.log[0]).query)
    assert q["pattern"] == ["python"] and q["books.name"] == ["wikipedia_en_all_nopic_2026-06"]
    assert q["format"] == ["xml"] and int(q["pageLength"][0]) <= 140
    assert out.startswith("Offline search, no internet used. Sources: Wikipedia (Kiwix snapshot 2026-06).")
    assert "**Python (programming language)**" in out and "[Wikipedia, 2026-06]" in out
    assert "<b>" not in out  # snippet markup is stripped


async def test_links_that_leave_the_content_path_or_the_server_are_never_offered(kiwix, mode):
    mode("airgapped")
    configure(provider="kiwix", kiwix_url=kiwix.url, kiwix_books=["b"])
    out = await WebSearchTool().execute({"query": "python"}, ctx())
    assert "Other port" not in out and "Not content" not in out
    assert len(web_search._kiwix_ids["c1"]) == 1


async def test_open_by_id_returns_plain_text_from_the_kiwix_host(kiwix, mode):
    mode("airgapped")
    configure(provider="kiwix", kiwix_url=kiwix.url, kiwix_books=["b"])
    out = await WebSearchTool().execute({"query": "python"}, ctx())
    rid = out.split("id: ")[1].split()[0]
    text = await WebSearchTool().execute({"action": "open", "id": rid}, ctx())
    assert "A snake & a language." in text and "evil()" not in text and "<p>" not in text
    assert Kiwix.log[-1].startswith("/content/")


@pytest.mark.parametrize("bad", ["k999", "http://127.0.0.1:1/content/x", "../../etc/passwd", ""])
async def test_open_takes_only_an_id_from_a_search_in_this_chat(kiwix, mode, bad):
    mode("airgapped")
    configure(provider="kiwix", kiwix_url=kiwix.url, kiwix_books=["b"])
    await WebSearchTool().execute({"query": "python"}, ctx())
    before = len(Kiwix.log)
    out = await WebSearchTool().execute({"action": "open", "id": bad}, ctx())
    assert out.startswith("Error") and len(Kiwix.log) == before
    other_chat = await WebSearchTool().execute({"action": "open", "id": "k1"}, ctx(conv="other"))
    assert other_chat.startswith("Error")


async def test_a_redirect_to_another_port_is_refused_on_open(kiwix, mode):
    mode("airgapped")
    configure(provider="kiwix", kiwix_url=kiwix.url)
    web_search._kiwix_ids["c1"] = {"k1": f"{kiwix.url}/content/redirect"}
    out = await WebSearchTool().execute({"action": "open", "id": "k1"}, ctx())
    assert out.startswith("Error") and not any("/content/x" in p for p in Kiwix.log)


async def test_a_kiwix_server_outside_the_network_is_not_contacted_in_airgapped(mode, monkeypatch):
    mode("airgapped")
    configure(provider="kiwix", kiwix_url="http://203.0.113.5:8080")
    out = await WebSearchTool().execute({"query": "python"}, ctx())
    assert out.startswith("Error") and "not on this computer or your network" in out


# --- your documents ---

@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    yield s
    await s.close()


async def test_documents_are_found_through_fts_without_embeddings(store, mode):
    mode("airgapped")
    await store.ingest_document("notes/boiler.md", "The boiler pressure should stay near 1.5 bar in winter.")
    await store.ingest_document("notes/other.md", "Nothing about heating here, only gardening tips.")
    configure(provider="offline")
    web_search.set_search_config(web_search._search_config, None, store)
    out = await WebSearchTool().execute({"query": "boiler pressure"}, ctx())
    assert "Sources: your documents." in out and "notes/boiler.md" in out and "gardening" not in out
    await store.ingest_document("notes/new.md", "A freshly added boiler manual.")  # the triggers keep it current
    assert "notes/new.md" in await WebSearchTool().execute({"query": "manual"}, ctx())
    await store.db.execute("DELETE FROM document_chunks WHERE source = 'notes/new.md'")
    assert "notes/new.md" not in await WebSearchTool().execute({"query": "manual"}, ctx())


async def test_no_offline_sources_says_what_to_do(store, mode):
    mode("airgapped")
    configure(provider="offline")
    web_search.set_search_config(web_search._search_config, None, store)
    out = await WebSearchTool().execute({"query": "x"}, ctx())
    assert out == "No offline sources yet: run paw docs add <folder> or set search.kiwix_url."


def test_paw_docs_add_indexes_a_folder_and_skips_big_files(tmp_path, monkeypatch):
    import micropaw.config as config_mod
    monkeypatch.setattr(config_mod, "DB_FILE", tmp_path / "data.db")
    import micropaw.cli as cli
    monkeypatch.setattr(cli, "DB_FILE", tmp_path / "data.db")
    folder = tmp_path / "docs"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_text("alpha bravo charlie")
    (folder / "sub" / "b.md").write_text("# Title\n\ndelta echo")
    (folder / "c.png").write_bytes(b"\x89PNG")
    (folder / "huge.txt").write_bytes(b"x" * (20 * 1024 * 1024))
    r = CliRunner().invoke(main, ["docs", "add", str(folder)])
    assert r.exit_code == 0, r.output
    assert "Indexed 2 file(s)" in r.output and "huge.txt" in r.output and "too big" in r.output
    import asyncio

    async def check():
        s = MemoryStore(tmp_path / "data.db")
        await s.init()
        try:
            from micropaw import docsearch
            return [h["source"] for h in await docsearch.search(s, "delta")]
        finally:
            await s.close()

    assert asyncio.run(check()) == [str(folder / "sub" / "b.md")]


# --- the modes ---

def test_airgapped_turns_auto_into_offline_and_refuses_every_online_provider():
    for p in ("auto", "offline"):
        assert web_search.plan(SearchConfig(provider=p), "airgapped", False).backends == ["kiwix", "docs"]
    for p in ("searxng", "brave", "duckduckgo"):
        got = web_search.plan(SearchConfig(provider=p, searxng_url="http://x", brave_api_key="k"), "airgapped", False)
        assert not got.backends and "internet" in got.refusal
    assert "turned off" in web_search.plan(SearchConfig(provider="off"), "normal", False).refusal


async def test_airgapped_search_only_connects_to_loopback(kiwix, mode, monkeypatch):
    mode("airgapped")
    configure(provider="auto", kiwix_url=kiwix.url, kiwix_books=["b"])
    seen = []
    real = socket.socket.connect

    def spy(self, address):
        seen.append(address)
        return real(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy)
    await WebSearchTool().execute({"query": "python"}, ctx())
    assert seen and all(a[0] in ("127.0.0.1", "::1") for a in seen)


def test_local_mode_asks_for_every_online_query_and_shows_it_in_full(mode):
    mode("local")
    cfg = SearchConfig(searxng_url="http://search.home.lan:8888", proxy="socks5h://127.0.0.1:9050")
    web_search.set_search_config(cfg)
    got = []
    for q in ("first secret query", "second one"):
        got.append(WebSearchTool().needs_approval({"query": q}, ctx()))
    assert got[0] == ("Search via your SearXNG at search.home.lan:8888 (through socks5h://127.0.0.1:9050): "
                      "first secret query")
    assert got[1].endswith(": second one")  # asked again: no session allow


def test_a_proxy_login_is_never_shown_in_the_approval(mode):
    mode("local")
    web_search.set_search_config(SearchConfig(searxng_url="http://s:1", proxy="socks5h://user:pw@127.0.0.1:9050"))
    ask = WebSearchTool().needs_approval({"query": "q"}, ctx())
    assert "pw" not in ask and "user" not in ask


async def test_unattended_local_run_gets_offline_results_and_a_note(kiwix, mode):
    mode("local")
    configure(provider="searxng", searxng_url="http://127.0.0.1:1", kiwix_url=kiwix.url, kiwix_books=["b"])
    for c in (ctx(approve=None), ctx(platform="cron")):
        tool = WebSearchTool()
        assert tool.needs_approval({"query": "python"}, c) is None
        out = await run_tool(tool, {"query": "python"}, c)
        assert "only offline sources were used" in out and "Offline search, no internet used" in out


async def test_a_down_searxng_is_reported_and_no_other_service_is_tried(mode, monkeypatch):
    mode("local")
    configure(provider="auto", searxng_url="http://127.0.0.1:1")
    ddg = []

    async def never(*a, **k):
        ddg.append(a)

    monkeypatch.setattr(web_search, "_search_duckduckgo", never)
    out = await WebSearchTool().execute({"query": "python"}, ctx())
    assert "Your search server at 127.0.0.1:1 is unreachable" in out and not ddg


async def test_a_socks_proxy_without_its_package_gives_the_install_hint(mode, monkeypatch):
    mode("local")
    configure(provider="searxng", searxng_url="http://127.0.0.1:1", proxy="socks5h://127.0.0.1:9050")

    def boom(*a, **k):
        raise ImportError("socksio")

    monkeypatch.setattr(web_search.net, "client", boom)
    assert "pip install micropaw[tor]" in await WebSearchTool().execute({"query": "q"}, ctx())


@pytest.mark.parametrize("provider", ["brave", "duckduckgo"])
async def test_local_mode_refuses_search_services_that_are_not_your_own(mode, provider):
    mode("local")
    configure(provider=provider, brave_api_key="k")
    out = await WebSearchTool().execute({"query": "q"}, ctx())
    assert out.startswith("Error") and "own SearXNG" in out


def test_normal_mode_asks_only_once_outside_content_was_read():
    configure(provider="auto")
    assert WebSearchTool().needs_approval({"query": "q"}, ctx()) is None
    assert "DuckDuckGo" in WebSearchTool().needs_approval({"query": "q"}, ctx(marked=True))


def test_normal_mode_has_no_silent_fallback_for_a_chosen_provider():
    assert web_search.plan(SearchConfig(provider="auto"), "normal", False).backends == ["public", "duckduckgo"]
    own = web_search.plan(SearchConfig(provider="auto", searxng_url="http://x"), "normal", False)
    assert own.backends == ["searxng"]
    assert web_search.plan(SearchConfig(provider="auto", brave_api_key="k"), "normal", False).backends == ["brave"]
    assert web_search.plan(SearchConfig(provider="searxng"), "normal", False).refusal


def test_the_approval_gate_never_offers_always_for_a_search():
    from micropaw.gateway import _Approvals
    assert _Approvals._rule(SimpleNamespace(), "web_search", "Search via x: q", False) == ""
    assert _Approvals._rule(SimpleNamespace(), "shell", "ls", False) != ""


# --- a configured Kiwix is searched in every mode ---

@pytest.mark.parametrize("m,provider", [("normal", "auto"), ("normal", "searxng"), ("local", "auto"),
                                        ("local", "searxng"), ("airgapped", "auto")])
async def test_a_configured_kiwix_is_queried_in_every_mode_before_the_online_search(kiwix, mode, monkeypatch, m,
                                                                                    provider):
    mode(m)
    configure(provider=provider, searxng_url="http://127.0.0.1:9", kiwix_url=kiwix.url, kiwix_books=["b"])
    asked = []

    async def fake_searx(query, n, base_url=None, proxy=""):
        asked.append(base_url)
        return "Search results for 'python' (via your SearXNG at 127.0.0.1:9):\n\n- online hit"

    monkeypatch.setattr(web_search, "_search_searxng", fake_searx)
    out = await WebSearchTool().execute({"query": "python"}, ctx())
    assert any(p.startswith("/search") for p in Kiwix.log)
    assert "**Python (programming language)**" in out
    if m == "airgapped":
        assert not asked and "online hit" not in out
    else:
        assert asked and out.index("Python (programming language)") < out.index("online hit")


def test_the_local_approval_says_offline_sources_come_first_only_when_kiwix_is_set(mode):
    mode("local")
    configure(provider="searxng", searxng_url="http://s:1", kiwix_url="http://127.0.0.1:8080")
    assert WebSearchTool().needs_approval({"query": "q"}, ctx()).startswith("Search your offline sources, then via ")
    configure(provider="searxng", searxng_url="http://s:1")
    assert WebSearchTool().needs_approval({"query": "q"}, ctx()).startswith("Search via ")


async def test_the_offline_header_only_says_no_internet_when_the_server_is_on_your_network(kiwix, mode, monkeypatch):
    mode("normal")
    configure(provider="kiwix", kiwix_url=kiwix.url, kiwix_books=["b"])
    assert "no internet used" in await WebSearchTool().execute({"query": "python"}, ctx())
    monkeypatch.setattr(privacy, "is_local_url", lambda url: False)
    out = await WebSearchTool().execute({"query": "python"}, ctx())
    assert "no internet used" not in out and "is not on your network" in out
