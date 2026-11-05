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


# --- scan and add stay on this computer and your network in local and air-gapped modes -------------

import pytest  # noqa: E402

import micropaw.modelscan as ms  # noqa: E402
from micropaw import privacy  # noqa: E402

CLOUD = "https://api.example-cloud.test/v1"


@pytest.fixture
def mode_of(lan_home):
    def set_mode(mode, **extra):
        lan_home.write_text(json.dumps({"privacy": {"mode": mode}, **extra}))
        privacy._reset_for_tests()
    yield set_mode
    privacy._reset_for_tests()


@pytest.fixture
def probed(monkeypatch):
    calls = []

    async def fake(name, kind, url, key="", details=True, connect=None):
        calls.append((url, key))
        return None

    monkeypatch.setattr(ms, "probe_target", fake)
    return calls


@pytest.mark.parametrize("mode", ["local", "airgapped"])
def test_scan_never_contacts_a_cloud_entry_or_sends_its_key(mode_of, probed, monkeypatch, mode):
    mode_of(mode, providers={"cloudy": {"base_url": CLOUD, "api_key_env": "CLOUDY_KEY"}})
    monkeypatch.setenv("CLOUDY_KEY", "sk-secret")
    r = CliRunner().invoke(main, ["models", "scan"])
    assert r.exit_code == 0, r.output
    assert "cloudy" in r.output and "skipped: not on this computer or your network" in r.output
    assert probed and not any("example-cloud" in u or k for u, k in probed)


def test_scan_in_normal_mode_still_asks_the_cloud_entry(mode_of, probed, monkeypatch):
    mode_of("normal", providers={"cloudy": {"base_url": CLOUD, "api_key_env": "CLOUDY_KEY"}})
    monkeypatch.setenv("CLOUDY_KEY", "sk-secret")
    CliRunner().invoke(main, ["models", "scan"])
    assert (CLOUD.removesuffix("/v1"), "sk-secret") in probed


def test_scan_host_outside_the_network_is_refused_in_local_modes(mode_of, probed):
    mode_of("local")
    r = CliRunner().invoke(main, ["models", "scan", "--host", "8.8.8.8"])
    assert r.exit_code == 1 and "8.8.8.8 is not on this computer or your network" in r.output
    assert not probed
    r = CliRunner().invoke(main, ["models", "scan", "--host", "192.168.1.50"])  # your own network is fine
    assert r.exit_code == 0 and any("192.168.1.50" in u for u, _ in probed)


@pytest.mark.parametrize("address", [CLOUD, "http://8.8.8.8:11434"])
def test_add_to_a_non_local_address_is_refused_before_anything_is_sent(mode_of, probed, monkeypatch, address):
    mode_of("local")
    monkeypatch.setenv("K", "sk-secret")
    r = CliRunner().invoke(main, ["models", "add", "t1", address, "--key-env", "K"])
    assert r.exit_code == 1 and ("this computer or your network" in r.output or "Could not find" in r.output)
    assert not probed


# --- the refusal says the real cause ---------------------------------------------------------------

@pytest.mark.parametrize("address", ["gpu-box.local", "myserver", "http://example.com:11434"])
def test_an_unknown_name_is_a_lookup_problem_not_an_https_one(mode_of, probed, address, monkeypatch):
    mode_of("normal")
    monkeypatch.setattr(ms.privacy, "is_local_url", lambda url: False)  # the name does not resolve to a private address
    r = CliRunner().invoke(main, ["models", "add", "t1", address])
    host = address.removeprefix("http://").split(":")[0]
    assert r.exit_code == 1 and f"Could not find {host} on your network" in r.output
    assert "IP address" in r.output and "privacy.lan_hosts" in r.output
    assert "https" not in r.output and "clear text" not in r.output
    assert not probed


def test_a_public_ip_over_http_names_clear_text_and_the_key_only_when_there_is_one(mode_of, monkeypatch):
    mode_of("normal")
    monkeypatch.setenv("K", "x")
    text = ms.refusal_for(Config(), "t1", "http://8.8.8.8:11434", "")
    assert "clear text" in text and "key" not in text and "Use https" in text
    assert "messages and key" in ms.refusal_for(Config(), "t1", "http://8.8.8.8:11434", "K")
    assert ms.refusal_for(Config(), "t1", "https://8.8.8.8:11434", "K") is None  # https to a public host is fine


def test_the_wizard_says_it_could_not_find_the_name_before_probing(monkeypatch, capsys, mode_of):
    mode_of("normal")
    monkeypatch.setattr(ms.privacy, "is_local_url", lambda url: False)

    async def no_probe(*a, **k):
        pytest.fail("a refused address is never probed")

    monkeypatch.setattr(ms, "detect", no_probe)
    monkeypatch.setattr(ms, "probe_host", no_probe)
    c = Config()
    wiz._add_remote(Scripted({"Address of the other computer": "gpu-box.local"}), c, "ollama")
    out = capsys.readouterr().out
    assert "Could not find gpu-box.local" in out and "Nothing answered" not in out and not c.providers
