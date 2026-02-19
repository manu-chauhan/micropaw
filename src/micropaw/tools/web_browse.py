"""Web browsing tool using Playwright, with smart YouTube transcript extraction."""

from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any
from xml.etree.ElementTree import fromstring

import httpx

from micropaw.security import validate_url
from micropaw.tools.base import Tool, ToolContext

logger = logging.getLogger(__name__)

MAX_CONTENT = 20_000

# Shared browser instance (lazy init)
_browser = None
_playwright = None

# YouTube URL patterns — matches watch, short URLs, and Shorts
_YT_PATTERN = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})"
)
_INNERTUBE_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":\s*"([a-zA-Z0-9_-]+)"')
_INNERTUBE_CLIENT = {"clientName": "ANDROID", "clientVersion": "20.10.38"}


async def _get_browser():
    """Lazy-initialize a shared Playwright browser instance."""
    global _browser, _playwright
    if _browser is None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Playwright not installed. Install with: pip install micropaw[browser]")
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


async def fetch_youtube_transcript(video_id: str) -> str:
    """Fetch YouTube transcript via InnerTube API. Raw httpx, zero extra deps."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US",
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15.0) as client:
        # 1. Get API key from video page
        page = await client.get(f"https://www.youtube.com/watch?v={video_id}")
        m = _INNERTUBE_KEY_RE.search(page.text)
        if not m:
            return ""

        # 2. InnerTube player request (ANDROID client returns valid caption URLs)
        player = (await client.post(
            f"https://www.youtube.com/youtubei/v1/player?key={m.group(1)}",
            json={"context": {"client": _INNERTUBE_CLIENT}, "videoId": video_id},
        )).json()

        details = player.get("videoDetails", {})
        title = details.get("title", "Unknown")
        author = details.get("author", "Unknown")
        duration = int(details.get("lengthSeconds", 0))

        tracks = (player.get("captions", {})
                  .get("playerCaptionsTracklistRenderer", {})
                  .get("captionTracks", []))
        if not tracks:
            mins, secs = divmod(duration, 60)
            return (
                f"Title: {title}\nAuthor: {author}\nDuration: {mins}:{secs:02d}"
                "\n\nNo captions available for this video."
            )

        # Prefer manual English, then any English, then first available
        en_manual = [t for t in tracks if t["languageCode"].startswith("en") and t.get("kind") != "asr"]
        en_any = [t for t in tracks if t["languageCode"].startswith("en")]
        track = (en_manual or en_any or tracks)[0]

        cap_url = track["baseUrl"]
        if "&exp=xpe" in cap_url:
            return f"Title: {title}\nAuthor: {author}\n\nCaptions require authentication for this video."

        # 3. Fetch and parse caption XML
        cap = await client.get(cap_url)
        if not cap.text:
            return ""

        root = fromstring(cap.text)
        segments: list[str] = []
        for tag in ("p", "text"):  # manual captions use <p>, auto-gen uses <text>
            for elem in root.iter(tag):
                # Text may be direct (old format) or inside <s> children (new format)
                direct = unescape(elem.text or "").replace("\n", " ").strip()
                if direct:
                    segments.append(direct)
                else:
                    parts = [unescape(s.text or "").strip() for s in elem.iter("s") if s.text]
                    if parts:
                        segments.append(" ".join(parts))
            if segments:
                break

        transcript = " ".join(segments)
        mins, secs = divmod(duration, 60)
        lang = track.get("languageCode", "?")
        kind = "auto-generated" if track.get("kind") == "asr" else "manual"

        return (
            f"Title: {title}\n"
            f"Author: {author}\n"
            f"Duration: {mins}:{secs:02d}\n"
            f"Captions: {lang} ({kind})\n\n"
            f"Transcript:\n{transcript}"
        )


class WebBrowseTool(Tool):
    name = "web_browse"
    description = (
        "Browse a URL and extract its content. "
        "YouTube URLs automatically extract the video transcript for RAG indexing."
    )
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

        # Smart YouTube detection — fetch transcript directly, skip Playwright
        yt_match = _YT_PATTERN.search(url)
        if yt_match and extract == "text":
            try:
                transcript = await fetch_youtube_transcript(yt_match.group(1))
                if transcript:
                    logger.info("YouTube transcript fetched for %s", yt_match.group(1))
                    return transcript
            except Exception as e:
                logger.warning("YouTube transcript failed for %s, falling back to Playwright: %s", url, e)

        # Standard Playwright flow for non-YouTube URLs (or YouTube fallback)
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
