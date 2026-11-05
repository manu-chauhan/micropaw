"""The privacy mode setting: read once, and only ever tightened."""

from __future__ import annotations

import json

import pytest

import micropaw.config as config_mod
from micropaw import privacy


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("MICROPAW_PRIVACY", raising=False)
    privacy._reset_for_tests()
    yield
    privacy._reset_for_tests()


def test_default_is_normal():
    assert config_mod.Config().privacy.mode == "normal"
    assert privacy.freeze("normal") == "normal" and not privacy.local_only()


def test_env_can_only_tighten(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"privacy": {"mode": "airgapped"}}))
    monkeypatch.setenv("MICROPAW_PRIVACY", "normal")
    assert config_mod.load_config().privacy.mode == "airgapped"
    monkeypatch.setenv("MICROPAW_PRIVACY", "local")
    (tmp_path / "config.json").write_text(json.dumps({"privacy": {"mode": "normal"}}))
    assert config_mod.load_config().privacy.mode == "local"


def test_frozen_mode_never_loosens(monkeypatch):
    monkeypatch.setenv("MICROPAW_PRIVACY", "airgapped")
    assert privacy.freeze("normal") == "airgapped"
    assert privacy.freeze("normal") == "airgapped" and privacy.airgapped() and privacy.local_only()
