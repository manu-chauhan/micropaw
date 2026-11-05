"""paw privacy, /privacy, /status, doctor and the line at the end of a reply."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from micropaw import privacy, privacy_report
from micropaw.cli import main
from micropaw.config import Config
from micropaw.providers.registry import Registry


@pytest.fixture
def mode():
    def set_mode(m):
        privacy._reset_for_tests()
        privacy.freeze(m)

    yield set_mode
    privacy._reset_for_tests()


def cfg(**kw) -> Config:
    c = Config(**kw)
    c.provider_config.claude.api_key = "sk-test"
    return c


def test_the_table_names_every_role_with_its_destination(mode):
    mode("normal")
    c = cfg(models={"fast": "ollama:small"}, voice={"stt": "local:whisper"},
            search={"provider": "off"}, mcp_servers={"files": {"command": "npx"}})
    text = privacy_report.render(c)
    assert "Privacy: normal" in text
    assert "- main model (main agent): claude at api.anthropic.com (the internet)" in text
    assert "- fast model (main agent): ollama:small at localhost:11434 (this computer)" in text
    assert "- speech to text: localhost:8000 (this computer)" in text
    assert "- text to speech: off" in text and "- web search: off (-)" in text
    assert "- MCP files: a program on this computer, which may use the network itself (this computer)" in text
    assert "MicroPaw cannot check" in text


@pytest.mark.parametrize("m,needle", [
    ("airgapped", "air-gapped"), ("local", "local: models"), ("normal", "normal:")])
def test_paw_privacy_prints_one_snapshot_per_mode(mode, monkeypatch, tmp_path, m, needle):
    import micropaw.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"privacy": {"mode": m}, "provider": "ollama"}))
    mode(m)
    r = CliRunner().invoke(main, ["privacy"])
    assert r.exit_code == 0, r.output
    assert f"Privacy: {m}" in r.output and needle in r.output and "Where data goes:" in r.output
    if m == "airgapped":
        assert "web pages (http_request): off" in r.output and "web browser: off" in r.output


def test_status_line_shows_the_mode_and_a_pending_change(mode):
    mode("local")
    assert privacy_report.status_line(cfg(privacy={"mode": "local"})) == "Privacy: local (your network allowed)"
    assert privacy_report.status_line(cfg(privacy={"mode": "local", "allow_lan": False})) \
        == "Privacy: local (this computer only)"
    assert privacy_report.status_line(cfg(privacy={"mode": "airgapped"})).endswith("Pending after restart: airgapped")


def test_destination_kinds():
    assert privacy_report.destination("http://127.0.0.1:11434") == ("127.0.0.1:11434", "this computer")
    assert privacy_report.destination("http://192.168.1.50:11434")[1] == "your network"
    assert privacy_report.destination("http://100.101.102.103:11434")[1] == "your tailnet"
    assert privacy_report.destination("https://api.anthropic.com")[1] == "the internet"


def test_labels_name_the_host_and_the_fallback_that_answered():
    c = cfg(providers={"desktop": {"base_url": "http://192.168.1.50:11434", "kind": "ollama"}})
    reg = Registry(c)
    assert privacy_report.reply_label("claude", "hi", reg, c) == "\n\n[via claude (api.anthropic.com)]"
    assert privacy_report.reply_label("ollama:x", "hi", reg, c) == "\n\n[via ollama on this machine]"
    assert privacy_report.reply_label("desktop:m", "hi", reg, c) \
        == "\n\n[via desktop (192.168.1.50:11434, your network)]"
    note = "hi\n\n[answered by groq:llama because claude timed out]"
    assert privacy_report.reply_label("claude", note, reg, c).startswith("\n\n[via groq (api.groq.com")
    c.display.show_provider = False
    assert privacy_report.reply_label("claude", "hi", reg, c) == ""


def test_the_gateway_label_uses_the_agents_main_model_and_skips_internal_runs():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from micropaw.core.profile import AgentProfile
    from micropaw.gateway import Gateway
    c = cfg()
    gw = Gateway(c, MagicMock(), MagicMock(), [], models=Registry(c))
    agent = SimpleNamespace(profile=AgentProfile(name="main", main="claude"))
    assert gw._reply_label(agent, "terminal", "hello") == "\n\n[via claude (api.anthropic.com)]"
    assert gw._reply_label(agent, "heartbeat", "hello") == ""
    assert Gateway(c, MagicMock(), MagicMock(), [])._reply_label(agent, "terminal", "hello") == ""


async def test_status_and_privacy_commands(mode):
    from unittest.mock import MagicMock

    from micropaw.gateway import Gateway
    mode("local")
    c = cfg(privacy={"mode": "local"})
    gw = Gateway(c, MagicMock(), MagicMock(), [])
    agent = type("A", (), {"name": "main", "profile": None, "provider": type("P", (), {"name": "claude"})()})()
    gw.agents, gw.agent = {"main": agent}, agent
    out = await gw._owner_command("/status", "terminal", "u", None)
    assert out.startswith("Privacy: local (your network allowed)")
    assert "Where data goes:" in await gw._owner_command("/privacy", "terminal", "u", None)


def test_doctor_and_status_commands_show_the_privacy_mode(mode, monkeypatch, tmp_path):
    import micropaw.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    mode("normal")
    r = CliRunner().invoke(main, ["status"])
    assert r.exit_code == 0 and "Privacy" in r.output and "normal" in r.output


def test_doctor_warns_when_ollama_cloud_is_not_disabled_and_reads_the_server_file(mode, monkeypatch, tmp_path):
    mode("local")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    c = cfg(provider="ollama")
    byname = {n: (m, d) for n, m, d in privacy_report.advice(c)}
    assert byname["Ollama cloud models"][0] == "note" and "not found" in byname["Ollama cloud models"][1]
    (tmp_path / ".ollama").mkdir()
    server = tmp_path / ".ollama" / "server.json"
    server.write_text(json.dumps({"disable_ollama_cloud": False}))
    assert {n: m for n, m, _ in privacy_report.advice(c)}["Ollama cloud models"] == "warn"
    server.write_text(json.dumps({"disable_ollama_cloud": True}))
    assert "Ollama cloud models" not in {n for n, _, _ in privacy_report.advice(c)}
    assert "Disk encryption" in {n for n, _, _ in privacy_report.advice(c)}


def test_doctor_warns_about_cloud_models_in_normal_and_a_plain_http_server_on_the_network(mode):
    mode("normal")
    c = cfg(providers={"desktop": {"base_url": "http://192.168.1.50:11434", "kind": "ollama"}},
            models={"fast": "desktop:m"})
    got = {n: (m, d) for n, m, d in privacy_report.advice(c)}
    assert got["Cloud models"][0] == "warn" and "claude" in got["Cloud models"][1]
    assert got["Model on your network"][0] == "warn" and "192.168.1.50:11434" in got["Model on your network"][1]


def test_doctor_search_asks_shows_the_query_and_reports_the_first_line(mode, monkeypatch, tmp_path):
    import micropaw.config as config_mod
    from micropaw.tools import web_search
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps(
        {"privacy": {"mode": "local"}, "search": {"provider": "searxng", "searxng_url": "http://127.0.0.1:1"}}))
    mode("local")
    monkeypatch.setattr("micropaw.service.running_gateway", lambda: None)

    async def fake_searx(*a, **k):
        return "Search results for 'x' (via your SearXNG at 127.0.0.1:1):\n\n- a"

    monkeypatch.setattr(web_search, "_search_searxng", fake_searx)
    r = CliRunner().invoke(main, ["doctor", "--search"], input="y\n")
    assert "Search via your SearXNG at 127.0.0.1:1 (direct): MicroPaw test search" in r.output
    assert "Search results for 'x' (via your SearXNG" in r.output
    declined = CliRunner().invoke(main, ["doctor", "--search"], input="n\n")
    assert "Search results for" not in declined.output
