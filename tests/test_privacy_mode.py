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


# --- the boot module -------------------------------------------------------------------------------------------------

def _run_boot(tmp_path, config: dict | None, env: dict | None = None, code: str = ""):
    import os
    import subprocess
    import sys
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if config is not None:
        (home / "config.json").write_text(json.dumps(config))
    script = f"""
import os, sys
from micropaw import _boot, privacy
mode = _boot.prepare()
print(mode, 'huggingface_hub' in sys.modules, os.environ.get('HF_HUB_OFFLINE'), os.environ.get('DO_NOT_TRACK'))
{code}
"""
    full = {k: v for k, v in os.environ.items() if k != "MICROPAW_PRIVACY"}
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
                          env={**full, "MICROPAW_HOME": str(home), **(env or {})})


def test_boot_without_a_privacy_block_is_normal_and_installs_nothing(tmp_path):
    out = _run_boot(tmp_path, {}, code="from micropaw import egress; print(egress.installed())")
    assert out.returncode == 0, out.stderr
    first, second = out.stdout.splitlines()
    assert first.split() == ["normal", "False", "None", "1"] and second == "None"  # telemetry off, offline not forced


def test_boot_airgapped_sets_the_environment_before_huggingface_loads_and_installs_the_guard(tmp_path):
    out = _run_boot(tmp_path, {"privacy": {"mode": "airgapped"}}, env={"HF_HUB_OFFLINE": "0"},
                    code="from micropaw import egress; print(egress.installed() is not None)")
    assert out.returncode == 0, out.stderr
    first, second = out.stdout.splitlines()
    assert first.split() == ["airgapped", "False", "1", "1"] and second == "True"


def test_boot_config_normal_with_env_airgapped_stays_airgapped(tmp_path):
    out = _run_boot(tmp_path, {"privacy": {"mode": "normal"}}, env={"MICROPAW_PRIVACY": "airgapped"})
    assert out.stdout.split()[0] == "airgapped"
    out = _run_boot(tmp_path, {"privacy": {"mode": "airgapped"}}, env={"MICROPAW_PRIVACY": "normal"})
    assert out.stdout.split()[0] == "airgapped"


def test_boot_exits_with_a_message_when_the_guard_cannot_be_installed(tmp_path):
    code = """
import sys
def bad(event, args):
    if event == "sys.addaudithook":
        raise RuntimeError("earlier hook")
sys.addaudithook(bad)
"""
    import os
    import subprocess
    import sys
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"privacy": {"mode": "local"}}))
    out = subprocess.run([sys.executable, "-c", code + "from micropaw import _boot\n_boot.prepare()"],
                         capture_output=True, text=True, timeout=60,
                         env={**os.environ, "MICROPAW_HOME": str(home)})
    assert out.returncode != 0
    assert "privacy.mode is local, but the network guard is not working" in out.stderr and "Not starting" in out.stderr


@pytest.mark.skipif(__import__("os").name == "nt", reason="umask is POSIX only")
def test_boot_sets_a_private_umask(tmp_path):
    out = _run_boot(tmp_path, {}, code="print(oct(os.umask(0o077)))")
    assert out.stdout.splitlines()[-1] == "0o77"


def test_the_console_script_and_python_dash_m_both_go_through_boot():
    from pathlib import Path
    root = Path(__file__).parent.parent
    toml = (root / "pyproject.toml").read_text()
    assert 'paw = "micropaw._boot:main"' in toml and 'micropaw = "micropaw._boot:main"' in toml
    assert "_boot" in (root / "src" / "micropaw" / "__main__.py").read_text()

