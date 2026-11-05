"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path, monkeypatch):
    # Tool calls are audited; keep test runs out of the real ~/.micropaw/audit.jsonl
    import micropaw.utils.audit as audit
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit.jsonl")


@pytest.fixture(autouse=True)
def _privacy_normal(monkeypatch):
    # Tests run in normal mode with default privacy settings, never the developer's own config.json
    from micropaw import privacy
    from micropaw.config import PrivacyConfig
    monkeypatch.delenv("MICROPAW_PRIVACY", raising=False)
    privacy._reset_for_tests()
    privacy.freeze("normal")
    privacy.configure(PrivacyConfig())
    yield
    privacy._reset_for_tests()
