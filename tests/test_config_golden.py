"""The dataclass config reads every config the pydantic one (kept in tests/legacy_pydantic_config.py) did."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")

from micropaw import setup_wizard as wiz  # noqa: E402
from micropaw.config import Config, ConfigValueError, _apply_env_overrides, _migrate, changed_fields  # noqa: E402
from micropaw.utils.redact import config_secrets  # noqa: E402
from tests import legacy_pydantic_config as old  # noqa: E402
from tests.test_wizard_models import Choice, Scripted, patch_env  # noqa: E402

FULL = {
    "provider": "ollama",
    "provider_config": {
        "claude": {"api_key": "sk-ant-aaaaaaaaaaaa", "effort": "high", "max_tokens": "8000"},
        "openai": {"api_key": "sk-openai-bbbbbbbb", "base_url": "https://example.test"},
        "ollama": {"host": "gpu-box.local", "model": "qwen3", "num_ctx": 4096.0, "keep_alive": "5m"},
        "gemini": {"api_key": "gem-cccccccc"},
    },
    "channels": {
        "terminal": {"enabled": "false"},
        "telegram": {"enabled": True, "token": "123:telegram-secret", "allowed_users": ["1", "2"]},
        "discord": {"token": "discord-secret", "allowed_users": ["3"]},
        "slack": {"bot_token": "xoxb-1", "app_token": "xapp-1", "signing_secret": "sign-1"},
        "whatsapp": {"token": "wa-1", "phone_id": "9", "verify_token": "v-1", "app_secret": "s-1"},
        "webchat": {"enabled": 1, "port": "9000", "auth_token": "web-secret", "allowed_origins": ["http://x"]},
    },
    "permissions": {"allow_shell": "yes", "sandbox": "bwrap", "disabled_tools": ["web_search"]},
    "persona": {"name": "Rex", "location": "Oslo"},
    "rate_limit": {"messages_per_minute": 5},
    "embedding": {"enabled": False, "provider": "ollama", "api_key_env": "EMB_KEY"},
    "search": {"provider": "searxng", "brave_api_key": "brave-secret", "kiwix_books": ["a", "b"]},
    "context": {"window_tokens": "32000"},
    "scheduler": {"heartbeat_interval": 60},
    "google": {"enabled": True, "client_id": "gid-1", "client_secret": "gsecret-1"},
    "models": {"fast": "ollama:gemma4:e4b", "vision": "claude:claude-haiku-5-5"},
    "voice": {"stt": "openai:whisper-1", "reply_with_voice": "on"},
    "privacy": {"mode": " Air-Gapped ", "allow": ["telegram"], "lan_hosts": ["10.1.0.0/16"]},
    "storage": {"retention_days": 30, "audit": "names"},
    "mcp_servers": {
        "files": {"command": "npx", "args": ["-y", "pkg@1.2.3"], "env": {"TOKEN": "mcp-secret"}, "timeout": 60},
        "remote": {"url": "https://mcp.example/x", "headers": {"Authorization": "${MCP_TOKEN}"}, "timeout": "30"},
    },
    "fallback_providers": ["ollama:llama3.1"],
    "budget": {"daily_usd": 5, "warn_at": "0.5"},
    "agents": {
        "work": {"model": "claude:claude-sonnet-5-5", "fast": "", "fallbacks": [], "history": "separate",
                 "persona": {"name": "Work"}, "tools": {"allow": ["mcp_*"]}, "allow_cloud": True},
    },
    "routes": {"telegram:-100123": "work", "discord:*": "main"},
    "providers": {"myvllm": {"base_url": "gpu-box:8000", "api_key_env": "VLLM_KEY", "kind": "ollama"}},
    "toolset": "lean",
    "display": {"show_provider": "false"},
    "some_future_key": {"anything": 1},
    "config_version": "1.0",
}

LEGACY_V0 = {"email": {"enabled": True}, "calendar": {"enabled": False, "client_id": "x"}, "provider": "openai"}

CORPUS = {
    "empty": {},
    "full": FULL,
    "legacy v0 file": _migrate(json.loads(json.dumps(LEGACY_V0))),
    "unknown keys in sections": {"search": {"provider": "off", "removed_setting": 1}, "removed_section": {}},
}


def _load_old(raw):
    return old.Config(**raw)


def _load_new(raw):
    return Config.from_dict(raw)


@pytest.mark.parametrize("name", list(CORPUS))
def test_same_config_from_both_loaders(name):
    raw = CORPUS[name]
    assert asdict(_load_new(raw)) == _load_old(raw).model_dump()


def test_defaults_match():
    assert asdict(Config()) == old.Config().model_dump()


def test_env_overrides_give_the_same_config(monkeypatch):
    for var, value in {"ANTHROPIC_API_KEY": "sk-env", "OLLAMA_HOST": "0.0.0.0:11434", "MICROPAW_PRIVACY": "local",
                       "TELEGRAM_BOT_TOKEN": "tg-env", "MICROPAW_FAST_MODEL": "ollama:a"}.items():
        monkeypatch.setenv(var, value)
    raw = _apply_env_overrides(json.loads(json.dumps(FULL)))
    assert asdict(_load_new(raw)) == _load_old(raw).model_dump()
    assert _load_new(raw).provider_config.ollama.host == "http://127.0.0.1:11434"


def _wizard_output(monkeypatch, rules):
    patch_env(monkeypatch, [])
    saved = []
    monkeypatch.setattr(wiz, "config_exists", lambda: False)
    monkeypatch.setattr(wiz, "save_config", lambda cfg: saved.append(cfg))
    q = Scripted(rules)
    monkeypatch.setitem(sys.modules, "questionary", SimpleNamespace(
        select=q.select, confirm=q.confirm, text=q.text, password=q.password, checkbox=q.checkbox, Choice=Choice))
    wiz.run_setup()
    return json.loads(json.dumps(asdict(saved[0])))


@pytest.mark.parametrize("rules", [{"How private": "local"}, {"How private": "normal"}, {"How private": "airgapped"}])
def test_wizard_output_loads_the_same(monkeypatch, rules):
    raw = _wizard_output(monkeypatch, rules)
    assert asdict(_load_new(raw)) == _load_old(raw).model_dump() == raw


# Bad input: both loaders refuse it and point at the same place.
BAD = {
    "text where a number goes": {"context": {"max_tool_iterations": "many"}},
    "a number where text goes": {"persona": {"name": 5}},
    "fraction for an int": {"scheduler": {"max_jobs": 2.5}},
    "bad boolean": {"google": {"enabled": "maybe"}},
    "bad literal": {"permissions": {"sandbox": "chroot"}},
    "bad privacy mode": {"privacy": {"mode": "private"}},
    "list expected": {"search": {"kiwix_books": "a"}},
    "number in a string list": {"channels": {"telegram": {"allowed_users": [123]}}},
    "dict expected": {"routes": ["a"]},
    "section is not a dict": {"budget": 3},
    "bad ollama host": {"provider_config": {"ollama": {"host": "foo bar"}}},
    "bad provider base_url": {"providers": {"x": {"base_url": "a b"}}},
    "provider without base_url": {"providers": {"x": {}}},
    "mcp server with nothing to run": {"mcp_servers": {"x": {}}},
    "mcp env value not a string": {"mcp_servers": {"x": {"command": "c", "env": {"A": 1}}}},
    "unknown agent key": {"agents": {"a": {"users": ["1"]}}},
    "bad agent name": {"agents": {"Bad Name": {}}},
    "unknown provider in an agent": {"agents": {"a": {"model": "nope:model"}}},
    "route to a missing agent": {"routes": {"telegram:1": "ghost"}},
    "route without a colon": {"routes": {"telegram": "main"}},
    "bad toolset": {"toolset": "some"},
    "none for a plain field": {"persona": {"name": None}},
}


@pytest.mark.parametrize("name", list(BAD))
def test_bad_config_is_refused_at_the_same_place(name):
    raw = BAD[name]
    with pytest.raises(Exception) as was:
        _load_old(raw)
    with pytest.raises(ConfigValueError) as now:
        _load_new(raw)
    assert [str(p) for p in was.value.errors()[0]["loc"]] == [str(p) for p in now.value.loc]


def test_an_agent_with_an_unknown_key_says_extra_inputs():
    with pytest.raises(ConfigValueError, match=r"agents\.a\.users: Extra inputs are not permitted"):
        Config.from_dict({"agents": {"a": {"users": ["1"]}}})


def test_an_unknown_key_is_reported_after_a_bad_field():
    raw = {"agents": {"a": {"fast": {"x": "y"}, "model_x": {}}}}
    with pytest.raises(Exception) as was:
        _load_old(raw)
    with pytest.raises(ConfigValueError) as now:
        _load_new(raw)
    assert [str(p) for p in was.value.errors()[0]["loc"]] == [str(p) for p in now.value.loc] == ["agents", "a", "fast"]


@pytest.mark.parametrize("raw", [
    {"storage": {"retention_days": "abc"}}, {"storage": {"retention_days": 1.5}}, {"google": {"enabled": "maybe"}},
    {"budget": {"warn_at": "x"}}, {"permissions": {"sandbox": "nope"}}, {"channels": 5}, {"budget": {"warn_at": []}},
])
def test_error_wording_keeps_the_pydantic_reason(raw):
    with pytest.raises(Exception) as was:
        _load_old(raw)
    with pytest.raises(ConfigValueError) as now:
        _load_new(raw)
    assert now.value.why == was.value.errors()[0]["msg"]


# Scalar rules: for each field type, many inputs, same answer or both refuse.
SCALARS = [0, 1, 2, -1, 5, 5.0, 5.5, 0.0, 1.0, True, False, "0", "1", "5", " 5 ", "+5", "-5", "5.0", "5.5", "1e3",
           "1_000", "", "abc", "true", "True", "TRUE", "yes", "no", "off", "on", "y", "n", "t", "f", "false", "2",
           None, [], [1], {}, {"a": 1}, "nan", "inf", 10**20,
           "\u0663", "\uff11\uff12", "1.", "5.", " true ", "1_000.0", "\u0663.\u0660", "+5.00", "1__0"]


def _same(section, key, value, make_old, make_new):
    raw = {section: {key: value}}
    try:
        expected = make_old(raw).model_dump()[section][key]
    except Exception:
        with pytest.raises(ConfigValueError):
            make_new(raw)
        return
    got = asdict(make_new(raw))[section][key]
    assert repr(got) == repr(expected), (value, got, expected)


@pytest.mark.parametrize("section,key", [
    ("context", "max_tool_iterations"),   # int
    ("budget", "warn_at"),                # float
    ("google", "enabled"),                # bool
    ("persona", "name"),                  # str
    ("permissions", "sandbox"),           # Literal
    ("embedding", "enabled"),             # str with a before-validator
    ("search", "kiwix_books"),            # list[str]
])
def test_scalar_coercion_matches_pydantic(section, key):
    for value in SCALARS:
        _same(section, key, value, _load_old, _load_new)


def test_optional_fields_match_pydantic():
    cases = {"fast": [None, "", "ollama:a", 3], "fallbacks": [None, [], ["ollama:a"], "ollama:a", [3]]}
    for key, values in cases.items():
        for value in values:
            raw = {"agents": {"a": {key: value}}}
            try:
                expected = old.Config(**raw).model_dump()["agents"]["a"][key]
            except Exception:
                with pytest.raises(ConfigValueError):
                    Config.from_dict(raw)
                continue
            assert asdict(Config.from_dict(raw))["agents"]["a"][key] == expected


# The secrets redact collects must not shrink.
def test_redact_sees_the_same_secrets():
    new_secrets = config_secrets(_load_new(FULL))
    assert new_secrets == old.config_secrets(_load_old(FULL))
    expected = ("sk-ant-aaaaaaaaaaaa", "sk-openai-bbbbbbbb", "gem-cccccccc", "123:telegram-secret", "discord-secret",
                "xoxb-1", "xapp-1", "sign-1", "wa-1", "s-1", "web-secret", "brave-secret", "gsecret-1", "mcp-secret")
    assert set(expected) <= set(new_secrets.values())


def test_changed_fields_lists_only_what_differs_from_the_default():
    cfg = Config.from_dict({"mcp_servers": {"x": {"command": "npx", "args": ["a"], "timeout": 300}}}).mcp_servers["x"]
    assert changed_fields(cfg) == {"command": "npx", "args": ["a"]}


# paw --version must not pay for the config
def test_config_loads_no_pydantic():
    code = "import sys, micropaw.config; print('pydantic' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "MICROPAW_HOME": os.devnull + "x"})
    assert out.stdout.strip() == "False"
