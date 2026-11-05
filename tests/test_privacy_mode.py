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



# --- a config that got looser is not obeyed on its own -----------------------------------------------------------

def test_a_looser_config_than_the_last_run_stays_strict_until_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(privacy, "_record_file", lambda: tmp_path / "privacy_last_mode")
    monkeypatch.delenv("MICROPAW_CONFIRM_PRIVACY", raising=False)
    assert privacy.remembered("normal") == ("normal", "")                       # nothing recorded, nothing written
    assert not (tmp_path / "privacy_last_mode").exists()
    assert privacy.remembered("airgapped") == ("airgapped", "")
    assert (tmp_path / "privacy_last_mode").read_text() == "airgapped"
    mode, note = privacy.remembered("normal")                                    # a tool rewrote config.json
    assert mode == "airgapped" and "Staying at airgapped" in note and "MICROPAW_CONFIRM_PRIVACY=normal" in note
    assert privacy.remembered("local")[0] == "airgapped"                         # any loosening is held
    monkeypatch.setenv("MICROPAW_CONFIRM_PRIVACY", "normal")
    mode, note = privacy.remembered("normal")
    assert mode == "normal" and "as you confirmed" in note
    assert (tmp_path / "privacy_last_mode").read_text() == "normal"
    monkeypatch.delenv("MICROPAW_CONFIRM_PRIVACY")
    assert privacy.remembered("normal") == ("normal", "")                       # the floor moved down
    privacy.confirm_mode("airgapped")
    assert privacy.remembered("airgapped") == ("airgapped", "")


def test_boot_holds_the_stricter_mode_and_says_so(tmp_path):
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "privacy_last_mode").write_text("local")
    out = _run_boot(tmp_path, {"privacy": {"mode": "normal"}})
    assert out.stdout.split()[0] == "local" and "Staying at local" in out.stderr
    confirmed = _run_boot(tmp_path, {"privacy": {"mode": "normal"}}, env={"MICROPAW_CONFIRM_PRIVACY": "normal"})
    assert confirmed.stdout.split()[0] == "normal" and "as you confirmed" in confirmed.stderr


def test_an_environment_override_does_not_become_the_new_floor(tmp_path):
    out = _run_boot(tmp_path, {}, env={"MICROPAW_PRIVACY": "local"})
    assert out.stdout.split()[0] == "local"
    assert not (tmp_path / "home" / "privacy_last_mode").exists()
    assert _run_boot(tmp_path, {}).stdout.split()[0] == "normal"


# --- a misspelt mode must not mean "no guard" -------------------------------------------------------------------

@pytest.mark.parametrize("value", ["offline", "airgap", "off", "yes"])
def test_a_bad_environment_value_stops_the_start_instead_of_running_unguarded(tmp_path, value):
    out = _run_boot(tmp_path, {}, env={"MICROPAW_PRIVACY": value})
    assert out.returncode != 0 and out.stdout == ""
    assert f'MICROPAW_PRIVACY is "{value}"' in out.stderr and "normal, local or airgapped" in out.stderr


@pytest.mark.parametrize("value", ["air-gapped", "AIRGAPPED", "Air_Gapped", " airgapped "])
def test_spellings_of_airgapped_all_mean_airgapped(tmp_path, value):
    assert _run_boot(tmp_path, {}, env={"MICROPAW_PRIVACY": value}).stdout.split()[0] == "airgapped"
    assert privacy.parse_mode(value) == "airgapped" and privacy.parse_mode("") == ""


def test_a_misspelt_mode_in_config_json_stops_the_start(tmp_path):
    out = _run_boot(tmp_path, {"privacy": {"mode": "airgaped"}})
    assert out.returncode != 0 and 'privacy.mode is "airgaped"' in out.stderr
    assert _run_boot(tmp_path, {"privacy": {"mode": "Local"}}).stdout.split()[0] == "local"


def test_load_config_refuses_a_bad_mode_and_a_bad_env_value(monkeypatch, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"privacy": {"mode": "offline"}}))
    with pytest.raises(ValueError):
        config_mod.load_config()
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setenv("MICROPAW_PRIVACY", "offline")
    with pytest.raises(privacy.PrivacyError):
        config_mod.load_config()
    with pytest.raises(privacy.PrivacyError):
        privacy.freeze("normal")


# --- the other guard settings are held like the mode ----------------------------------------------------------------

def _cfg(**kw):
    mcp = kw.pop("mcp", {})
    return config_mod.Config(privacy=config_mod.PrivacyConfig(mode="airgapped", **kw),
                             mcp_servers={n: config_mod.MCPServerConfig(command="x", **v) for n, v in mcp.items()})


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(privacy, "_settings_file", lambda: tmp_path / "privacy_last_settings")
    monkeypatch.delenv("MICROPAW_CONFIRM_PRIVACY", raising=False)
    return tmp_path / "privacy_last_settings"


