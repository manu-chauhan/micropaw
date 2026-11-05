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

pytest_plugins = ["tests.lan_fixtures"]


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


# --- privacy first, and a model server on another computer ---

class Choice:
    def __init__(self, title, value=None, **kw):
        self.title, self.value = title, title if value is None else value


class Scripted:
    """A stand-in for questionary: answers by a word in the question, else the default (or the first choice)."""

    Choice = Choice

    def __init__(self, rules=None):
        self.rules = rules or {}
        self.asked: list[tuple[str, object]] = []  # (question, default offered)

    def _answer(self, kind, message, default=None, choices=None):
        self.asked.append((message, default))
        for word, value in self.rules.items():
            if word in message:
                return Answer(value)
        if kind == "select":
            return Answer(default if default is not None else choices[0].value)
        if kind == "checkbox":
            return Answer([])
        return Answer(default)

    def select(self, message, choices=None, default=None):
        return self._answer("select", message, default, choices)

    def confirm(self, message, default=True):
        return self._answer("confirm", message, default)

    def text(self, message, default=""):
        return self._answer("text", message, default)

    def password(self, message):
        return self._answer("password", message, "")

    def checkbox(self, message, choices=None):
        return self._answer("checkbox", message, None, choices)

    def default_for(self, word):
        return next(d for m, d in self.asked if word in m)


def test_only_a_cloud_key_gives_normal_mode_search_off_and_retention_90(monkeypatch):
    patch_env(monkeypatch, [])
    c = Config()
    q = Scripted()
    assert wiz._choose_privacy(q, c) == "normal" and c.privacy.mode == "normal"
    assert q.default_for("How private") == "normal"
    wiz._ask_search(q, c)
    assert c.search.provider == "off" and q.default_for("Web search") == "off"
    from types import SimpleNamespace
    holder = SimpleNamespace(storage=SimpleNamespace(retention_days=0))
    wiz._ask_retention(q, holder)
    assert holder.storage.retention_days == 90 and q.default_for("keep old messages") == 90
    wiz._ask_retention(q, Config())  # a version without the storage setting asks nothing


def test_a_model_server_on_this_computer_preselects_local(monkeypatch):
    patch_env(monkeypatch, [server(a=3)])
    q = Scripted()
    c = Config()
    assert wiz._choose_privacy(q, c) == "local"
    assert q.default_for("How private") == "local"


def test_airgapped_asks_for_the_network_kiwix_and_forces_offline_search(monkeypatch):
    patch_env(monkeypatch, [])
    q = Scripted({"How private": "airgapped", "other computers": True, "addresses on your network": "gpu-box, nas",
                  "Kiwix server": "localhost:8080", "Kiwix book": "wikipedia_en_all_nopic_2026-06, devdocs_en_python"})
    c = Config()
    assert wiz._choose_privacy(q, c) == "airgapped"
    assert c.privacy.allow_lan is True and c.privacy.lan_hosts == ["gpu-box", "nas"]
    assert c.search.kiwix_url == "http://localhost:8080" and len(c.search.kiwix_books) == 2
    assert c.search.provider == "offline" and not any("SearXNG" in m for m, _ in q.asked)


def test_local_mode_asks_for_your_own_searxng_and_a_proxy(monkeypatch):
    patch_env(monkeypatch, [])
    q = Scripted({"How private": "local", "Your own SearXNG": "search.home.lan:8888",
                  "SOCKS proxy": "socks5h://127.0.0.1:9050"})
    c = Config()
    wiz._choose_privacy(q, c)
    assert c.search.provider == "searxng" and c.search.searxng_url == "http://search.home.lan:8888"
    assert c.search.proxy == "socks5h://127.0.0.1:9050"


def test_a_cloud_fallback_for_a_local_main_is_asked_and_sets_the_flag():
    c = Config()
    assert wiz._ask_cloud_fallback(Scripted({"messages would go": True}), c, "ollama", ["claude", "ollama"]) \
        == ["claude", "ollama"] and c.privacy.allow_cloud_fallback is True
    c = Config()
    assert wiz._ask_cloud_fallback(Scripted({"messages would go": False}), c, "ollama", ["claude"]) == []
    assert c.privacy.allow_cloud_fallback is False
    c = Config()  # a cloud main is not asked
    assert wiz._ask_cloud_fallback(Scripted(), c, "claude", ["openai"]) == ["openai"]


