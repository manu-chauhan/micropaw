"""Web search: offline sources (Kiwix, your own documents), your own SearXNG, or the public web, by privacy mode."""

from __future__ import annotations

import itertools
import logging
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree.ElementTree import ParseError, fromstring

import httpx

from micropaw import net, privacy, webfetch
from micropaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public SearXNG instances (shuffled on each search for load balancing)
# These all support /search?q=...&format=json
# ---------------------------------------------------------------------------

_SEARXNG_INSTANCES = [
    "https://search.sapti.me",
    "https://searx.be",
    "https://search.bus-hit.me",
    "https://searx.tiekoetter.com",
    "https://search.ononoki.org",
]

_SEARXNG_TIMEOUT = 8.0
_DDG_TIMEOUT = 15.0
_BRAVE_TIMEOUT = 10.0
_KIWIX_TIMEOUT = 15.0
_MAX_SEARXNG_TRIES = 3
KIWIX_MAX_BYTES = 2_000_000
KIWIX_MAX_BOOKS = 5
OPEN_LIMIT = 12_000
_ONLINE = frozenset({"searxng", "public", "brave", "duckduckgo"})
_UNATTENDED = frozenset({"cron", "heartbeat", "system"})

# Injected at startup from config
_search_config: Any = None
_store: Any = None
# conversation id -> {result id: article URL}. Only links the Kiwix server itself returned, and only valid ones.
_kiwix_ids: dict[str, dict[str, str]] = {}
_MAX_IDS = 60
_id_counter = itertools.count(1)


def set_search_config(config: Any, privacy_config: Any = None, store: Any = None) -> None:
    global _search_config, _store
    _search_config = config
    _store = store
    webfetch.set_privacy_config(privacy_config, getattr(config, "proxy", ""))


# ---------------------------------------------------------------------------
# Which backends may answer, by mode
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    backends: list[str]
    refusal: str = ""
    note: str = ""

    @property
    def online(self) -> bool:
        return any(b in _ONLINE for b in self.backends)

    @property
    def offline(self) -> bool:
        return any(b in ("kiwix", "docs") for b in self.backends)


def plan(cfg: Any, mode: str, unattended: bool) -> Plan:
    """The backends for this config, mode and run. Anything online is only here when the mode allows it."""
    p = cfg.provider
    offline = ["kiwix", "docs"]
    if p == "off":
        return Plan([], "Web search is turned off. Set search.provider to turn it on.")
    if p in ("offline", "kiwix", "docs"):
        return Plan(offline if p == "offline" else [p])
    if mode == "airgapped":
        if p == "auto":
            return Plan(offline)
        return Plan([], f"Search provider '{p}' sends queries to the internet. Air-gapped mode only searches "
                        "offline sources: set search.provider to offline, kiwix or docs.")
    if mode == "local":
        if p in ("brave", "duckduckgo"):
            return Plan([], f"Search provider '{p}' sends queries to the internet. Local mode only allows your "
                            "own SearXNG (search.searxng_url) and offline sources.")
        if not cfg.searxng_url:
            if p == "searxng":
                return Plan([], "Set search.searxng_url to your own SearXNG server to search online in local mode.")
            return Plan(offline)
        if unattended:
            return Plan(offline, note="Nobody is here to approve an online search, so only offline sources were used.")
        # offline sources first, so a Kiwix server or your documents are never left unused
        return Plan([*(["kiwix"] if cfg.kiwix_url else []), "docs", "searxng"])
    kiwix = ["kiwix"] if cfg.kiwix_url and p in ("auto", "searxng") else []
    if p == "auto":
        if cfg.brave_api_key:
            return Plan([*kiwix, "brave"])
        return Plan([*kiwix, *(["searxng"] if cfg.searxng_url else ["public", "duckduckgo"])])
    if p == "searxng" and not cfg.searxng_url:
        return Plan([], "Set search.searxng_url to your SearXNG server, or use provider auto for public instances.")
    if p == "brave" and not cfg.brave_api_key:
        return Plan([], "Set search.brave_api_key to use Brave Search.")
    return Plan([*kiwix, p])


