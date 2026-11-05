"""One place to make HTTP clients: proxies, TLS context, connection limits and the privacy guard.

In airgapped and local mode a client ignores HTTP_PROXY and friends (traffic cannot be rerouted by the
environment) and refuses a host the guard does not allow before any name lookup.
"""

from __future__ import annotations

import functools
import ssl

import httpx

from micropaw import privacy

# Model calls often come more than 5 s apart (every slow tool round), so keep a few connections warm.
LIMITS = httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=120.0)


@functools.cache
def ssl_context() -> ssl.SSLContext:
    """One TLS context for the process: building one costs about 6 ms on a desktop (certifi bundle)."""
    return httpx.create_ssl_context()


async def _check(request: httpx.Request) -> None:
    """Refuse a host outside the policy with a plain message, before httpx resolves it."""
    if privacy.local_only() and (why := privacy.guard().check_host(request.url.host)):
        raise privacy.EgressBlocked(why)


def _check_sync(request: httpx.Request) -> None:
    if privacy.local_only() and (why := privacy.guard().check_host(request.url.host)):
        raise privacy.EgressBlocked(why)


def _options(url: str, local: bool | None, kwargs: dict) -> dict:
    if local is None:
        local = bool(url) and privacy.is_local_url(url)
    kwargs.setdefault("trust_env", not (local or privacy.local_only()))
    if not local and "verify" not in kwargs:
        kwargs["verify"] = ssl_context()
    return kwargs


def client(url: str = "", *, timeout: float | httpx.Timeout = 30.0, local: bool | None = None,
           **kwargs) -> httpx.AsyncClient:
    """An AsyncClient for talking to `url`'s host. Local hosts skip HTTP proxies from the environment."""
    hooks = kwargs.pop("event_hooks", None) or {}
    hooks.setdefault("request", []).insert(0, _check)
    return httpx.AsyncClient(timeout=timeout, event_hooks=hooks, **_options(url, local, kwargs))


def sync_client(url: str = "", *, timeout: float | httpx.Timeout = 30.0, local: bool | None = None,
                **kwargs) -> httpx.Client:
    """The same for the few synchronous callers (the update check, `paw test`)."""
    hooks = kwargs.pop("event_hooks", None) or {}
    hooks.setdefault("request", []).insert(0, _check_sync)
    return httpx.Client(timeout=timeout, event_hooks=hooks, **_options(url, local, kwargs))


def permit(host: str, ttl: float = 120.0, ips: tuple[str, ...] = ()) -> str | None:
    """Allow one host for a short time after the owner approved a search or a fetch. A refusal text or None."""
    return privacy.permit(host, ttl, ips)
