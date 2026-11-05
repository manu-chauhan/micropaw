"""Clean fetch: no tracking parameters, no cookies, no referrer, every redirect hop checked."""

from __future__ import annotations

import httpx
import pytest

from micropaw import webfetch


def ok(url):
    return None


def transport(handler, seen):
    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped)


def test_tracking_parameters_and_the_fragment_are_removed():
    url = "https://example.com/a?utm_source=x&id=7&fbclid=1&gclid=2&dclid=3&msclkid=4&mc_cid=5&mc_eid=6" \
          "&igshid=7&yclid=8&_hsenc=9&_hsmi=10&UTM_Medium=m&q=a+b#section"
    assert webfetch.clean_url(url) == "https://example.com/a?id=7&q=a+b"
    assert webfetch.clean_url("https://example.com/a") == "https://example.com/a"


def test_long_path_pieces_are_pointed_out_for_the_approval():
    assert webfetch.odd_segments("https://e.com/" + "x" * 50 + "?k=short") == ["x" * 40 + "..."]
    assert webfetch.odd_segments("https://e.com/a/b?k=v") == []


async def test_the_request_has_no_tracking_no_referer_no_cookie_and_a_fixed_agent():
    seen = []
    final, resp, body = await webfetch.fetch_clean(
        "https://example.com/p?utm_source=x&id=1", check=ok,
        transport=transport(lambda r: httpx.Response(200, text="hello"), seen))
    req = seen[0]
    assert str(req.url) == "https://example.com/p?id=1" and body == b"hello"
    assert "referer" not in req.headers and "cookie" not in req.headers
    assert req.headers["user-agent"] == webfetch.USER_AGENT


async def test_a_cookie_is_never_sent_back_not_in_a_redirect_and_not_in_the_next_call():
    seen = []

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Set-Cookie": "sid=abc; Path=/", "Location": "/next"})
        return httpx.Response(200, headers={"Set-Cookie": "other=1; Path=/"}, text="done")

    for _ in range(2):
        await webfetch.fetch_clean("https://example.com/start", check=ok, transport=transport(handler, seen))
    assert [r.url.path for r in seen] == ["/start", "/next", "/start", "/next"]
    assert all("cookie" not in r.headers and "referer" not in r.headers for r in seen)


async def test_every_redirect_hop_is_checked():
    seen = []

    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:9/secret"})
        return httpx.Response(200, text="secret")

    def check(url):
        return "blocked address" if "127.0.0.1" in url else None

    with pytest.raises(webfetch.FetchError, match="blocked address"):
        await webfetch.fetch_clean("https://example.com/", check=check, transport=transport(handler, seen))
    assert [r.url.host for r in seen] == ["example.com"]


async def test_a_hop_to_a_new_host_needs_its_own_yes():
    asked = []

    def handler(request):
        if request.url.host == "a.example":
            return httpx.Response(301, headers={"Location": "https://b.example/page?utm_source=z"})
        return httpx.Response(200, text="b")

    async def no(url):
        asked.append(url)
        return False

    async def yes(url):
        asked.append(url)
        return True

    with pytest.raises(webfetch.FetchError, match="not approved"):
        await webfetch.fetch_clean("https://a.example/", check=ok, approve_new_host=no,
                                   transport=transport(handler, []))
    _, _, body = await webfetch.fetch_clean("https://a.example/", check=ok, approve_new_host=yes,
                                            transport=transport(handler, []))
    assert body == b"b" and asked == ["https://b.example/page", "https://b.example/page"]


async def test_redirects_stop_after_five_and_the_body_is_capped():
    def loop(request):
        return httpx.Response(302, headers={"Location": "/again"})

    with pytest.raises(webfetch.FetchError, match="Too many redirects"):
        await webfetch.fetch_clean("https://example.com/", check=ok, transport=transport(loop, []))
    _, _, body = await webfetch.fetch_clean("https://example.com/", check=ok, max_bytes=10,
                                            transport=transport(lambda r: httpx.Response(200, content=b"x" * 500), []))
    assert len(body) == 10


def test_html_to_text_drops_scripts_and_decodes_entities():
    text = webfetch.html_to_text("<p>a &amp; b</p><script>x()</script><style>p{}</style><p>c</p>")
    assert "a & b" in text and "c" in text and "x()" not in text and "<" not in text
