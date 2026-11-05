"""Pages and shortcuts the owner opens later can load or follow remote URLs (an <img> or a ![](...) whose address
carries data). In local and airgapped mode, writes of such files are checked for links outside this side."""

from __future__ import annotations

import re
from pathlib import PurePath
from urllib.parse import urlparse

_VIEWED_LATER = frozenset({".html", ".htm", ".svg", ".md", ".xml", ".css", ".url", ".webloc", ".desktop"})
_URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)


def remote_hosts(path: str, content: str) -> list[str]:
    """Hosts outside this machine and network that the file links to ([] when the mode is normal)."""
    from micropaw import privacy
    if not privacy.local_only() or PurePath(path).suffix.lower() not in _VIEWED_LATER:
        return []
    hosts: list[str] = []
    for url in _URL.findall(content):
        if not privacy.is_local_url(url):
            host = urlparse(url).hostname or url
            if host not in hosts:
                hosts.append(host)
    return hosts[:10]


def refusal(hosts: list[str]) -> str:
    return ("Error: airgapped mode does not write files that link to outside hosts "
            f"({', '.join(hosts)}). Opening the file later would send requests there.")


def question(path: str, hosts: list[str]) -> str:
    return (f"Write {path}, which links to hosts outside your network: {', '.join(hosts)}.\n"
            "Opening the file later would send requests to them.")
