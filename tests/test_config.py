"""Tests for MicroClaw configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from microclaw.config import (
    Config,
    _apply_env_overrides,
    _redact,
    display_config,
    load_config,
    save_config,
)


class TestConfig:
    """Tests for the Config model defaults."""

    def test_defaults(self):
        c = Config()
        assert c.provider == "claude"
        assert c.config_version == "1.0"
        assert c.fallback_providers == []

    def test_provider_configs_exist(self):
        c = Config()
        assert c.provider_config.claude.model == "claude-sonnet-4-20250514"
        assert c.provider_config.openai.model == "gpt-4o"
        assert c.provider_config.ollama.host == "http://localhost:11434"
        assert c.provider_config.gemini.model == "gemini-2.5-flash"

    def test_channel_defaults(self):
        c = Config()
        assert c.channels.terminal.enabled is True
        assert c.channels.telegram.enabled is False
        assert c.channels.webchat.port == 8765

    def test_permissions_defaults(self):
        c = Config()
        assert c.permissions.allow_shell is False
        assert c.permissions.allow_file_write is False
        assert c.permissions.allow_web_browse is True
        assert c.permissions.disabled_tools == []
        assert c.permissions.confirm_dangerous is True

    def test_base_url_defaults(self):
        c = Config()
        assert c.provider_config.claude.base_url == "https://api.anthropic.com"
        assert c.provider_config.openai.base_url == "https://api.openai.com"
        assert c.provider_config.gemini.base_url == "https://generativelanguage.googleapis.com"

    def test_persona_defaults(self):
        c = Config()
        assert c.persona.name == "MicroClaw"


class TestEnvOverrides:
    """Tests for environment variable override logic."""

    def test_apply_anthropic_key(self):
        raw: dict = {}
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-123"}):
            _apply_env_overrides(raw)
        assert raw["provider_config"]["claude"]["api_key"] == "sk-test-123"

    def test_apply_telegram_token(self):
        raw: dict = {}
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "12345:ABC"}, clear=False):
            _apply_env_overrides(raw)
        assert raw["channels"]["telegram"]["token"] == "12345:ABC"

    def test_no_override_when_env_unset(self):
        raw: dict = {}
        with patch.dict(os.environ, {}, clear=True):
            _apply_env_overrides(raw)
        assert raw == {}

    def test_apply_microclaw_provider(self):
        raw: dict = {}
        with patch.dict(os.environ, {"MICROCLAW_PROVIDER": "ollama"}, clear=False):
            _apply_env_overrides(raw)
        assert raw["provider"] == "ollama"


class TestRedaction:
    """Tests for secret redaction."""

    def test_redact_long_key(self):
        assert _redact("sk-ant-abc123456789xyz") == "sk-ant-a...9xyz"

    def test_redact_short_key(self):
        assert _redact("short") == "***"

    def test_redact_empty(self):
        assert _redact("") == ""

    def test_display_config_redacts_keys(self):
        c = Config()
        c.provider_config.claude.api_key = "sk-ant-very-long-secret-key"
        data = display_config(c)
        key = data["provider_config"]["claude"]["api_key"]
        assert "very-long" not in key
        assert key.startswith("sk-ant-v")
        assert key.endswith("key")


class TestSaveLoad:
    """Tests for config save/load cycle."""

    def test_save_and_load(self, tmp_path: Path):
        config_file = tmp_path / "config.json"
        config_dir = tmp_path

        with (
            patch("microclaw.config.CONFIG_FILE", config_file),
            patch("microclaw.config.CONFIG_DIR", config_dir),
        ):
            original = Config(provider="openai")
            save_config(original)

            assert config_file.exists()

            loaded = load_config()
            assert loaded.provider == "openai"

    def test_load_missing_file_returns_defaults(self, tmp_path: Path):
        config_file = tmp_path / "nonexistent.json"
        with patch("microclaw.config.CONFIG_FILE", config_file):
            # Clear env vars that might interfere
            with patch.dict(os.environ, {}, clear=True):
                c = load_config()
            assert c.provider == "claude"
