"""First-run rough edges: headless with no channel, status, doctor, privacy labels, quiet start, messages."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import micropaw.cli as cli
import micropaw.config as config_mod
from micropaw import privacy

pytest_plugins = ["tests.lan_fixtures"]


def run(*args, input=None):
    return CliRunner().invoke(cli.main, list(args), input=input)


@pytest.fixture
def home(lan_home, tmp_path, monkeypatch):
    """lan_home plus the names cli bound at import, so no test reads or writes the real ~/.micropaw."""
    monkeypatch.setattr(cli, "CONFIG_FILE", lan_home)
    monkeypatch.setattr(cli, "DB_FILE", tmp_path / "data.db")
    monkeypatch.setattr(config_mod, "DB_FILE", tmp_path / "data.db")
    return lan_home


FAKE = {"provider": "ollama", "providers": {"fakeoai": {"base_url": "http://127.0.0.1:18080/v1"}},
        "agents": {"main": {"model": "fakeoai:fake-gpt"}}}


def write(home, data):
    home.write_text(json.dumps(data))


def test_headless_with_only_the_terminal_says_what_to_add_and_runs_nothing(home, monkeypatch):
    write(home, {"channels": {"terminal": {"enabled": True}}})
    monkeypatch.setattr("micropaw.service.run_headless", lambda make: pytest.fail("must not start"))
    r = run("start", "--headless")
    assert r.exit_code == 1
    assert "paw connect telegram" in r.output and "Traceback" not in r.output
    assert "paw setup" not in r.output


def test_privacy_does_not_call_an_unset_kiwix_the_internet(home):
    write(home, {"privacy": {"mode": "local"}, "search": {"provider": "offline", "kiwix_url": ""}})
    privacy._reset_for_tests()
    privacy.freeze("local")
    out = run("privacy").output
    assert "Kiwix: not set up (off)" in out and "Kiwix (the internet)" not in out


def test_status_shows_the_gateway_and_the_model_chats_really_use(home, monkeypatch):
    write(home, FAKE)
    monkeypatch.setattr("micropaw.service.running_gateway", lambda: None)
    out = run("status").output
    assert "fakeoai:fake-gpt" in out and "Gateway" in out and "not running" in out
    monkeypatch.setattr("micropaw.service.running_gateway", lambda: 4242)
    assert "running, PID 4242" in run("status").output


def test_doctor_database_not_created_is_a_note_and_a_missing_config_says_run_setup(home, monkeypatch):
    monkeypatch.setattr("micropaw.modelscan.doctor_rows", lambda config: _async([]))
    out = run("doctor").output
    assert "not found. Run 'paw setup'" in " ".join(out.split())
    assert "FAIL │ not created" not in out and "created on the first chat" in " ".join(out.split())
    db_row = next(line for line in out.splitlines() if "Database" in line)
    assert "FAIL" not in db_row


async def _async(value):
    return value


def test_doctor_provider_row_follows_the_main_model_and_keyless_models_need_no_key(home, monkeypatch):
    write(home, FAKE)
    monkeypatch.setattr("micropaw.modelscan.doctor_rows", lambda config: _async([]))
    out = " ".join(run("doctor").output.split())
    assert "Provider (fakeoai:fake-gpt)" in out and "no API key needed" in out
    assert "API key configured" not in out


def test_doctor_sandbox_off_on_purpose_in_airgapped_windows_is_a_warning():
    from micropaw.tools.sandbox import Sandbox
    assert "FAIL" in cli._sandbox_mark(Sandbox("none", error="bwrap is missing"), True)  # not air-gapped
    privacy._reset_for_tests()
    privacy.freeze("airgapped")
    try:
        assert "warn" in cli._sandbox_mark(Sandbox("none", error="Windows cannot cut a child process off"), True)
        assert "FAIL" in cli._sandbox_mark(Sandbox("none", error="bwrap is missing"), False)
    finally:
        privacy._reset_for_tests()


def test_doctor_service_state_unknown_is_a_warning_not_a_pass(home, monkeypatch):
    from micropaw import service as svc
    monkeypatch.setattr("micropaw.modelscan.doctor_rows", lambda config: _async([]))
    monkeypatch.setattr(svc, "state", lambda: (_ for _ in ()).throw(svc.ServiceError("schtasks blocked")))
    row = next(line for line in run("doctor").output.splitlines() if "Service" in line)
    assert "warn" in row and "unknown" in row
