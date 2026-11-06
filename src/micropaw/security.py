"""Security utilities — SSRF protection, URL validation, path security."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from micropaw import privacy

BLOCKED_HOSTS = frozenset({
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "169.254.169.254",  # AWS/GCP metadata
    "metadata.google.internal",
    "[::1]",
})

BLOCKED_DOMAIN_SUFFIXES = (
    ".internal", ".local", ".corp", ".lan", ".intranet",
    ".home.arpa",
)

ALLOWED_SCHEMES = frozenset({"http", "https"})


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything that is not a public internet address.

    Uses is_global so shared ranges such as 100.64.0.0/10 (CGNAT, Alibaba Cloud
    metadata, Tailscale) are blocked along with private, loopback and link-local.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_multicast or not ip.is_global


async def validate_url(url: str) -> str | None:
    """Validate a URL for SSRF protection.

    Returns an error string if blocked, None if safe.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL"

    if parsed.scheme not in ALLOWED_SCHEMES:
        return f"Blocked scheme: {parsed.scheme!r}. Only http/https allowed."

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return "No hostname in URL"

    if hostname in BLOCKED_HOSTS:
        return f"Blocked host: {hostname}"

    # Check for IP addresses
    try:
        ip = ipaddress.ip_address(hostname)
        if _is_private_ip(ip):
            return f"Blocked private/reserved IP: {ip}"
    except ValueError:
        pass  # Domain name, not IP

    # Block common internal domain patterns
    lower_host = hostname.lower()
    for suffix in BLOCKED_DOMAIN_SUFFIXES:
        if lower_host.endswith(suffix):
            return f"Blocked internal domain: {hostname}"

    # Local and airgapped mode: a name the owner has not just approved is refused before any lookup,
    # so a refused host cannot leak through DNS either
    if privacy.local_only() and (why := privacy.guard().check_host(hostname)):
        return f"Blocked by privacy mode: {why}"

    # DNS resolution check — prevent DNS rebinding
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(
            None, lambda: socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM),
        )
        for info in infos:
            addr = info[4][0]
            resolved_ip = ipaddress.ip_address(addr)
            if _is_private_ip(resolved_ip):
                return f"Blocked: {hostname} resolves to private IP {resolved_ip}"
    except socket.gaierror:
        return f"Blocked: cannot resolve hostname {hostname}"

    return None
