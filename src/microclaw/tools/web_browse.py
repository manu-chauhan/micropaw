"""Web browsing tool using Playwright."""

from __future__ import annotations

import logging
from typing import Any

from microclaw.security import validate_url
from microclaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

MAX_CONTENT = 20_000

# Shared browser instance (lazy init)
_browser = None
_playwright = None


async def _get_browser():
    """Lazy-initialize a shared Playwright browser instance."""
    global _browser, _playwright
    if _browser is None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Playwright not installed. Install with: pip install microclaw[browser]")
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=True)
    return _browser


async def cleanup_browser():
    """Clean up the browser on shutdown."""
    global _browser, _playwright
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None


class WebBrowseTool(Tool):
    name = "web_browse"
    description = "Browse a URL and extract its content. Supports text extraction, screenshots, and form interaction."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to browse"},
            "extract": {
                "type": "string",
                "description": "What to extract: 'text' (readable content), 'html' (raw HTML), 'title' (page title)",
                "enum": ["text", "html", "title"],
            },
        },
        "required": ["url"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if not context.allow_web_browse:
            return "Error: Web browsing is not enabled."

        url = args["url"]
        extract = args.get("extract", "text")

        if error := validate_url(url):
            return f"Error: {error}"

        try:
            browser = await _get_browser()
            ctx = await browser.new_context()
            page = await ctx.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                if extract == "title":
                    title = await page.title()
                    return f"Page title: {title}\nURL: {page.url}"

                if extract == "html":
                    html = await page.content()
                    if len(html) > MAX_CONTENT:
                        html = html[:MAX_CONTENT] + "\n... (truncated)"
                    return html

                # Default: text extraction with DOM cleaning
                await page.evaluate("""
                    for (const el of document.querySelectorAll('script, style, nav, footer, header, aside')) {
                        el.remove();
                    }
                """)
                text = await page.inner_text("body")
                text = text.strip()
                if len(text) > MAX_CONTENT:
                    text = text[:MAX_CONTENT] + "\n... (truncated)"
                return f"Content from {page.url}:\n\n{text}" if text else f"No readable content found at {url}"

            finally:
                await ctx.close()

        except RuntimeError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error browsing {url}: {e}"
