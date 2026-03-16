"""Security utilities — SSRF protection, URL validation, path security."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

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
    """Check if IP is private, including IPv4-mapped IPv6 addresses."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


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


def validate_path(path: str, allowed_root: str | None = None) -> str | None:
    """Validate a file path for path traversal attacks.

    Returns an error string if blocked, None if safe.
    """
    resolved = Path(path).resolve()

    # Block obvious bad patterns
    if ".." in Path(path).parts:
        return f"Blocked: path contains '..': {path}"

    # If an allowed root is specified, ensure the path is under it
    if allowed_root:
        root = Path(allowed_root).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return f"Blocked: path {resolved} is outside allowed root {root}"

    # Block sensitive system paths
    sensitive = ["/etc/shadow", "/etc/passwd", "/etc/sudoers"]
    if str(resolved) in sensitive:
        return f"Blocked sensitive path: {resolved}"

    return None


def safe_workspace_path(workspace: str | None = None) -> Path:
    """Return a safe workspace root directory."""
    if workspace:
        return Path(workspace).resolve()
    return Path(os.getcwd()).resolve()