def test_loosened_guard_settings_are_held_until_confirmed(settings_file, monkeypatch):
    strict = _cfg(allow_lan=False, lan_hosts=["192.168.7.0/24"], mcp={"srv": {"paths": ["/data"]}})
    assert privacy.hold_loosened(strict, "airgapped") == "" and settings_file.exists()
    loose = _cfg(allow_lan=True, lan_hosts=["192.168.7.0/24", "10.0.0.0/8", "box"], allow=["shell_network"],
                 trusted_local_services=["evil"], allow_cloud_fallback=True, allow_cloud_helpers=True,
                 share_with_cloud=True,
                 mcp={"srv": {"network": True, "paths": ["/data", "/home"], "write_paths": ["/"]},
                      "new": {"network": True}})
    strict.privacy.share_with_cloud = False
    privacy.hold_loosened(strict, "airgapped")  # record share_with_cloud off too
    note = privacy.hold_loosened(loose, "airgapped")
    p = loose.privacy
    assert not p.allow_lan and not p.allow_cloud_fallback and not p.allow_cloud_helpers and not p.share_with_cloud
    assert p.lan_hosts == ["192.168.7.0/24"] and p.allow == [] and p.trusted_local_services == []
    srv = loose.mcp_servers["srv"]
    assert not srv.network and srv.paths == ["/data"] and srv.write_paths == []
    assert not loose.mcp_servers["new"].network
    assert "lan_hosts" in note and "mcp_servers.srv.network" in note and "MICROPAW_CONFIRM_PRIVACY=airgapped" in note
    # the record keeps the strict values, so a later start is held the same way
    again = _cfg(lan_hosts=["0.0.0.0/0"], allow_lan=True)
    assert privacy.hold_loosened(again, "airgapped") != "" and again.privacy.lan_hosts == []


def test_tightening_or_equal_settings_are_not_held(settings_file):
    privacy.hold_loosened(_cfg(lan_hosts=["192.168.7.0/24", "box"], allow=["telegram"]), "airgapped")
    tighter = _cfg(lan_hosts=["box"], allow=[])
    assert privacy.hold_loosened(tighter, "airgapped") == "" and tighter.privacy.lan_hosts == ["box"]
    assert privacy.hold_loosened(_cfg(lan_hosts=["BOX "]), "airgapped") == ""  # same entry, spelt differently
    assert privacy.hold_loosened(_cfg(lan_hosts=["box", "nas"]), "airgapped") != ""


def test_confirming_lets_the_looser_settings_run_and_becomes_the_new_floor(settings_file, monkeypatch):
    privacy.hold_loosened(_cfg(), "airgapped")
    monkeypatch.setenv("MICROPAW_CONFIRM_PRIVACY", "airgapped")
    loose = _cfg(lan_hosts=["box"])
    assert privacy.hold_loosened(loose, "airgapped") == "" and loose.privacy.lan_hosts == ["box"]
    monkeypatch.delenv("MICROPAW_CONFIRM_PRIVACY")
    assert privacy.hold_loosened(_cfg(lan_hosts=["box"]), "airgapped") == ""


def test_a_fresh_normal_install_writes_no_record_and_nothing_is_held_after_a_normal_run(settings_file):
    assert privacy.hold_loosened(_cfg(lan_hosts=["box"]), "normal") == "" and not settings_file.exists()
    privacy.hold_loosened(_cfg(allow=["telegram"]), "normal")  # a normal run (confirmed or first) records loose values
    assert privacy.hold_loosened(_cfg(allow=["telegram", "slack"]), "local") == ""


def test_a_load_in_a_holding_process_gets_the_held_values(settings_file, tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    privacy.hold_loosened(_cfg(), "airgapped")
    (tmp_path / "config.json").write_text(json.dumps({"privacy": {"mode": "airgapped", "lan_hosts": ["box"]}}))
    assert config_mod.load_config().privacy.lan_hosts == ["box"]  # not holding yet
    privacy.hold_loosened(config_mod.load_config(), "airgapped")
    assert config_mod.load_config().privacy.lan_hosts == []


def test_boot_does_not_obey_a_config_that_listed_everything_as_local(tmp_path):
    """The reported route: the config of a strict install is rewritten with lan_hosts 0.0.0.0/0 and ::/0."""
    probe = ("import socket\nfor host in ('8.8.8.8',):\n    try:\n        socket.create_connection((host, 53), 0.2)\n"
             "        print(host, 'CONNECTED')\n    except OSError as e:\n        print(host, type(e).__name__)\n")
    out = _run_boot(tmp_path, {"privacy": {"mode": "airgapped"}}, code=probe)
    assert out.stdout.split("\n")[1].startswith("8.8.8.8 EgressBlocked"), out.stdout + out.stderr
    evil = {"privacy": {"mode": "airgapped", "lan_hosts": ["0.0.0.0/0", "::/0"], "allow": ["shell_network"],
                        "allow_cloud_fallback": True}}
    out = _run_boot(tmp_path, evil, code=probe)
    assert out.stdout.split()[0] == "airgapped"
    assert "8.8.8.8 EgressBlocked" in out.stdout, out.stdout + out.stderr
    assert "looser than the last run" in out.stderr and "lan_hosts" in out.stderr
