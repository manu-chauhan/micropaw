"""paw models list | scan | test."""

from __future__ import annotations

import httpx
from click.testing import CliRunner

import micropaw.config as config_mod
from micropaw.cli import main
from micropaw.providers.base import AIProvider, AIResponse, ToolCall
from micropaw.providers.registry import LocalModel, LocalServer, Registry


def run(*args):
    return CliRunner().invoke(main, list(args))


def test_models_list_shows_agents_and_presets(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "none.json")
    monkeypatch.setenv("MICROPAW_PROVIDER", "ollama")
    r = run("models", "list")
    assert r.exit_code == 0, r.output
    assert "local only" in r.output and "lmstudio" in r.output and "deepseek" in r.output


def test_models_scan_prints_servers_and_a_suggestion(monkeypatch):
    async def fake(targets=None):
        return [LocalServer("ollama", "http://127.0.0.1:11434", [LocalModel("qwen", 5_000_000_000)])]

    monkeypatch.setattr("micropaw.providers.registry.scan_local", fake)
    r = run("models", "scan")
    assert "ollama:qwen" in r.output and "Suggested main model" in r.output


def test_models_scan_with_nothing_found_points_to_ollama(monkeypatch):
    async def fake(targets=None):
        return []

    monkeypatch.setattr("micropaw.providers.registry.scan_local", fake)
    assert "https://ollama.com/download" in run("models", "scan").output


class Tooling(AIProvider):
    name, model = "fake", "m"

    async def chat(self, messages, tools=None):
        calls = [ToolCall("1", "ping", {"n": 1})] if tools else []
        return AIResponse(content="pong", tool_calls=calls, usage={})


def test_models_test_reports_chat_and_tool_support(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "none.json")
    monkeypatch.setattr(Registry, "get", lambda self, spec: Tooling())
    r = run("models", "test", "claude:x")
    assert r.exit_code == 0 and "ok" in r.output and "not tested" in r.output


def test_models_test_gives_a_plain_message_when_the_server_is_down(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "none.json")

    class Down(Tooling):
        async def chat(self, messages, tools=None):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(Registry, "get", lambda self, spec: Down())
    assert "Cannot connect" in run("models", "test", "ollama:x").output.replace("\n", " ")
