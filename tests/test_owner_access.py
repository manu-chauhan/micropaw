"""Tests for the security hotfix: owner-only access, paused cron jobs, path and URL hardening."""

from __future__ import annotations

import json
import os
import stat
from unittest.mock import AsyncMock, MagicMock

import pytest

import micropaw.config as config_mod
import micropaw.gateway as gateway_mod
from micropaw.config import Config
from micropaw.gateway import Gateway, _AccessGuard


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """Point the config file at a temp dir for both modules that read it."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", path)
    monkeypatch.setattr(gateway_mod, "CONFIG_FILE", path)
    return path


class TestAccessGuard:
    def test_empty_allowlist_means_nobody(self, cfg_file):
        guard = _AccessGuard(Config())
        assert guard.is_allowed("telegram", "123") is False
        assert guard.is_allowed("slack", "U1") is False

    def test_listed_id_allowed(self, cfg_file):
        config = Config()
        config.channels.telegram.allowed_users = ["123"]
        assert _AccessGuard(config).is_allowed("telegram", "123") is True

    def test_local_and_internal_platforms_allowed(self, cfg_file):
        guard = _AccessGuard(Config())
        for platform in ("terminal", "webchat", "heartbeat", "system"):
            assert guard.is_allowed(platform, "anyone")

    def test_stranger_told_their_id_once(self, cfg_file):
        guard = _AccessGuard(Config())
        first = guard.check("telegram", "555")
        assert "555" in first and "paw allow telegram 555" in first
        assert guard.check("telegram", "555") == ""  # silent afterwards

    def test_global_reply_cap(self, cfg_file):
        guard = _AccessGuard(Config())
        replies = [guard.check("discord", str(i)) for i in range(guard._MAX_REPLIES_PER_DAY + 5)]
        assert sum(1 for r in replies if r) == guard._MAX_REPLIES_PER_DAY

    def test_paw_allow_takes_effect_without_restart(self, cfg_file):
        guard = _AccessGuard(Config())
        assert not guard.is_allowed("telegram", "777")
        config_mod.set_allowed_user("telegram", "777")
        os.utime(cfg_file, (1, 1))  # make sure the mtime differs from the guard's snapshot
        assert guard.is_allowed("telegram", "777")


class TestSetAllowedUser:
    def test_adds_removes_and_keeps_other_keys(self, cfg_file, monkeypatch):
        cfg_file.write_text(json.dumps({"provider": "ollama"}))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-written")
        assert config_mod.set_allowed_user("slack", "U1") == ["U1"]
        assert config_mod.set_allowed_user("slack", "U1") == ["U1"]  # no duplicates
        raw = json.loads(cfg_file.read_text())
        assert raw["provider"] == "ollama"
        assert "sk-should-not-be-written" not in cfg_file.read_text()
        assert config_mod.set_allowed_user("slack", "U1", remove=True) == []
        if os.name == "posix":
            assert stat.S_IMODE(cfg_file.stat().st_mode) == 0o600


class TestGatewayGate:
    def _gw(self) -> Gateway:
        gw = Gateway(Config(), MagicMock(), MagicMock(), [])
        gw.agent = MagicMock()
        gw.agent.handle_message = AsyncMock(return_value="AI response")
        gw.agent.last_usage = {"input": 0, "output": 0}
        return gw

    @pytest.mark.asyncio
    async def test_stranger_never_reaches_agent(self, cfg_file):
        gw = self._gw()
        reply = await gw._handle_message("hi", "999", "telegram", None)
        assert "private" in reply
        gw.agent.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_message_not_sent_to_model(self, cfg_file):
        gw = self._gw()
        reply = await gw._handle_message("   ", "owner", "terminal", None)
        assert "text" in reply
        gw.agent.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_cron_enable_is_handled_by_code(self, cfg_file):
        gw = self._gw()
        gw._scheduler = MagicMock()
        gw._scheduler.set_enabled = AsyncMock(return_value={"id": 4, "description": "check inbox"})
        reply = await gw._handle_message("/cron enable 4", "owner", "terminal", None)
        gw._scheduler.set_enabled.assert_awaited_once_with(4, True)
        assert "check inbox" in reply
        gw.agent.handle_message.assert_not_called()


class TestPathHardening:
    @pytest.mark.parametrize("rel", [
        ".git-credentials", ".config/gh/hosts.yml", ".npmrc", ".pypirc", ".ssh/id_ed25519", ".aws/credentials",
    ])
    def test_credential_files_unreadable(self, rel):
        from micropaw.tools.path_security import validate_file_path
        assert validate_file_path(os.path.join(os.path.expanduser("~"), rel)) is not None

    @pytest.mark.parametrize("rel", [
        ".bash_aliases", ".zshenv", ".config/systemd/user/x.service", ".local/bin/ls",
        "proj/.git/hooks/pre-commit", "venv/lib/python3.12/site-packages/evil.pth",
        ".config/fish/config.fish",
    ])
    def test_code_running_files_unwritable(self, rel):
        from micropaw.tools.path_security import validate_write_path
        assert validate_write_path(os.path.join(os.path.expanduser("~"), rel)) is not None

    def test_ordinary_file_still_writable(self, tmp_path):
        from micropaw.tools.path_security import validate_write_path
        assert validate_write_path(str(tmp_path / "notes.md")) is None

    def test_windows_separators_normalized(self):
        from micropaw.tools.path_security import _norm
        assert "/.ssh/" in _norm("C:\\Users\\x\\.ssh\\id_rsa")


class TestUrlAndIds:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ["http://100.100.100.200/latest/meta-data", "http://100.64.0.1/"])
    async def test_cgnat_blocked(self, url):
        from micropaw.security import validate_url
        assert await validate_url(url) is not None

    @pytest.mark.parametrize("value", ["../../work@group.calendar.google.com", "abc/def", "..", ""])
    def test_bad_google_ids(self, value):
        from micropaw.tools.google_auth import bad_id
        assert bad_id(value, "event_id") is not None

    @pytest.mark.parametrize("value", ["18c2f0a9b7d1e3f4", "abc123_20261027T150000Z"])
    def test_good_google_ids(self, value):
        from micropaw.tools.google_auth import bad_id
        assert bad_id(value, "event_id") is None


class TestShellEnv:
    def test_secrets_not_passed_to_children(self, monkeypatch):
        from micropaw.tools.shell import _child_env
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "x")
        env = _child_env()
        assert not {"ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "WHATSAPP_APP_SECRET"} & env.keys()
        assert "PATH" in env


class TestWhatsAppSecretRequired:
    @pytest.mark.asyncio
    async def test_refuses_to_start_without_app_secret(self):
        from micropaw.channels.whatsapp import WhatsAppChannel
        with pytest.raises(RuntimeError, match="app_secret"):
            await WhatsAppChannel(token="t", phone_id="p").start()
