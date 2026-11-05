"""Setup wizard model step: plans, config writes, the pull with progress, missing Ollama."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from micropaw import setup_wizard as wiz
from micropaw.config import Config
from micropaw.providers.registry import LocalModel, LocalServer

GB = 10**9
GIB = 2**30


def server(name="ollama", **models) -> LocalServer:
    return LocalServer(name, f"http://127.0.0.1:1/{name}", [LocalModel(n, int(g * GB)) for n, g in models.items()])


def test_cloud_only_uses_one_lab_for_main_and_fast():
    plan = wiz.plan_models("claude", [], 16 * GIB, hybrid=False)
    assert plan.main == "claude:claude-sonnet-5-5" and plan.fast == "claude:claude-haiku-5-5" and not plan.fallbacks


def test_hybrid_puts_a_small_local_model_on_quick_jobs_and_a_big_one_as_backup():
    sv = [server(a=3, b=9)]
    plan = wiz.plan_models("claude", sv, 16 * GIB, hybrid=True)
    assert plan.main == "claude:claude-sonnet-5-5"
    assert plan.fast == "ollama:a" and plan.fallbacks == ["ollama:b"]


def test_hybrid_asked_but_nothing_local_falls_back_to_cloud_only():
    plan = wiz.plan_models("openai", [], 16 * GIB, hybrid=True)
    assert plan.fast == "openai:gpt-5.4-mini" and not plan.fallbacks


def test_local_only_main_is_largest_that_fits_and_fast_is_main():
    plan = wiz.plan_models("ollama", [server(a=3, b=9, c=14)], 16 * GIB, hybrid=False)
    assert plan.main == "ollama:b" and plan.fast == ""


def test_local_only_with_nothing_fitting_gives_an_empty_plan():
    assert wiz.plan_models("ollama", [server(c=14)], 8 * GIB, hybrid=False).main == ""


def test_apply_plan_writes_legacy_fields_for_builtin_providers():
    c = Config()
    wiz.apply_plan(c, wiz.plan_models("claude", [server(a=3, b=9)], 16 * GIB, hybrid=True))
    assert c.provider == "claude" and c.provider_config.claude.model == "claude-sonnet-5-5"
    assert c.models.fast == "ollama:a" and c.fallback_providers == ["ollama:b"]


def test_apply_plan_uses_agents_main_for_other_local_servers():
    c = Config()
    wiz.apply_plan(c, wiz.ModelPlan(main="lmstudio:qwen"))
    assert c.agents["main"].model == "lmstudio:qwen"
    assert Config(**json.loads(c.model_dump_json())).agents["main"].model == "lmstudio:qwen"  # round-trips


class Answer:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


class FakeQuestionary:
    def __init__(self, yes=True):
        self.yes = yes
        self.asked: list[str] = []

    def confirm(self, text, default=True):
        self.asked.append(text)
        return Answer(self.yes)


def patch_env(monkeypatch, servers, ram=16 * GIB, which=None):
    async def fake_scan(targets=None):
        return servers

    monkeypatch.setattr(wiz, "scan_local", fake_scan)
    monkeypatch.setattr("micropaw.providers.ollama.ram_bytes", lambda: ram)
    monkeypatch.setattr(wiz.shutil, "which", lambda name: which)


def test_choose_models_hybrid_flow(monkeypatch):
    patch_env(monkeypatch, [server(a=3, b=9)])
    c = Config()
    q = FakeQuestionary()
    wiz._choose_models(q, c, "claude")
    assert any("quick jobs" in t for t in q.asked)
    assert c.models.fast == "ollama:a" and c.fallback_providers == ["ollama:b"]


def test_choose_models_declined_changes_nothing(monkeypatch):
    patch_env(monkeypatch, [server(a=3)])
    c = Config()
    wiz._choose_models(FakeQuestionary(yes=False), c, "ollama")
    assert c.models.fast == "" and c.provider_config.ollama.model == "llama3.1"


def test_ollama_missing_prints_the_download_link(monkeypatch, capsys):
    patch_env(monkeypatch, [], which=None)
    wiz._choose_models(FakeQuestionary(), Config(), "ollama")
    assert wiz.OLLAMA_DOWNLOAD in capsys.readouterr().out


def test_ollama_installed_but_not_running(monkeypatch, capsys):
    patch_env(monkeypatch, [], which="/usr/bin/ollama")
    wiz._choose_models(FakeQuestionary(), Config(), "ollama")
    assert "not running" in capsys.readouterr().out


def test_nothing_pulled_offers_a_ram_sized_download(monkeypatch):
    patch_env(monkeypatch, [LocalServer("ollama", "http://x", [])], ram=16 * GIB)
    pulled = []
    monkeypatch.setattr(wiz, "_pull", lambda host, tag: pulled.append(tag) or True)
    c = Config()
    q = FakeQuestionary()
    wiz._choose_models(q, c, "ollama")
    assert pulled == ["qwen3.5:9b"] and c.provider_config.ollama.model == "qwen3.5:9b"
    assert any("Download qwen3.5:9b" in t for t in q.asked)


def test_declining_the_download_leaves_config_alone(monkeypatch):
    patch_env(monkeypatch, [LocalServer("ollama", "http://x", [])])
    monkeypatch.setattr(wiz, "_pull", lambda host, tag: pytest.fail("must not pull"))
    c = Config()
    wiz._choose_models(FakeQuestionary(yes=False), c, "ollama")
    assert c.provider_config.ollama.model == "llama3.1"


# --- /api/pull with progress ---

class PullHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()
        for obj in ({"status": "pulling manifest"},
                    {"status": "pulling abc", "total": 100, "completed": 40},
                    {"status": "pulling abc", "total": 100, "completed": 100},
                    {"status": "success"}):
            self.wfile.write(json.dumps(obj).encode() + b"\n")


@pytest.fixture
def pull_server():
    srv = HTTPServer(("127.0.0.1", 0), PullHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


async def test_pull_model_streams_progress(pull_server):
    from micropaw.providers.ollama import pull_model
    chunks = [c async for c in pull_model(pull_server, "m")]
    assert chunks[-1]["status"] == "success" and chunks[1]["completed"] == 40


def test_wizard_pull_returns_true_on_success(pull_server):
    assert wiz._pull(pull_server, "m") is True


def test_wizard_pull_returns_false_when_server_is_down(capsys):
    assert wiz._pull("http://127.0.0.1:1", "m") is False
    assert "Download failed" in capsys.readouterr().out


def test_apply_plan_keeps_the_users_fast_and_vision_models_when_the_plan_has_none():
    c = Config()
    c.models.fast, c.models.vision = "ollama:small", "ollama:eyes"
    wiz.apply_plan(c, wiz.ModelPlan(main="ollama:big"))
    assert (c.models.fast, c.models.vision) == ("ollama:small", "ollama:eyes")
    wiz.apply_plan(c, wiz.ModelPlan(main="ollama:big", fast="ollama:tiny"))
    assert (c.models.fast, c.models.vision) == ("ollama:tiny", "ollama:eyes")
