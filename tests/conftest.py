"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path, monkeypatch):
    # Tool calls are audited; keep test runs out of the real ~/.micropaw/audit.jsonl
    import micropaw.utils.audit as audit
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit.jsonl")
