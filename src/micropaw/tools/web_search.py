"""Web search tool — SearXNG (public instances) → DuckDuckGo fallback, or Brave with API key."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

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
_MAX_SEARXNG_TRIES = 3

# Injected at startup from config
_search_config: Any = None


def set_search_config(config: Any) -> None:
    global _search_config
    _search_config = config


# ---------------------------------------------------------------------------
# Search backends
# ---------------------------------------------------------------------------

async def _search_searxng(query: str, max_results: int, base_url: str | None = None) -> str | None:
    """Search via SearXNG. Returns formatted results or None on failure."""
    import random

    if base_url:
        # Self-hosted instance — try once
        urls = [base_url.rstrip("/")]
    else:
        # Public instances — shuffle and try up to 3
        urls = random.sample(_SEARXNG_INSTANCES, min(_MAX_SEARXNG_TRIES, len(_SEARXNG_INSTANCES)))

    for url in urls:
        try:
            async with httpx.AsyncClient(timeout=_SEARXNG_TIMEOUT) as client:
                resp = await client.get(
                    f"{url}/search",
                    params={"q": query, "format": "json", "language": "en"},
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()

            results = []
            for item in data.get("results", [])[:max_results]:
                title = item.get("title", "")
                link = item.get("url", "")
                snippet = item.get("content", "")
                if title and link:
                    entry = f"- **{title}**\n  {link}"
                    if snippet:
                        entry += f"\n  {snippet}"
                    results.append(entry)

            if results:
                source = urlparse(url).hostname or url
                return f"Search results for '{query}' (via {source}):\n\n" + "\n\n".join(results)

            # Got a response but no results — try next instance
            logger.debug("SearXNG %s returned no results for '%s'", url, query)

        except Exception as e:
            logger.debug("SearXNG %s failed: %s", url, e)
            continue

    return None  # All instances failed


async def _search_brave(query: str, max_results: int, api_key: str) -> str | None:
    """Search via Brave Search API. Returns formatted results or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=_BRAVE_TIMEOUT) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max_results},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("web", {}).get("results", [])[:max_results]:
            title = item.get("title", "")
            link = item.get("url", "")
            snippet = item.get("description", "")
            if title and link:
                entry = f"- **{title}**\n  {link}"
                if snippet:
                    entry += f"\n  {snippet}"
                results.append(entry)

        if results:
            return f"Search results for '{query}' (via Brave):\n\n" + "\n\n".join(results)

        return f"No results found for: {query}"

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "Error: Invalid Brave API key. Check your search.brave_api_key config."
        if e.response.status_code == 429:
            logger.warning("Brave Search rate limited")
            return None  # Will fall through to next backend
        return f"Brave Search error: HTTP {e.response.status_code}"
    except Exception as e:
        logger.debug("Brave Search failed: %s", e)
        return None


async def _search_duckduckgo(query: str, max_results: int) -> str:
    """Search via DuckDuckGo Lite (HTML). Always returns a string (never None)."""
    import re
    from urllib.parse import unquote, urlparse, parse_qs

    try:
        async with httpx.AsyncClient(timeout=_DDG_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(
                "https://lite.duckduckgo.com/lite/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()

        text = resp.text
        titles = re.findall(r"class='result-link'>([^<]+)</a>", text)
        raw_urls = re.findall(r'href="([^"]+)"[^>]*class=[\'"]result-link', text)
        snippets = re.findall(r"class='result-snippet'>([^<]*(?:<b>[^<]*</b>[^<]*)*)", text)

        results = []
        for i in range(min(max_results, len(titles))):
            title = titles[i].strip()
            # Extract real URL from DDG redirect
            url = ""
            if i < len(raw_urls):
                parsed = parse_qs(urlparse(raw_urls[i]).query)
                url = unquote(parsed.get("uddg", [""])[0])
            snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
            if title:
                entry = f"- **{title}**"
                if url:
                    entry += f"\n  {url}"
                if snippet:
                    entry += f"\n  {snippet}"
                results.append(entry)

        if results:
            return f"Search results for '{query}' (via DuckDuckGo):\n\n" + "\n\n".join(results)

        return f"No results found for: {query}"

    except Exception as e:
        return f"Search error: {e}"


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web and return results with titles, URLs, and snippets."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
            "max_results": {"type": "integer", "description": "Maximum results to return (default: 5)"},
        },
        "required": ["query"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        query = args["query"]
        max_results = min(args.get("max_results", 5), 20)

        # Read search config (injected at startup)
        cfg = _search_config
        provider = cfg.provider if cfg else "auto"
        brave_key = cfg.brave_api_key if cfg else ""
        searxng_url = cfg.searxng_url if cfg else ""

        # Brave — if explicitly configured or has a key
        if provider == "brave" or (provider == "auto" and brave_key):
            if brave_key:
                result = await _search_brave(query, max_results, brave_key)
                if result:
                    return result
                # Brave failed — fall through to SearXNG/DDG
                logger.info("Brave Search failed, falling back to SearXNG")

        # DuckDuckGo only — if explicitly configured
        if provider == "duckduckgo":
            return await _search_duckduckgo(query, max_results)

        # SearXNG (default path for "auto" and "searxng")
        result = await _search_searxng(query, max_results, base_url=searxng_url or None)
        if result:
            return result

        # All SearXNG instances failed — fall back to DuckDuckGo
        logger.info("All SearXNG instances failed for '%s', falling back to DuckDuckGo", query)
        return await _search_duckduckgo(query, max_results)
