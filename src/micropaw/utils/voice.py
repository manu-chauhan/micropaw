"""Voice notes to text, and replies to speech, through OpenAI-style endpoints (local or hosted)."""

from __future__ import annotations

import logging
import os
import re

import httpx

from micropaw.config import Config
from micropaw.providers.registry import PRESETS

logger = logging.getLogger(__name__)


def _resolve(spec: str, local_url: str, config: Config, hosted: bool) -> tuple[str, str, str] | None:
    name, _, model = spec.partition(":")
    name, model = name.strip().lower(), model.strip()
    if not model:
        return None
    if name == "local":
        return local_url, "", model
    if name == "openai" and config.provider_config.openai.api_key:
        return config.provider_config.openai.base_url, config.provider_config.openai.api_key, model
    if hosted and name in PRESETS:
        key_env = PRESETS[name].key_env
        key = os.environ.get(key_env, "") if key_env else ""
        if key or not key_env:
            return PRESETS[name].base_url, key, model
    return None


def _endpoint(config: Config) -> tuple[str, str, str] | None:
    """(base_url, api_key, model) for config.voice.stt, or None when unset or unusable."""
    return _resolve(config.voice.stt, config.voice.stt_url, config, hosted=True)


def tts_endpoint(config: Config) -> tuple[str, str, str] | None:
    """(base_url, api_key, model) for config.voice.tts: OpenAI or a local server only."""
    return _resolve(config.voice.tts, config.voice.tts_url, config, hosted=False)


def _audio_url(base_url: str, path: str = "audio/transcriptions") -> str:
    """The endpoint URL. A base URL that already ends in a version ("/v1") is not given a second one."""
    base = base_url.rstrip("/")
    return f"{base}/{path}" if re.search(r"/v\d+$", base) else f"{base}/v1/{path}"


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


TTS_MAX_CHARS = 4000
TTS_MAX_BYTES = 20 * 1024 * 1024


def _audio_kind(data: bytes) -> str | None:
    """"audio/ogg" for Ogg (Opus), "audio/mpeg" for mp3, else None."""
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    return None


async def synthesize(text: str, config: Config) -> tuple[bytes, str] | None:
    """(audio, mime type) for the text, or None if text-to-speech isn't set up or the call failed.

    Opus is requested so Telegram can show it as a voice note. A server that sends mp3 instead
    gives "audio/mpeg", which the channel sends as an audio file.
    """
    endpoint = tts_endpoint(config)
    if endpoint is None or not text.strip():
        return None
    base_url, key, model = endpoint
    voice = config.voice.tts_voice or ("af_heart" if config.voice.tts.startswith("local") else "alloy")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    body = {"model": model, "input": text[:TTS_MAX_CHARS], "voice": voice, "response_format": "opus"}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(_audio_url(base_url, "audio/speech"), headers=headers, json=body)
            resp.raise_for_status()
            data = resp.content
    except httpx.HTTPError as e:
        logger.warning("Text-to-speech failed: %s", e)
        return None
    kind = _audio_kind(data)
    if kind is None or len(data) > TTS_MAX_BYTES:
        logger.warning("Text-to-speech returned audio that cannot be sent (%d bytes)", len(data))
        return None
    return data, kind
