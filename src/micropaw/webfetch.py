"""Clean web fetch: no cookies, no referrer, no tracking parameters, redirects checked one hop at a time."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

from micropaw import net

USER_AGENT = "Mozilla/5.0 (compatible; MicroPaw)"
MAX_REDIRECTS = 5
_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|yclid|_hsenc|_hsmi)$", re.I)


_privacy_config = None
_proxy = ""


def set_privacy_config(cfg, proxy: str = "") -> None:
    global _privacy_config, _proxy
    _privacy_config, _proxy = cfg, proxy if isinstance(proxy, str) else ""


def proxy() -> str:
    """search.proxy (socks5h:// or socks5://), "" when none."""
    return _proxy


def allowed(feature: str) -> bool:
    """True when the owner named the feature (web_fetch, web_browse) in privacy.allow."""
    return feature in (getattr(_privacy_config, "allow", None) or [])


class FetchError(Exception):
    """A plain reason, safe to show to the owner and the model."""


def clean_url(url: str) -> str:
    """The URL without tracking parameters and without the fragment."""
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return urlunparse(p._replace(query=query, fragment=""))


def odd_segments(url: str) -> list[str]:
    """Long path or query pieces that could carry data out. For the approval text, not blocked."""
    p = urlparse(url)
    pieces = [s for s in p.path.split("/") if s] + [v for _, v in parse_qsl(p.query)]
    return [s[:40] + "..." for s in pieces if len(s) >= 40]


Check = Callable[[str], Awaitable[str | None] | str | None]
Hop = Callable[[str], Awaitable[bool]]


async def _ok(check: Check, url: str) -> str | None:
    out = check(url)
    return await out if hasattr(out, "__await__") else out  # type: ignore[misc]


async def fetch_clean(
    url: str, *, check: Check, max_bytes: int = 2_000_000, timeout: float = 15.0, via: str | None = None,
    approve_new_host: Hop | None = None, **client_kwargs,
) -> tuple[str, httpx.Response, bytes]:
    """GET `url` and return (final URL, response, body up to max_bytes).

    A new client per call (no cookie carried over), every cookie refused, no Referer, a fixed User-Agent.
    `check(url)` returns an error text or None for the first URL and for every redirect hop; a hop to
    another host also needs `approve_new_host` to say yes. Raises FetchError.
    """
    url = clean_url(url)
    extra = {"proxy": via} if via else {}
    async with net.client(url, timeout=timeout, follow_redirects=False,
                          headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*;q=0.5"},
                          **extra, **client_kwargs) as client:
        client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        for hop in range(MAX_REDIRECTS + 1):
            if err := await _ok(check, url):
                raise FetchError(err)
            try:
                async with client.stream("GET", url) as resp:
                    if resp.is_redirect and (loc := resp.headers.get("location")):
                        nxt = clean_url(urljoin(url, loc))
                        if urlparse(nxt).netloc != urlparse(url).netloc and approve_new_host is not None \
                                and not await approve_new_host(nxt):
                            raise FetchError(f"The page redirects to {urlparse(nxt).netloc}, which was not approved.")
                        url = nxt
                        continue
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            break
                    return url, resp, bytes(body[:max_bytes])
            except httpx.HTTPError as e:
                raise FetchError(f"Could not fetch {urlparse(url).netloc}: {type(e).__name__}") from e
    raise FetchError("Too many redirects.")


def html_to_text(html: str, limit: int = 12_000) -> str:
    """Readable text from HTML: scripts, styles and tags dropped, entities decoded."""
    from html import unescape
    html = re.sub(r"(?is)<(script|style|noscript|template|svg).*?</\1>|<!--.*?-->", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|pre|table)>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit] + ("\n... (truncated)" if len(text) > limit else "")
