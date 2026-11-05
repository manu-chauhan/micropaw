"""Web browsing tool using Playwright, with smart YouTube transcript extraction."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import mimetypes
import os
import re
import threading
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree.ElementTree import fromstring

import httpx

from micropaw.providers.base import MediaItem
from micropaw.security import validate_url
from micropaw.tools.base import Tool, ToolContext, ToolResult
from micropaw.tools.path_security import in_workspace, validate_file_path

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
            raise RuntimeError("Playwright is not installed. Run: paw extras add browser")
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


# Per-conversation browser sessions (page stays alive for interactions)
_sessions: dict[str, tuple[Any, Any]] = {}  # conv_id -> (context, page)


async def _get_session(conv_id: str, url: str | None = None) -> Any:
    """Get or create a browser page for this conversation."""
    if conv_id not in _sessions:
        browser = await _get_browser()
        ctx = await browser.new_context()
        page = await ctx.new_page()
        _sessions[conv_id] = (ctx, page)
    _ctx, page = _sessions[conv_id]
    if url:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    return page


async def _left_public_web(page: Any) -> str | None:
    """Re-check where the page ended up after redirects, clicks or scripts.

    Only the first URL is checked before loading, so a public page that redirects
    to 169.254.169.254 or localhost would otherwise hand internal data to the model.
    """
    if page.url.split(":", 1)[0] not in ("http", "https", "file"):
        return None  # about:blank, chrome-error:// and the like
    if any(page.url.startswith(f"http://127.0.0.1:{srv.server_address[1]}/") for srv in _previews.values()):
        return None  # our own preview server; its page can only load from there
    if error := await validate_url(page.url):
        await page.goto("about:blank")
        return f"Error: page moved to a blocked address ({error})."
    return None


async def close_session(conv_id: str) -> None:
    """Close browser session for a conversation (called at end of agent turn)."""
    if server := _previews.pop(conv_id, None):
        await asyncio.to_thread(server.shutdown)
        server.server_close()
    if conv_id in _sessions:
        ctx, _page = _sessions.pop(conv_id)
        try:
            await ctx.close()
        except Exception:
            pass


# Preview servers, one per conversation: a local web server for one workspace folder, 127.0.0.1 only
_previews: dict[str, ThreadingHTTPServer] = {}
_PREVIEW_MAX_FILE = 20 * 1024 * 1024
_PREVIEW_PAGES = (".html", ".htm")


def _plain_file(base: Path, parts: list[str]) -> Path | None:
    """base/parts when every step is a real folder or file: no symlinks, no '..', no drive letters."""
    cur = base
    for part in parts:
        if part in ("", ".", "..") or ":" in part or "\\" in part:
            return None
        cur = cur / part
        if cur.is_symlink():
            return None
    return cur if cur.is_file() else None


def _preview_server(root: Path) -> ThreadingHTTPServer:
    """Serves files under `root` on a random port, 127.0.0.1 only. Secret-looking files are refused."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            rel = unquote(self.path.split("?", 1)[0].split("#", 1)[0])
            path = _plain_file(root, [p for p in rel.split("/") if p])
            if path is None or validate_file_path(str(path)) or path.stat().st_size > _PREVIEW_MAX_FILE:
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


def preview_file(raw: str, workspace: str) -> Path | str:
    """The workspace HTML file to preview, or the reason it is refused."""
    if not raw or not workspace:
        return "'preview' needs 'path', an HTML file in the workspace."
    ws = Path(os.path.abspath(Path(workspace).expanduser()))
    try:
        rel = Path(os.path.abspath(ws / Path(raw).expanduser())).relative_to(ws)
    except ValueError:
        return "only .html files inside the workspace can be previewed."
    file = _plain_file(ws, list(rel.parts))
    if file is None or file.suffix.lower() not in _PREVIEW_PAGES or not in_workspace(file.resolve(), workspace):
        return "only plain .html files inside the workspace can be previewed (no symlinks)."
    return validate_file_path(str(file)) or file


