"""Tests for per-job models ("provider:model") and OpenAI-compatible presets."""

from __future__ import annotations

from micropaw.config import Config
from micropaw.providers.registry import Registry


def _create_role_provider(spec, config):
    return Registry(config).get(spec) if spec else None


def test_unset_means_main_model():
    assert _create_role_provider("", Config()) is None


def test_ollama_model_names_keep_their_colon():
    p = _create_role_provider("ollama:gemma4:e4b", Config())
    assert p.name == "ollama" and p.model == "gemma4:e4b"


def test_local_openai_compatible_server_needs_no_key():
    p = _create_role_provider("lmstudio:qwen3.6", Config())
    assert p.model == "qwen3.6" and p._api_url == "http://localhost:1234/v1/chat/completions"


def test_hosted_preset_reads_its_key_from_env(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert _create_role_provider("groq:llama-3.3-70b-versatile", Config()) is None
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    p = _create_role_provider("groq:llama-3.3-70b-versatile", Config())
    assert p._api_url == "https://api.groq.com/openai/v1/chat/completions"


def test_claude_role_uses_claude_key():
    config = Config()
    config.provider_config.claude.api_key = "sk-test"
    p = _create_role_provider("claude:claude-haiku-5-5", config)
    assert p.model == "claude-haiku-5-5"


def test_unknown_provider_falls_back_to_main():
    assert _create_role_provider("nope:model", Config()) is None


def test_env_overrides(monkeypatch, tmp_path):
    import micropaw.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "none.json")
    monkeypatch.setenv("MICROPAW_FAST_MODEL", "ollama:qwen3.6")
    assert config_mod.load_config().models.fast == "ollama:qwen3.6"


def test_voice_stt_still_resolves_through_the_presets(monkeypatch):
    from micropaw.utils.voice import _audio_url, _endpoint

    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    c = Config(voice={"stt": "groq:whisper-large-v3"})
    base, key, model = _endpoint(c)
    assert (key, model) == ("gsk-test", "whisper-large-v3")
    assert _audio_url(base) == "https://api.groq.com/openai/v1/audio/transcriptions"
    c.voice.stt = "lmstudio:whisper"
    assert _audio_url(_endpoint(c)[0]) == "http://localhost:1234/v1/audio/transcriptions"
    monkeypatch.delenv("GROQ_API_KEY")
    c.voice.stt = "groq:whisper-large-v3"
    assert _endpoint(c) is None
    assert _audio_url("http://localhost:8000") == "http://localhost:8000/v1/audio/transcriptions"  # "local:" style