async def _noop():
    pass


def test_another_computer_writes_the_provider_and_the_main_model(monkeypatch, model_server):
    box = model_server(models=("qwen3.6:27b",))
    monkeypatch.setattr(wiz, "_pull", lambda *a: pytest.fail("a remote host is never pulled onto"))
    monkeypatch.setattr("micropaw.providers.ollama.ram_bytes", lambda: 4 * GIB)  # this computer is small
    q = Scripted({"Address of the other computer": f"127.0.0.1:{box.port}"})
    c = Config()
    wiz._choose_models(q, c, "ollama", "network")
    assert list(c.providers) == ["lan-127-0-0-1"] and c.providers["lan-127-0-0-1"].base_url.startswith(box.url)
    assert c.agents["main"].model == "lan-127-0-0-1:qwen3.6:27b"  # sized for that computer, not this one
    assert not any("Download" in m for m, _ in q.asked)


def test_a_cloud_user_adds_a_remote_fast_model_and_keeps_the_provider(monkeypatch, model_server):
    box = model_server(models=("small",))
    q = Scripted({"Address of the other computer": f"127.0.0.1:{box.port}", "Use it for": "fast"})
    c = Config()
    c.provider = "claude"
    wiz._choose_models(q, c, "claude", "network")
    assert c.models.fast == "lan-127-0-0-1:small" and c.provider == "claude" and "main" not in c.agents
    q = Scripted({"Address of the other computer": f"127.0.0.1:{box.port}", "Use it for": "fallback"})
    c = Config()
    wiz._choose_models(q, c, "claude", "network")
    assert c.fallback_providers == ["lan-127-0-0-1:small"]


def test_another_computer_that_needs_a_key_or_does_not_answer_saves_nothing(monkeypatch, model_server, capsys):
    locked = model_server(key="secret")
    c = Config()
    wiz._add_remote(Scripted({"Address of the other computer": f"127.0.0.1:{locked.port}"}), c, "ollama")
    assert "needs a key" in capsys.readouterr().out and not c.providers
    wiz._add_remote(Scripted({"Address of the other computer": "127.0.0.1:1"}), c, "ollama")
    assert "Nothing answered" in capsys.readouterr().out and not c.providers
    wiz._add_remote(Scripted({"Address of the other computer": "a b"}), c, "ollama")
    assert "not an address" in capsys.readouterr().out


def test_the_network_scan_is_a_separate_question_that_defaults_to_no_and_says_what_it_sends(monkeypatch):
    import micropaw.modelscan as ms
    monkeypatch.setattr(ms, "own_ip", lambda: "192.168.1.37")
    q = Scripted()
    called = []

    async def fake_scan(hosts, **k):
        called.append(hosts)
        return []

    monkeypatch.setattr(ms, "scan_lan", fake_scan)
    wiz._offer_lan_scan(q, Config(), "ollama")
    message, default = q.asked[0]
    assert default is False and not called
    assert "254 addresses on 192.168.1.0/24" in message and "Only scan a network you own" in message
    wiz._offer_lan_scan(Scripted({"Look for model servers": True}), Config(), "ollama")
    assert len(called[0]) == 254


def test_run_setup_ends_to_end_for_a_claude_user_with_a_fallback_ollama_host(monkeypatch):
    import sys
    from types import SimpleNamespace
    patch_env(monkeypatch, [])
    saved = []
    c = Config(provider_config={"ollama": {"host": "http://192.168.1.50:11434"}})
    monkeypatch.setattr(wiz, "config_exists", lambda: True)
    monkeypatch.setattr(wiz, "load_config", lambda: c)
    monkeypatch.setattr(wiz, "save_config", lambda cfg: saved.append(cfg))
    q = Scripted({"Which AI provider": "claude", "Enter your Anthropic": "sk-x", "Set up fallback": True})
    q.checkbox = lambda message, choices=None: Answer(["ollama"])
    monkeypatch.setitem(sys.modules, "questionary", SimpleNamespace(
        select=q.select, confirm=q.confirm, text=q.text, password=q.password, checkbox=q.checkbox, Choice=Choice))
    wiz.run_setup()
    assert saved and saved[0].privacy.mode == "normal" and saved[0].search.provider == "off"
    assert q.default_for("Ollama host for fallback") == "http://192.168.1.50:11434"
    assert "sent to the model with every message" in next(m for m, _ in q.asked if "location" in m)