def _unattended(context: ToolContext) -> bool:
    return context.approve is None or context.platform in _UNATTENDED


def _why(e: Exception) -> str:
    """The error type and HTTP status only: httpx error text holds the whole URL, which holds the query."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return type(e).__name__ + (f" {status}" if status else "")


def _proxy_label(proxy: str) -> str:
    p = urlparse(proxy)
    return f"through {p.scheme}://{p.hostname}" + (f":{p.port}" if p.port else "")


def _host(url: str) -> str:
    return urlparse(url).netloc.rpartition("@")[2]


# ---------------------------------------------------------------------------
# Online backends
# ---------------------------------------------------------------------------

def _format_searx(query: str, data: dict, max_results: int, via: str) -> str | None:
    results = []
    for item in data.get("results", [])[:max_results]:
        title, link, snippet = item.get("title", ""), item.get("url", ""), item.get("content", "")
        if title and link:
            results.append(f"- **{title}**\n  {link}" + (f"\n  {snippet}" if snippet else ""))
    return f"Search results for '{query}' (via {via}):\n\n" + "\n\n".join(results) if results else None


async def _search_searxng(query: str, max_results: int, base_url: str | None = None, proxy: str = "") -> str | None:
    """Search via SearXNG. Returns formatted results or None on failure.

    With `base_url` it is your own server: tried once, through `proxy` when one is set, and a failure is
    reported by the caller. Without it, public instances are tried in turn.
    """
    import random

    urls = [base_url.rstrip("/")] if base_url else random.sample(_SEARXNG_INSTANCES, _MAX_SEARXNG_TRIES)
    for url in urls:
        try:
            via = {"proxy": proxy} if base_url and proxy else {}
            async with net.client(url, timeout=_SEARXNG_TIMEOUT, **via) as client:
                resp = await client.get(f"{url}/search", params={"q": query, "format": "json", "language": "en"},
                                        headers={"Accept": "application/json"})
                resp.raise_for_status()
                data = resp.json()
            host = urlparse(url).hostname or url
            text = _format_searx(query, data, max_results,
                                 f"your SearXNG at {_host(url)}" if base_url else f"{host}, a public SearXNG")
            if text:
                return text
            logger.debug("SearXNG %s returned no results", url)
        except ImportError:
            raise
        except Exception as e:
            logger.debug("SearXNG %s failed: %s", _host(url), _why(e))
    return None


async def _search_brave(query: str, max_results: int, api_key: str) -> str | None:
    """Search via Brave Search API. Returns text, or None when it is rate limited or cannot be reached."""
    try:
        async with net.client("https://api.search.brave.com", timeout=_BRAVE_TIMEOUT) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max_results},
                headers={"Accept": "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": api_key},
            )
            resp.raise_for_status()
            data = resp.json()
        results = []
        for item in data.get("web", {}).get("results", [])[:max_results]:
            title, link, snippet = item.get("title", ""), item.get("url", ""), item.get("description", "")
            if title and link:
                results.append(f"- **{title}**\n  {link}" + (f"\n  {snippet}" if snippet else ""))
        if results:
            return f"Search results for '{query}' (via Brave):\n\n" + "\n\n".join(results)
        return f"No results found for: {query}"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "Error: Invalid Brave API key. Check your search.brave_api_key config."
        if e.response.status_code == 429:
            return None
        return f"Brave Search error: HTTP {e.response.status_code}"
    except Exception as e:
        logger.debug("Brave Search failed: %s", _why(e))
        return None


async def _search_duckduckgo(query: str, max_results: int) -> str:
    """Search via DuckDuckGo Lite (HTML). Always returns a string (never None)."""
    from urllib.parse import parse_qs, unquote

    try:
        async with net.client("https://lite.duckduckgo.com", timeout=_DDG_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get("https://lite.duckduckgo.com/lite/", params={"q": query},
                                    headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
        text = resp.text
        titles = re.findall(r"class='result-link'>([^<]+)</a>", text)
        raw_urls = re.findall(r'href="([^"]+)"[^>]*class=[\'"]result-link', text)
        snippets = re.findall(r"class='result-snippet'>([^<]*(?:<b>[^<]*</b>[^<]*)*)", text)
        results = []
        for i in range(min(max_results, len(titles))):
            title = titles[i].strip()
            url = unquote(parse_qs(urlparse(raw_urls[i]).query).get("uddg", [""])[0]) if i < len(raw_urls) else ""
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
            if title:
                results.append(f"- **{title}**" + (f"\n  {url}" if url else "") + (f"\n  {snippet}" if snippet else ""))
        if results:
            return f"Search results for '{query}' (via DuckDuckGo):\n\n" + "\n\n".join(results)
        return f"No results found for: {query}"
    except Exception as e:
        return f"Search error: {e}"


# ---------------------------------------------------------------------------
# Offline backends: Kiwix and your own documents
# ---------------------------------------------------------------------------

def book_label(book: str) -> tuple[str, str]:
    """("Wikipedia", "2026-06") from "wikipedia_en_all_nopic_2026-06". The date is "" when there is none."""
    m = re.search(r"_(\d{4}-\d{2})$", book)
    base = book[: m.start()] if m else book
    return base.split("_")[0].capitalize() if base.startswith(("wikipedia", "wiktionary", "devdocs")) else base, \
        m.group(1) if m else ""


def _kiwix_check(kiwix_url: str):
    """A check for fetch_clean: only the configured scheme, host and port, and only below <root>/content/."""
    base = urlparse(kiwix_url)
    root = base.path.rstrip("/")

    def check(url: str) -> str | None:
        p = urlparse(url)
        same = (p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)) == \
               (base.scheme, base.hostname, base.port or (443 if base.scheme == "https" else 80))
        if not same or not p.path.startswith(f"{root}/content/") or ".." in p.path.split("/"):
            return "Only articles on your Kiwix server can be opened."
        return None

    return check


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_kiwix(data: bytes) -> list[dict]:
    """[{title, link, snippet}] from a kiwix-serve /search?format=xml reply (RSS items). Never raises."""
    if b"<!DOCTYPE" in data[:2048].upper() or b"<!ENTITY" in data.upper():
        return []  # no entity tricks from a server we do not control
    try:
        root = fromstring(data)
    except ParseError:
        return []
    hits = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "item":
            continue
        fields = {c.tag.rsplit("}", 1)[-1]: (c.text or "") for c in item}
        if fields.get("title") and fields.get("link"):
            hits.append({"title": _strip(fields["title"]), "link": fields["link"].strip(),
                         "snippet": _strip(fields.get("description", ""))[:400]})
    return hits


async def _search_kiwix(query: str, max_results: int, cfg: Any, conv_id: str) -> tuple[list[str], list[str]]:
    """(result blocks, source names). One request per configured book, at most KIWIX_MAX_BOOKS."""
    base = cfg.kiwix_url.rstrip("/")
    check = _kiwix_check(base)
    books = cfg.kiwix_books[:KIWIX_MAX_BOOKS] or [""]
    blocks, sources = [], []
    ids = _kiwix_ids.setdefault(conv_id, {})
    while len(_kiwix_ids) > 100:
        _kiwix_ids.pop(next(iter(_kiwix_ids)))
    for book in books:
        params = {"pattern": query, "format": "xml", "pageLength": min(max(max_results, 1), 140)}
        if book:
            params["books.name"] = book
        try:
            async with net.client(base, timeout=_KIWIX_TIMEOUT) as client:
                async with client.stream("GET", f"{base}/search", params=params) as resp:
                    resp.raise_for_status()
                    data = bytearray()
                    async for chunk in resp.aiter_bytes():
                        data += chunk
                        if len(data) > KIWIX_MAX_BYTES:
                            break
        except httpx.HTTPError as e:
            logger.debug("Kiwix at %s failed: %s", _host(base), _why(e))
            sources.append(f"{book or 'Kiwix'} (not answering at {_host(base)})")
            continue
        name, date = book_label(book or "Kiwix")
        sources.append(f"{name} (Kiwix snapshot {date})" if date else name)
        for hit in parse_kiwix(bytes(data[:KIWIX_MAX_BYTES]))[:max_results]:
            url = urljoin(base + "/", hit["link"])
            if check(url):
                continue  # a link that leaves the Kiwix content path is never offered
            if len(ids) >= _MAX_IDS:
                ids.pop(next(iter(ids)))
            rid = f"k{next(_id_counter)}"
            ids[rid] = url
            blocks.append(f"- **{hit['title']}**  [{name}{', ' + date if date else ''}]  id: {rid}"
                          + (f"\n  {hit['snippet']}" if hit["snippet"] else ""))
    return blocks, sources


async def _search_docs(query: str, max_results: int, context: ToolContext) -> list[str]:
    if _store is None:
        return []
    from micropaw import docsearch
    users = tuple(dict.fromkeys((context.memory_user, "_global")))
    hits = await docsearch.search(_store, query, max_results, users)
    return [f"- **{h['source']}**  [your documents]\n  {_strip(h['snippet'])}" for h in hits]


async def _search_offline(query: str, max_results: int, cfg: Any, backends: list[str], context: ToolContext,
                          quiet: bool = False) -> str:
    """`quiet`: an online search follows, so "no offline sources yet" is not worth saying."""
    blocks: list[str] = []
    sources: list[str] = []
    if "kiwix" in backends and cfg.kiwix_url:
        kb, ks = await _search_kiwix(query, max_results, cfg, context.conversation_id)
        blocks += kb
        sources += ks
    if "docs" in backends and _store is not None:
        from micropaw import docsearch
        if await docsearch.has_documents(_store, tuple(dict.fromkeys((context.memory_user, "_global")))):
            blocks += await _search_docs(query, max_results, context)
            sources.append("your documents")
    if not sources:
        return "" if quiet else "No offline sources yet: run paw docs add <folder> or set search.kiwix_url."
    off_net = "kiwix" in backends and cfg.kiwix_url and not privacy.is_local_url(cfg.kiwix_url)
    head = (f"Offline sources, searched on {_host(cfg.kiwix_url)}, which is not on your network. "
            if off_net else "Offline search, no internet used. ") + f"Sources: {', '.join(sources)}."
    return f"{head}\n\n" + ("\n\n".join(blocks[:max_results * 2]) if blocks else f"No results found for: {query}")


async def _open_article(rid: str, context: ToolContext) -> str:
    cfg = _search_config
    url = _kiwix_ids.get(context.conversation_id, {}).get(rid)
    if not cfg or not cfg.kiwix_url:
        return "Error: no Kiwix server is set up (search.kiwix_url)."
    if not url:
        return "Error: open takes the id of a result from a search in this chat, for example id k1."
    if privacy.local_only() and not privacy.is_local_url(cfg.kiwix_url):
        return "Error: the Kiwix server is not on this computer or your network, so it was not contacted."
    try:
        final, resp, body = await webfetch.fetch_clean(url, check=_kiwix_check(cfg.kiwix_url.rstrip("/")),
                                                       max_bytes=KIWIX_MAX_BYTES, timeout=_KIWIX_TIMEOUT)
    except webfetch.FetchError as e:
        return f"Error: {e}"
    if resp.status_code != 200:
        return f"Error: the Kiwix server answered HTTP {resp.status_code}."
    return f"Article from Kiwix ({urlparse(final).path.split('/content/', 1)[-1]}):\n\n" \
           + webfetch.html_to_text(body.decode(resp.encoding or "utf-8", errors="replace"), OPEN_LIMIT)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class WebSearchTool(Tool):
    name = "web_search"
    marks = True
    description = ("Search for information: titles, links and snippets. "
                   "Offline results have an id; action 'open' with it reads the article.")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer"},
            "action": {"type": "string", "enum": ["search", "open"]},
            "id": {"type": "string", "description": "Result id, for open"},
        },
        "required": [],
    }

    @staticmethod
    def _plan(context: ToolContext) -> Plan:
        from micropaw.config import SearchConfig
        return plan(_search_config or SearchConfig(), privacy.mode(), _unattended(context))

    def needs_approval(self, args: dict[str, Any], context: ToolContext) -> str | None:
        """Every online query in local mode, and in normal mode once outside content was read. Never "always"."""
        if args.get("action") == "open":
            return None
        p = self._plan(context)
        if not p.online or (privacy.mode() == "normal" and not context.marked):
            return None
        cfg = _search_config
        host = _host(cfg.searxng_url) if cfg and cfg.searxng_url else "?"
        route = f"{_proxy_label(cfg.proxy)}" if cfg and cfg.proxy else "direct"
        local = bool(cfg and cfg.searxng_url and privacy.is_local_url(cfg.searxng_url))
        where = {"searxng": f"your SearXNG at {host} ({route})" if local  # a public address is not "your own server"
                 else f"the internet ({host}, {route})",
                 "public": "public SearXNG instances, then DuckDuckGo", "brave": "Brave Search",
                 "duckduckgo": "DuckDuckGo"}[next(b for b in p.backends if b in _ONLINE)]
        first = "your offline sources, then " if "kiwix" in p.backends else ""
        return f"Search {first}via {where}: {args.get('query', '')}"

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if args.get("action") == "open":
            return await _open_article(str(args.get("id") or ""), context)
        query = str(args.get("query") or "").strip()
        if not query:
            return "Error: a search needs a 'query'."
        max_results = min(int(args.get("max_results") or 5), 20)
        from micropaw.config import SearchConfig
        cfg = _search_config or SearchConfig()
        p = self._plan(context)
        if p.refusal:
            return f"Error: {p.refusal}"
        if privacy.local_only() and cfg.kiwix_url and not privacy.is_local_url(cfg.kiwix_url):
            return "Error: the Kiwix server is not on this computer or your network, so it was not contacted."
        offline = await _search_offline(query, max_results, cfg, p.backends, context, quiet=p.online) \
            if p.offline else ""
        online = await self._search_online(query, max_results, cfg, p) if p.online else ""
        if online.startswith("Error:"):
            return online
        return "\n\n".join(x for x in (p.note, offline, online) if x)

    @staticmethod
    async def _search_online(query: str, max_results: int, cfg: Any, p: Plan) -> str:
        if "brave" in p.backends:
            out = await _search_brave(query, max_results, cfg.brave_api_key) or (
                "Brave Search is busy or could not be reached. No other search service was tried.")
        elif "duckduckgo" in p.backends and "public" not in p.backends:
            out = await _search_duckduckgo(query, max_results)
        elif "searxng" in p.backends:
            try:
                out = await _search_searxng(query, max_results, cfg.searxng_url, cfg.proxy) or (
                    f"Your search server at {_host(cfg.searxng_url)} is unreachable or returned nothing. "
                    "No other search service was tried.")
            except ImportError:
                return "Error: a SOCKS proxy needs an extra package. Run: pip install micropaw[tor]"
        else:  # public instances, then DuckDuckGo: only for provider auto with nothing configured
            out = await _search_searxng(query, max_results) or await _search_duckduckgo(query, max_results)
        return out
