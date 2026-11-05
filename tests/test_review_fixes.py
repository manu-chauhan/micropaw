"""Fixes from the round 3 review of the LAN, search and setup work."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from click.testing import CliRunner

from micropaw import setup_wizard as wiz
from micropaw.cli import main
from micropaw.config import Config
from tests.test_wizard_models import Answer, Choice, Scripted, patch_env

pytest_plugins = ["tests.lan_fixtures"]


class Sequence(Scripted):
    """Answers a text question from a list, one per ask, then blank."""

    def __init__(self, texts, rules=None):
        super().__init__(rules)
        self.texts = {word: list(v) for word, v in texts.items()}

    def text(self, message, default=""):
        for word, values in self.texts.items():
            if word in message:
                self.asked.append((message, default))
                return Answer(values.pop(0) if values else "")
        return super().text(message, default)


def _install(monkeypatch, q):
    monkeypatch.setitem(sys.modules, "questionary", SimpleNamespace(
        select=q.select, confirm=q.confirm, text=q.text, password=q.password, checkbox=q.checkbox, Choice=Choice))


# --- a mistyped address in the wizard -----------------------------------------------------------

def test_a_mistyped_kiwix_or_searxng_address_is_asked_again_not_a_crash(monkeypatch, capsys):
    patch_env(monkeypatch, [])
    q = Sequence({"Kiwix server": ["my kiwix box", "localhost:8080"], "Your own SearXNG": ["a b", "a b", "a b"]},
                 {"How private": "local"})
    c = Config()
    wiz._choose_privacy(q, c)
    assert c.search.kiwix_url == "http://localhost:8080"
    assert c.search.searxng_url == "" and c.search.provider == "offline"  # gave up after three tries
    assert "not an address" in capsys.readouterr().out


def test_a_mistyped_ollama_host_is_never_saved(monkeypatch, capsys):
    patch_env(monkeypatch, [])
    saved = []
    c = Config()
    monkeypatch.setattr(wiz, "config_exists", lambda: False)
    monkeypatch.setattr(wiz, "save_config", lambda cfg: saved.append(cfg))
    q = Scripted({"How private": "local", "Ollama host:": "foo bar"})
    _install(monkeypatch, q)
    monkeypatch.setattr(wiz, "Config", lambda: c)
    wiz.run_setup()
    assert saved and saved[0].provider_config.ollama.host == "http://localhost:11434"
    Config(**json.loads(saved[0].model_dump_json()))  # what was saved loads again
    assert "not an address" in capsys.readouterr().out


def test_a_good_ollama_host_is_saved_as_a_full_address(monkeypatch):
    patch_env(monkeypatch, [])
    saved = []
    monkeypatch.setattr(wiz, "config_exists", lambda: False)
    monkeypatch.setattr(wiz, "save_config", lambda cfg: saved.append(cfg))
    _install(monkeypatch, Scripted({"How private": "local", "Ollama host:": "192.168.1.50"}))
    wiz.run_setup()
    assert saved[0].provider_config.ollama.host == "http://192.168.1.50:11434"


def test_a_bad_config_file_is_one_plain_line_and_setup_reset_recovers(lan_home, monkeypatch):
    lan_home.write_text(json.dumps({"provider_config": {"ollama": {"host": "foo bar"}}}))
    for args in (["status"], ["models", "scan"], ["setup"]):
        r = CliRunner().invoke(main, args, input="\n")
        out = r.output
        assert r.exit_code == 1 and "Traceback" not in out and "ValidationError" not in out, (args, out)
        assert str(lan_home) in out and "paw setup --reset" in out
        assert sum("Error:" in line for line in out.splitlines()) == 1
    patch_env(monkeypatch, [])
    saved = []
    monkeypatch.setattr(wiz, "save_config", lambda cfg: saved.append(cfg))
    _install(monkeypatch, Scripted({"How private": "local"}))
    wiz.run_setup(reset=True)
    assert saved and lan_home.with_name("config.json.bak").exists()
    kept = json.loads(lan_home.with_name("config.json.bak").read_text())
    assert kept["provider_config"]["ollama"]["host"] == "foo bar"
