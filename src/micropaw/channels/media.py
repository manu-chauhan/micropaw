"""Files shared in Discord and Slack: download images and audio with a size cap."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from micropaw.providers.base import MediaItem
from micropaw.security import validate_url

logger = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 4
_IMAGES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_HOPS = 3


@dataclass(slots=True)
class Attachment:
    url: str
    mime: str
    size: int  # as declared by the platform, 0 if unknown
    name: str
    outside: bool  # from someone other than the sender (forwarded or shared)


class MediaRefusedError(Exception):
    pass


def _kind(mime: str) -> str | None:
    mime = mime.split(";")[0].strip().lower()
    if mime in _IMAGES:
        return "image"
    return "audio" if mime.startswith("audio/") else None


async def download(url: str, headers: dict[str, str] | None = None, limit: int = MAX_BYTES) -> bytes:
    """The body of url, read in chunks and stopped at the limit. A redirect is followed only to the
    same host over https, so a token in headers never goes to another host."""
    origin = urlparse(url).hostname
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
        for _ in range(_HOPS + 1):
            if (blocked := await validate_url(url)) is not None:
                raise MediaRefusedError(blocked)
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.is_redirect:
                    nxt = httpx.URL(url).join(resp.headers.get("location", ""))
                    if nxt.host != origin or nxt.scheme != "https":
                        raise MediaRefusedError("it redirects to another host")
                    url = str(nxt)
                    continue
                resp.raise_for_status()
                if int(resp.headers.get("content-length") or 0) > limit:
                    raise MediaRefusedError("it is too large")
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > limit:
                        raise MediaRefusedError("it is too large")
                return bytes(data)
    raise MediaRefusedError("too many redirects")


async def fetch_media(
    files: list[Attachment], token: str = "", token_host: str = "", limit: int = MAX_BYTES,
) -> tuple[list[MediaItem], list[str]]:
    """(media, notes). Images and audio only; notes say why a file was skipped.

    The token is sent only to token_host or its subdomains (Slack: "slack.com").
    """
    items: list[MediaItem] = []
    notes: list[str] = []
    for f in files[:MAX_FILES]:
        kind = _kind(f.mime)
        if kind is None:
            notes.append(f"I skipped {f.name}: I can only read images and audio.")
            continue
        if f.size > limit:
            notes.append(f"I skipped {f.name}: it is too large (the limit is {limit // 2**20} MB).")
            continue
        headers = None
        if token:
            host = urlparse(f.url).hostname or ""
            if host != token_host and not host.endswith("." + token_host):
                notes.append(f"I skipped {f.name}: it is not hosted by {token_host}.")
                continue
            headers = {"Authorization": f"Bearer {token}"}
        try:
            data = await download(f.url, headers, limit)
        except (MediaRefusedError, httpx.HTTPError, ValueError) as e:
            logger.warning("Could not download %s: %s", f.name, e)
            notes.append(f"I could not read {f.name}: {e}." if isinstance(e, MediaRefusedError) else
                         f"I could not download {f.name}.")
            continue
        items.append(MediaItem(type=kind, data=data, mime_type=f.mime.split(";")[0].strip(), outside=f.outside))
    if len(files) > MAX_FILES:
        notes.append(f"I read the first {MAX_FILES} files and skipped the rest.")
    return items, notes
