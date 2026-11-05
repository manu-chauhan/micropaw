"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path, monkeypatch):
    # Tool calls are audited; keep test runs out of the real ~/.micropaw/audit.jsonl
    import micropaw.utils.audit as audit
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit.jsonl")


_BOOT_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy",
             "HF_HUB_OFFLINE", "OLLAMA_NO_CLOUD", "HF_HUB_DISABLE_XET", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
             "HF_HUB_DISABLE_TELEMETRY", "DO_NOT_TRACK", "HF_HUB_DISABLE_IMPLICIT_TOKEN", "HF_HUB_DISABLE_UPDATE_CHECK")


@pytest.fixture(autouse=True)
def _privacy_normal(monkeypatch):
    # Tests run in normal mode with default privacy settings, never the developer's own config.json.
    # privacy.boot_env writes os.environ directly, so what it sets is put back after each test.
    import os

    from micropaw import privacy
    from micropaw.config import PrivacyConfig
    saved = {k: os.environ.get(k) for k in _BOOT_ENV}
    monkeypatch.delenv("MICROPAW_PRIVACY", raising=False)
    privacy._reset_for_tests()
    privacy.freeze("normal")
    privacy.configure(PrivacyConfig())
    yield
    privacy._reset_for_tests()
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