async def _extract_text(page: Any) -> str:
    """Extract readable text from a Playwright page."""
    await page.evaluate("""
        for (const el of document.querySelectorAll('script, style, nav, footer, header, aside')) {
            el.remove();
        }
    """)
    text = (await page.inner_text("body")).strip()
    if len(text) > MAX_CONTENT:
        text = text[:MAX_CONTENT] + "\n... (truncated)"
    return text


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
        player_resp = await client.post(
            f"https://www.youtube.com/youtubei/v1/player?key={m.group(1)}",
            json={"context": {"client": _INNERTUBE_CLIENT}, "videoId": video_id},
        )
        try:
            player = player_resp.json()
        except Exception:
            return ""

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
    marks = True
    description = (
        "Browse pages and take screenshots. Actions: browse (load a URL), click, type, scroll, screenshot, "
        "preview (open a workspace HTML file; report console errors, window.__state and a screenshot). "
        "The page stays open during a turn. YouTube URLs give the transcript."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "What to do",
                "enum": ["browse", "click", "type", "scroll", "screenshot", "preview"],
            },
            "path": {"type": "string", "description": "HTML file in the workspace (for 'preview')"},
            "screenshot": {
                "type": "boolean",
                "description": "For preview: include a screenshot (default true)",
            },
            "url": {"type": "string", "description": "The URL (browse)"},
            "extract": {
                "type": "string",
                "description": "What to extract (default text)",
                "enum": ["text", "html", "title"],
            },
            "selector": {
                "type": "string",
                "description": "CSS selector or visible text (click, type)",
            },
            "text": {
                "type": "string",
                "description": "Text to type",
            },
            "direction": {
                "type": "string",
                "description": "Scroll direction",
                "enum": ["down", "up"],
            },
        },
        "required": [],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str | ToolResult:
        if not context.allow_web_browse:
            return "Error: Web browsing is not enabled."

        action = args.get("action", "browse")
        conv_id = context.conversation_id

        try:
            if action == "browse":
                return await self._action_browse(args, conv_id)
            elif action == "click":
                return await self._action_click(args, conv_id)
            elif action == "type":
                return await self._action_type(args, conv_id)
            elif action == "scroll":
                return await self._action_scroll(args, conv_id)
            elif action == "screenshot":
                return await self._action_screenshot(conv_id)
            elif action == "preview":
                return await self._action_preview(args, conv_id, context.workspace)
            else:
                return f"Unknown action: {action}"
        except RuntimeError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error in web_browse ({action}): {e}"

    async def _action_browse(self, args: dict[str, Any], conv_id: str) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: 'browse' action requires 'url'."
        extract = args.get("extract", "text")

        if error := await validate_url(url):
            return f"Error: {error}"

        # Smart YouTube detection
        yt_match = _YT_PATTERN.search(url)
        if yt_match and extract == "text":
            try:
                transcript = await fetch_youtube_transcript(yt_match.group(1))
                if transcript:
                    logger.info("YouTube transcript fetched for %s", yt_match.group(1))
                    return transcript
            except Exception as e:
                logger.warning("YouTube transcript failed for %s: %s", url, e)

        page = await _get_session(conv_id, url)
        if error := await _left_public_web(page):
            return error

        if extract == "title":
            title = await page.title()
            return f"Page title: {title}\nURL: {page.url}"
        if extract == "html":
            html = await page.content()
            if len(html) > MAX_CONTENT:
                html = html[:MAX_CONTENT] + "\n... (truncated)"
            return html

        text = await _extract_text(page)
        return f"Content from {page.url}:\n\n{text}" if text else f"No readable content found at {url}"

    async def _action_click(self, args: dict[str, Any], conv_id: str) -> str:
        selector = args.get("selector", "")
        if not selector:
            return "Error: 'click' requires 'selector' (CSS selector or visible text)."
        if conv_id not in _sessions:
            return "Error: No page open. Use 'browse' first to load a URL."

        _ctx, page = _sessions[conv_id]
        # Try CSS selector first, fall back to text-based click
        try:
            el = page.locator(selector)
            if await el.count() > 0:
                await el.first.click(timeout=5000)
            else:
                await page.get_by_text(selector, exact=False).first.click(timeout=5000)
        except Exception as e:
            return f"Error clicking '{selector}': {e}"

        await page.wait_for_load_state("domcontentloaded", timeout=10000)
        if error := await _left_public_web(page):
            return error
        text = await _extract_text(page)
        return f"Clicked '{selector}'. Page now at {page.url}:\n\n{text}"

    async def _action_type(self, args: dict[str, Any], conv_id: str) -> str:
        selector = args.get("selector", "")
        text = args.get("text", "")
        if not selector or not text:
            return "Error: 'type' requires 'selector' and 'text'."
        if conv_id not in _sessions:
            return "Error: No page open. Use 'browse' first to load a URL."

        _ctx, page = _sessions[conv_id]
        try:
            el = page.locator(selector)
            if await el.count() > 0:
                await el.first.fill(text, timeout=5000)
            else:
                await page.get_by_placeholder(selector).first.fill(text, timeout=5000)
        except Exception as e:
            return f"Error typing into '{selector}': {e}"

        return f"Typed '{text}' into '{selector}' on {page.url}"

    async def _action_scroll(self, args: dict[str, Any], conv_id: str) -> str:
        direction = args.get("direction", "down")
        if conv_id not in _sessions:
            return "Error: No page open. Use 'browse' first to load a URL."

        _ctx, page = _sessions[conv_id]
        delta = 800 if direction == "down" else -800
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(500)  # wait for lazy-loaded content
        if error := await _left_public_web(page):
            return error

        text = await _extract_text(page)
        return f"Scrolled {direction} on {page.url}:\n\n{text}"

    async def _action_preview(self, args: dict[str, Any], conv_id: str, workspace: str) -> str | ToolResult:
        """Open a workspace HTML file on a local server. The page may only load from that server."""
        if importlib.util.find_spec("playwright") is None:
            return "Error: the preview needs Playwright. Run: paw extras add browser"
        path = preview_file(str(args.get("path") or ""), workspace)
        if isinstance(path, str):
            return f"Error: {path}"
        await close_session(conv_id)  # a fresh page, never one that visited the open web
        root = path.parent
        server = _preview_server(root)
        _previews[conv_id] = server
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        browser = await _get_browser()
        ctx = await browser.new_context()
        _sessions[conv_id] = (ctx, await ctx.new_page())
        page = _sessions[conv_id][1]

        async def only_local(route: Any) -> None:
            await (route.continue_() if route.request.url.startswith(origin + "/") else route.abort())

        await ctx.route("**/*", only_local)
        await ctx.add_init_script("delete window.WebSocket;")
        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"uncaught: {e}"))
        await page.goto(f"{origin}/{path.name}", wait_until="load", timeout=15000)
        await page.wait_for_timeout(1000)  # let the first frames run
        try:
            state = await page.evaluate("typeof window.__state === 'function' ? window.__state() : window.__state")
        except Exception as e:
            state = f"(window.__state failed: {e})"
        shown = "not defined by the page" if state is None else json.dumps(state, default=str)[:2000]
        problems = "\n".join(e[:300] for e in errors[:20]) or "none"
        text = f"Preview of {path.name}\nConsole errors: {problems}\nwindow.__state: {shown}"
        if args.get("screenshot", True) is False:
            return text
        shot = await page.screenshot(type="png", full_page=False)
        return ToolResult(text=text, media=[MediaItem(type="image", data=shot, mime_type="image/png")])

    async def _action_screenshot(self, conv_id: str) -> str | ToolResult:
        if conv_id not in _sessions:
            return "Error: No page open. Use 'browse' first to load a URL."

        _ctx, page = _sessions[conv_id]
        if error := await _left_public_web(page):
            return error
        screenshot_bytes = await page.screenshot(type="png", full_page=False)
        title = await page.title()
        media = MediaItem(type="image", data=screenshot_bytes, mime_type="image/png")
        return ToolResult(
            text=f"Screenshot of '{title}' ({page.url})",
            media=[media],
        )
