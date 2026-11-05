"""Shared Google OAuth2 — token management and browser auth flow for Gmail + Calendar."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from micropaw.config import CONFIG_DIR

logger = logging.getLogger(__name__)

_TOKEN_FILE = CONFIG_DIR / "google_tokens.json"
# Gmail and Calendar IDs are plain tokens. Anything else ("/", "..") could turn
# the request into a call on a different endpoint, e.g. deleting a whole calendar.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,1024}")
_REDIRECT_PORT = 8089
_REDIRECT_URI = f"http://localhost:{_REDIRECT_PORT}"
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar"
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.modify"

# Injected at startup by gateway
_config: Any = None



async def api_get(token: str, url: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params or {}, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json()


async def api_post(token: str, url: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            url, json=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


async def api_delete(token: str, url: str) -> int:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.status_code


def api_error(e: httpx.HTTPStatusError, service: str) -> str:
    """Return sanitized error — don't echo raw response body (may contain OAuth diagnostics)."""
    return f"Error: {service} API returned HTTP {e.response.status_code}."


async def ensure_token(service: str = "Google") -> tuple[str | None, str | None]:
    """Get token or return error message. Returns (token, None) or (None, error)."""
    token = await get_access_token()
    if token:
        return token, None
    if needs_reauth():
        return None, (
            f"Error: {service} not authorized. "
            "Ask the user to run the 'authorize' action to sign in with Google."
        )
    return None, (
        "Error: Not authorized. Run the 'authorize' action first, "
        "or check that Google credentials are configured via 'paw setup'."
    )


def set_google_config(config: Any) -> None:
    """Inject GoogleConfig at startup."""
    global _config
    _config = config



def _load_tokens() -> dict | None:
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_tokens(tokens: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Write with restricted permissions from the start (avoid world-readable window)
    fd = os.open(str(_TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, json.dumps(tokens, indent=2).encode())
    finally:
        os.close(fd)


def _required_scopes() -> str:
    """Build space-separated scope string based on enabled services."""
    scopes = []
    if _config and getattr(_config, "calendar_enabled", True):
        scopes.append(SCOPE_CALENDAR)
    if _config and getattr(_config, "gmail_enabled", True):
        scopes.append(SCOPE_GMAIL)
    return " ".join(scopes) if scopes else SCOPE_CALENDAR


def _current_scopes() -> set[str]:
    """Return the set of scopes stored in the token file."""
    tokens = _load_tokens()
    if not tokens:
        return set()
    return set(tokens.get("scopes", "").split())


def needs_reauth() -> bool:
    """True if stored tokens lack scopes that are now required."""
    required = set(_required_scopes().split())
    current = _current_scopes()
    return not required.issubset(current)


async def get_access_token() -> str | None:
    """Get a valid access token, refreshing if expired."""
    tokens = _load_tokens()
    if not tokens:
        return None

    if tokens.get("expires_at", 0) > time.time() + 60:
        return tokens["access_token"]

    # Refresh
    refresh_token = tokens.get("refresh_token")
    if not refresh_token or not _config:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(_TOKEN_URL, data={
                "client_id": _config.client_id,
                "client_secret": _config.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            })
            if resp.status_code != 200:
                logger.error("Google token refresh failed (HTTP %d)", resp.status_code)
                return None
            data = resp.json()
            tokens["access_token"] = data["access_token"]
            tokens["expires_at"] = time.time() + data.get("expires_in", 3600)
            _save_tokens(tokens)
            return tokens["access_token"]
    except Exception as e:
        logger.error("Google token refresh error: %s", e)
        return None



def _run_auth_server() -> str | None:
    """Run a one-shot local HTTP server to capture the OAuth2 redirect code."""
    auth_code = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal auth_code
            qs = parse_qs(urlparse(self.path).query)
            auth_code = qs.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>Authorization successful!</h2>"
                b"<p>You can close this window and return to MicroPaw.</p>"
            )

        def log_message(self, *args):
            pass

    try:
        server = HTTPServer(("localhost", _REDIRECT_PORT), Handler)
        server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError as e:
        logger.error("Cannot start OAuth redirect server on port %d: %s", _REDIRECT_PORT, e)
        return None
    server.timeout = 120
    server.handle_request()
    server.server_close()
    return auth_code


async def authorize() -> str:
    """Run one-time OAuth2 authorization flow for enabled Google services."""
    if not _config or not _config.client_id or not _config.client_secret:
        return (
            "Error: Google services not configured. Run 'paw setup' to set "
            "your Google OAuth2 Client ID and Client Secret."
        )

    scopes = _required_scopes()

    params = urlencode({
        "client_id": _config.client_id,
        "redirect_uri": _REDIRECT_URI,
        "response_type": "code",
        "scope": scopes,
        "access_type": "offline",
        "prompt": "consent",
    })
    auth_url = f"{_AUTH_URL}?{params}"

    logger.info("Opening browser for Google authorization...")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    auth_code = await asyncio.to_thread(_run_auth_server)
    if not auth_code:
        return (
            f"Error: Authorization timed out or was cancelled.\n"
            f"Visit this URL manually if needed:\n{auth_url}"
        )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_TOKEN_URL, data={
                "client_id": _config.client_id,
                "client_secret": _config.client_secret,
                "code": auth_code,
                "redirect_uri": _REDIRECT_URI,
                "grant_type": "authorization_code",
            })
            if resp.status_code != 200:
                return f"Error: Token exchange failed (HTTP {resp.status_code})"

            data = resp.json()
            tokens = {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", ""),
                "expires_at": time.time() + data.get("expires_in", 3600),
                "scopes": scopes,
            }
            _save_tokens(tokens)

            services = []
            if SCOPE_GMAIL in scopes:
                services.append("Gmail")
            if SCOPE_CALENDAR in scopes:
                services.append("Calendar")
            return f"Google authorized successfully ({', '.join(services)}). Tokens saved."
    except Exception as e:
        return f"Error during authorization: {e}"


def bad_id(value: str, what: str) -> str | None:
    """Return an error if value is not a plain Google resource ID."""
    return None if _ID_RE.fullmatch(value or "") else f"Error: invalid {what}: {value!r}"
