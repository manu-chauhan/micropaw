"""Voice notes to text through an OpenAI-style transcription endpoint (local or hosted)."""

from __future__ import annotations

import logging
import os
import re

import httpx

from micropaw.config import Config
from micropaw.providers.registry import PRESETS

logger = logging.getLogger(__name__)


def _endpoint(config: Config) -> tuple[str, str, str] | None:
    """(base_url, api_key, model) for config.voice.stt, or None when unset or unusable."""
    name, _, model = config.voice.stt.partition(":")
    name, model = name.strip().lower(), model.strip()
    if not model:
        return None
    if name == "local":
        return config.voice.stt_url, "", model
    if name == "openai" and config.provider_config.openai.api_key:
        return config.provider_config.openai.base_url, config.provider_config.openai.api_key, model
    if name in PRESETS:
        key_env = PRESETS[name].key_env
        key = os.environ.get(key_env, "") if key_env else ""
        if key or not key_env:
            return PRESETS[name].base_url, key, model
    return None


def _audio_url(base_url: str) -> str:
    """The transcription URL. A base URL that already ends in a version ("/v1") is not given a second one."""
    base = base_url.rstrip("/")
    return base + ("/audio/transcriptions" if re.search(r"/v\d+$", base) else "/v1/audio/transcriptions")


async def transcribe(data: bytes, mime_type: str, config: Config) -> str | None:
    """Text of the recording, or None if speech-to-text isn't set up or the call failed."""
    endpoint = _endpoint(config)
    if endpoint is None:
        return None
    base_url, key, model = endpoint
    ext = mime_type.split("/")[-1].split(";")[0] or "ogg"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                _audio_url(base_url),
                headers=headers, data={"model": model}, files={"file": (f"voice.{ext}", data, mime_type)},
            )
            resp.raise_for_status()
            return resp.json().get("text", "").strip() or None
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Voice transcription failed: %s", e)
        return None
