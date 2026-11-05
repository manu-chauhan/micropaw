"""One place to make HTTP clients. Stream A adds per-host permits, keep-alive limits and the shared TLS context."""

from __future__ import annotations

import httpx


def client(url: str = "", *, timeout: float | httpx.Timeout = 30.0, local: bool | None = None,
           **kwargs) -> httpx.AsyncClient:
    """An AsyncClient for talking to `url`'s host. Local hosts skip HTTP proxies from the environment."""
    if local is None:
        from micropaw.privacy import is_local_url
        local = bool(url) and is_local_url(url)
    return httpx.AsyncClient(timeout=timeout, trust_env=not local, **kwargs)
