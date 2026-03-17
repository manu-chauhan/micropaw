"""HTTP request tool with SSRF protection."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from micropaw.security import validate_url
from micropaw.tools.base import Tool, ToolContext

MAX_BODY = 50_000
MAX_REDIRECTS = 5


class HttpRequestTool(Tool):
    name = "http_request"
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

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        url = args["url"]
        method = args.get("method", "GET").upper()
        headers = args.get("headers") or {}
        body = args.get("body")

        if error := await validate_url(url):
            return f"Error: {error}"

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                response = await client.request(method, url, headers=headers, content=body)

                # Manual redirect following with SSRF check at each hop
                redirects = 0
                while response.is_redirect and redirects < MAX_REDIRECTS:
                    location = response.headers.get("location", "")
                    if not location:
                        break

                    # Resolve relative redirects (RFC 3986)
                    if not location.startswith(("http://", "https://")):
                        from urllib.parse import urljoin
                        location = urljoin(url, location)

                    if error := await validate_url(location):
                        return f"Error: Redirect blocked — {error}"

                    # 301/302/303 change to GET, 307/308 preserve method
                    redirect_method = "GET" if response.status_code in (301, 302, 303) else method

                    # Strip sensitive headers on cross-origin redirect
                    redirect_headers = dict(headers)
                    if urlparse(location).netloc != urlparse(url).netloc:
                        redirect_headers.pop("authorization", None)
                        redirect_headers.pop("Authorization", None)

                    response = await client.request(
                        redirect_method, location,
                        headers=redirect_headers,
                        content=body if redirect_method != "GET" else None,
                    )
                    url = location
                    redirects += 1

            result = f"HTTP {response.status_code} {response.reason_phrase}\n"
            result += f"URL: {url}\n"

            content_type = response.headers.get("content-type", "")
            result += f"Content-Type: {content_type}\n\n"

            text = response.text
            if len(text) > MAX_BODY:
                text = text[:MAX_BODY] + f"\n\n... (truncated, {len(response.text)} total chars)"
            result += text

            return result

        except Exception as e:
            return f"HTTP request error: {e}"
