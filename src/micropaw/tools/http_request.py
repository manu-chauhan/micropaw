"""HTTP request tool with SSRF protection."""

from __future__ import annotations

import asyncio
import ipaddress
import os
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from micropaw import net, privacy
from micropaw.security import _is_private_ip, validate_url
from micropaw.tools.base import Tool, ToolContext

MAX_BODY = 50_000
MAX_DOWNLOAD = 5_000_000  # bytes read from the network before giving up
MAX_REDIRECTS = 5


class _BlockedError(Exception):
    pass


def _check_peer(response: httpx.Response, url: str) -> None:
    """Refuse if the connection actually went to a private address.

    validate_url resolves the name once and httpx resolves it again to connect, so a
    DNS answer that flips to 127.0.0.1 in between (rebinding) would get past the first
    check. This looks at the address really used. Skipped behind a proxy, which does
    its own resolving.
    """
    if os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("ALL_PROXY"):
        return
    stream = response.extensions.get("network_stream")
    peer = stream.get_extra_info("server_addr") if stream else None
    if peer and _is_private_ip(ipaddress.ip_address(peer[0])):
        raise _BlockedError(f"{urlparse(url).hostname} connected to a private address ({peer[0]})")


async def _send(client: httpx.AsyncClient, method: str, url: str, headers: dict, body: Any) -> httpx.Response:
    response = await client.send(client.build_request(method, url, headers=headers, content=body), stream=True)
    try:
        _check_peer(response, url)
    except _BlockedError:
        await response.aclose()
        raise
    return response


async def _read_capped(response: httpx.Response) -> tuple[bytes, bool]:
    """Read at most MAX_DOWNLOAD bytes. Returns (data, truncated)."""
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data += chunk
        if len(data) > MAX_DOWNLOAD:
            return bytes(data[:MAX_DOWNLOAD]), True
    return bytes(data), False


class HttpRequestTool(Tool):
    name = "http_request"
    marks = True
    description = "Make an HTTP request (GET, POST, PUT, DELETE) and return the response."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to request"},
            "method": {
                "type": "string",
                "description": "HTTP method (default: GET)",
                "enum": ["GET", "POST", "PUT", "DELETE"],
            },
            "headers": {"type": "object", "description": "Optional HTTP headers"},
            "body": {"type": "string", "description": "Optional request body (for POST/PUT)"},
        },
        "required": ["url"],
    }

    def needs_approval(self, args: dict[str, Any], context: ToolContext) -> str | None:
        """Anything but a plain GET changes something; after outside content, even a GET can carry data out."""
        method = args.get("method", "GET").upper()
        if privacy.local_only() and not privacy.airgapped():
            return f"{method} {args.get('url', '')}"  # local mode: every URL is shown and asked
        if method == "GET" and not context.marked:
            return None
        body = str(args.get("body") or "")
        headers = ", ".join((args.get("headers") or {}).keys())
        return (f"{method} {args.get('url', '')}" + (f"\nHeaders: {headers}" if headers else "")
                + (f"\nBody: {body[:300]}" if body else ""))

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        url = args["url"]
        method = args.get("method", "GET").upper()
        headers = args.get("headers") or {}
        body = args.get("body")

        if refusal := privacy.tool_refusal("web_fetch"):
            return f"Error: {refusal}"
        if privacy.local_only() and method != "GET":
            return "Error: Local mode allows web fetches with GET only."
        if privacy.local_only() and (host := urlparse(url).hostname):
            if why := await asyncio.to_thread(privacy.permit, host):  # the owner approved this URL just now
                return f"Error: {why}"

        if error := await validate_url(url):
            return f"Error: {error}"

        try:
            async with net.client(timeout=15.0, follow_redirects=False) as client:
                response = await _send(client, method, url, headers, body)

                # Manual redirect following with SSRF check at each hop
                redirects = 0
                while response.is_redirect and redirects < MAX_REDIRECTS:
                    location = response.headers.get("location", "")
                    if not location:
                        break
                    location = urljoin(url, location)  # resolves relative redirects (RFC 3986)

                    if error := await validate_url(location):
                        return f"Error: Redirect blocked — {error}"

                    # 301/302/303 change to GET, 307/308 preserve method
                    redirect_method = "GET" if response.status_code in (301, 302, 303) else method
                    cross_origin = urlparse(location).netloc != urlparse(url).netloc
                    if cross_origin and redirect_method != "GET":
                        # The owner approved sending this to the first host, not to another one
                        return f"Stopped: {url} redirected a {method} request to another site ({location})."
                    await response.aclose()
                    # A different site never gets the caller's headers (tokens, cookies, API keys)
                    response = await _send(
                        client, redirect_method, location,
                        {} if cross_origin else headers,
                        body if redirect_method != "GET" else None,
                    )
                    url = location
                    redirects += 1

                data, truncated_download = await _read_capped(response)
                await response.aclose()

            result = f"HTTP {response.status_code} {response.reason_phrase}\n"
            result += f"URL: {url}\n"

            content_type = response.headers.get("content-type", "")
            result += f"Content-Type: {content_type}\n\n"

            text = data.decode(response.encoding or "utf-8", errors="replace")
            if len(text) > MAX_BODY or truncated_download:
                text = text[:MAX_BODY] + "\n\n... (truncated)"
            result += text

            return result

        except _BlockedError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"HTTP request error: {e}"
