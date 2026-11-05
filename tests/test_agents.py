"""Agents: profiles and inheritance, the private-agent rule, routing, tool views, `paw agent`, config rules."""

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

import micropaw.config as config_mod
from micropaw.config import Config
from micropaw.core.agent import Agent
from micropaw.core.profile import ProfileError, ToolView, build_runtimes, build_view, resolve_profiles
from micropaw.gateway import Gateway
from micropaw.memory.store import MemoryStore
from micropaw.providers.registry import Registry
from micropaw.tools.base import Tool, ToolRegistry


def cfg(**kw) -> Config:
    c = Config(**kw)
    c.provider_config.claude.api_key = "sk-test"
    return c


def profiles(c: Config):
    return resolve_profiles(c, Registry(c))


# --- back-compat: legacy fields give one agent "main" ---

def test_legacy_config_makes_one_main_agent():
    c = cfg(fallback_providers=["ollama"], models={"fast": "claude:claude-haiku-5-5", "vision": "ollama:llava"})
    p = profiles(c)
    assert list(p) == ["main"]
    m = p["main"]
    assert (m.main, m.fast, m.vision, m.fallbacks) == ("claude", "claude:claude-haiku-5-5", "ollama:llava", ["ollama"])
    assert not m.separate and m.prefix == ""


def test_legacy_runtime_builds_the_same_chain_as_before(monkeypatch):
    c = cfg(fallback_providers=["ollama"])
    rt = build_runtimes(c, Registry(c))["main"]
    assert rt.provider.name == "failover(claude,ollama)"
    assert rt.fast is None and rt.vision is None


def test_main_without_a_key_is_a_clear_error():
    c = Config()
    with pytest.raises(ProfileError, match="No usable model for agent 'main'"):
        build_runtimes(c, Registry(c))


# --- private agents ---

LOCAL_AGENT = {"model": "ollama:qwen3.5:9b"}


def test_local_agent_does_not_inherit_cloud_fallbacks_or_roles():
    c = cfg(fallback_providers=["claude"], models={"fast": "claude:claude-haiku-5-5"}, agents={"family": LOCAL_AGENT})
    fam = profiles(c)["family"]
    assert fam.fallbacks == [] and fam.fast == ""
    assert fam.private and fam.separate and fam.prefix == "agent:family:"
    assert set(fam.dropped) == {"claude", "claude:claude-haiku-5-5"}


def test_local_agent_keeps_inherited_local_entries():
    c = cfg(fallback_providers=["ollama:small"], agents={"family": LOCAL_AGENT})
    assert profiles(c)["family"].fallbacks == ["ollama:small"]


def test_local_agent_may_list_cloud_only_with_allow_cloud():
    bad = cfg(agents={"family": {**LOCAL_AGENT, "fallbacks": ["claude"]}})
    with pytest.raises(ProfileError, match="allow_cloud"):
        profiles(bad)
    ok = cfg(agents={"family": {**LOCAL_AGENT, "fallbacks": ["claude"], "allow_cloud": True}})
    fam = profiles(ok)["family"]
    assert fam.fallbacks == ["claude"] and not fam.private


def test_cloud_agent_inherits_everything_and_shares_history():
    c = cfg(fallback_providers=["ollama"], agents={"coder": {"model": "claude:claude-haiku-5-5"}})
    coder = profiles(c)["coder"]
    assert coder.fallbacks == ["ollama"] and not coder.private and not coder.separate


def test_cloud_agent_gets_own_history_when_main_is_local_only():
    c = cfg(provider="ollama", agents={"coder": {"model": "claude:claude-haiku-5-5"}})
    p = profiles(c)
    assert p["main"].private and p["main"].prefix == ""  # main keeps today's conversation keys
    assert p["coder"].separate


def test_local_detection_follows_the_host(monkeypatch):
    c = cfg(agents={"far": {"model": "ollama:m"}})
    c.provider_config.ollama.host = "http://8.8.8.8:11434"
    assert not profiles(c)["far"].private
    c.provider_config.ollama.host = "http://192.168.1.5:11434"
    assert profiles(c)["far"].private


def test_private_agent_has_no_cloud_speech_to_text():
    c = cfg(agents={"family": LOCAL_AGENT}, voice={"stt": "openai:whisper-1"})
    c.provider_config.openai.api_key = "k"
    rt = build_runtimes(c, Registry(c))
    assert rt["family"].config.voice.stt == ""
    assert rt["main"].config.voice.stt == "openai:whisper-1"
    c2 = cfg(agents={"family": LOCAL_AGENT}, voice={"stt": "local:whisper"})
    assert build_runtimes(c2, Registry(c2))["family"].config.voice.stt == "local:whisper"


def test_persona_and_workspace_apply_per_agent():
    c = cfg(agents={"coder": {"model": "claude", "persona": {"personality": "terse", "name": "Coda"},
                              "workspace": "/tmp/code"}})
    rt = build_runtimes(c, Registry(c))
    assert rt["coder"].config.persona.name == "Coda" and rt["coder"].config.permissions.workspace == "/tmp/code"
    assert rt["main"].config.persona.name == "MicroPaw"


def test_agent_conversations_are_prefixed_for_separate_agents():
    c = cfg(agents={"family": LOCAL_AGENT})
    rt = build_runtimes(c, Registry(c))["family"]
    agent = Agent(rt.config, rt.provider, ToolRegistry(), MagicMock(), rt.profile)
    assert agent.conversations.get_conversation_id("telegram", "1") == "agent:family:dm:owner"
    assert agent.conversations.get_conversation_id("telegram", "1", "-100") == "agent:family:telegram:-100"
    assert agent.name == "family"
    main = Agent(Config(), MagicMock(), ToolRegistry(), MagicMock())
    assert main.conversations.get_conversation_id("telegram", "1") == "dm:owner"


def test_own_personality_is_not_overridden_by_soul_md(tmp_path, monkeypatch):
    (tmp_path / "SOUL.md").write_text("You are a pirate.")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    c = cfg(agents={"coder": {"model": "claude", "persona": {"personality": "terse engineer"}}})
    rt = build_runtimes(c, Registry(c))
    coder = Agent(rt["coder"].config, rt["coder"].provider, ToolRegistry(), MagicMock(), rt["coder"].profile)
    main = Agent(rt["main"].config, rt["main"].provider, ToolRegistry(), MagicMock(), rt["main"].profile)
    prompt = coder._build_system_prompt("terminal")
    assert "terse engineer" in prompt and "pirate" not in prompt
    assert "pirate" in main._build_system_prompt("terminal")


# --- config rules ---

def test_agent_users_field_is_rejected():
    with pytest.raises(ValueError, match="users"):
        Config(agents={"family": {"model": "ollama:x", "users": {"telegram": ["1"]}}})


def test_bad_specs_names_and_routes_are_clear_errors():
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        Config(agents={"a": {"model": "nope:x"}})
    with pytest.raises(ValueError, match="Agent name"):
        Config(agents={"Bad Name": {"model": "claude"}})
    with pytest.raises(ValueError, match="not in agents"):
        Config(routes={"telegram:1": "ghost"})
    with pytest.raises(ValueError, match="platform:chat"):
        Config(agents={"a": {"model": "claude"}}, routes={"telegram": "a"})


# --- tool views ---

class T(Tool):
    description = "d"
    parameters: dict = {"type": "object", "properties": {}}

    def __init__(self, name):
        self.name = name

    async def execute(self, args, context):
        return "ok"


def base_registry(*names) -> ToolRegistry:
    r = ToolRegistry()
    for n in names:
        r.register(T(n))
    return r


def test_allow_list_keeps_only_those_tools():
    base = base_registry("shell", "file_read", "web_search", "mcp_ghidra_decompile")
    view = ToolView(base, ["shell", "file_read", "mcp_ghidra_*"], [])
    assert sorted(view.names) == ["file_read", "mcp_ghidra_decompile", "shell"]
    assert view.get("web_search") is None and view.get("shell") is not None
    assert [d.name for d in view.to_tool_defs()] == view.names


def test_deny_list_removes_after_allow():
    base = base_registry("shell", "file_read", "email")
    assert sorted(ToolView(base, [], ["shell", "email"]).names) == ["file_read"]
    assert sorted(ToolView(base, ["shell", "file_read"], ["shell"]).names) == ["file_read"]


def test_mcp_names_and_patterns():
    base = base_registry("shell", "mcp_blender_render", "mcp_ghidra_decompile")
    assert sorted(ToolView(base, [], ["mcp_*"]).names) == ["shell"]
    assert sorted(ToolView(base, [], ["mcp_ghidra_*"]).names) == ["mcp_blender_render", "shell"]
    assert sorted(ToolView(base, ["mcp_blender_render"], []).names) == ["mcp_blender_render"]


def test_view_sees_tools_registered_after_it_was_made():
    base = base_registry("shell")
    view = ToolView(base, [], ["mcp_ghidra_*"])
    base.register(T("mcp_ghidra_decompile"))  # an MCP server that came up later
    base.register(T("mcp_blender_render"))
    assert sorted(view.names) == ["mcp_blender_render", "shell"]


def test_unfiltered_main_uses_the_shared_registry_and_others_get_a_view():
    c = cfg(agents={"coder": {"model": "claude", "tools": {"deny": ["shell"]}}, "plain": {"model": "claude"}})
    rts = build_runtimes(c, Registry(c))
    base = base_registry("shell", "delegate", "vision")
    assert build_view(base, rts["main"]) is base
    assert isinstance(build_view(base, rts["coder"]), ToolView)
    assert isinstance(build_view(base, rts["plain"]), ToolView)  # named agents always get their own


def test_view_binds_delegate_and_vision_to_the_agents_models():
    from micropaw.tools.delegate import DelegateTool
    from micropaw.tools.vision import VisionTool
    c = cfg(agents={"coder": {"model": "claude", "fast": "claude:claude-haiku-5-5"}})
    rts = build_runtimes(c, Registry(c))
    base = ToolRegistry()
    base.register(DelegateTool())
    base.register(VisionTool())
    view = build_view(base, rts["coder"])
    assert view.get("delegate")._provider is rts["coder"].fast
    assert view.get("delegate")._registry is view  # sub-agents get the agent's tools only
    assert view.get("vision")._provider is rts["coder"].provider  # no vision role: its main model


# --- routing ---

def make_gateway(**cfg_kw) -> Gateway:
    c = cfg(agents={"coder": {"model": "claude"}, "family": LOCAL_AGENT}, **cfg_kw)
    gw = Gateway(c, MagicMock(), MagicMock(), [])
    gw.agents = {
        n: SimpleNamespace(name=n, profile=None, provider=SimpleNamespace(name=n)) for n in ("main", "coder", "family")
    }
    gw.agent = gw.agents["main"]
    return gw


def test_route_precedence_chat_choice_then_exact_then_wildcard_then_main():
    gw = make_gateway(routes={"telegram:-100": "coder", "telegram:*": "family", "discord:*": "coder"})
    assert gw._route("telegram", "1", "-100").name == "coder"       # exact chat
    assert gw._route("telegram", "1", "-200").name == "family"      # platform wildcard
    assert gw._route("slack", "1", "C1").name == "main"             # nothing matches
    gw._chat_agent["telegram:-100"] = "main"                        # /agent main beats the exact route
    assert gw._route("telegram", "1", "-100").name == "main"
    gw._chat_agent["telegram:-200"] = "coder"                       # and beats the wildcard
    assert gw._route("telegram", "1", "-200").name == "coder"


def test_dm_route_uses_the_user_id():
    gw = make_gateway(routes={"telegram:555": "family"})
    assert gw._route("telegram", "555", None).name == "family"
    assert gw._route("telegram", "556", None).name == "main"


def test_internal_runs_always_go_to_main():
    gw = make_gateway(routes={"system:*": "coder", "heartbeat:*": "coder"})
    assert gw._route("system", "cron", "cron-1").name == "main"
    assert gw._route("heartbeat", "heartbeat", "system").name == "main"


def test_one_agent_routes_to_agent():
    gw = Gateway(Config(), MagicMock(), MagicMock(), [])
    gw.agent = SimpleNamespace(name="main")
    assert gw._route("telegram", "1", None) is gw.agent


async def test_agent_commands_and_persistence(tmp_path):
    store = MemoryStore(str(tmp_path / "t.db"))
    await store.init()
    try:
        gw = make_gateway()
        gw.store = store
        await gw._load_chat_agents()
        assert "main" in await gw._owner_command("/agent", "telegram", "1", None)
        listing = await gw._owner_command("/agents", "telegram", "1", None)
        assert "coder" in listing and "family" in listing
        assert "No agent called 'ghost'" in await gw._owner_command("/agent ghost", "telegram", "1", None)
        assert "now talks to" in await gw._owner_command("/agent coder", "telegram", "1", None)
        assert gw._route("telegram", "1", None).name == "coder"
        # a new gateway reads the choice back from data.db
        gw2 = make_gateway()
        gw2.store = store
        await gw2._load_chat_agents()
        assert gw2._route("telegram", "1", None).name == "coder"
        who = await gw._owner_command("/whoami", "telegram", "1", "-100")
        assert "telegram:-100" in who and "Your user ID: 1" in who
    finally:
        await store.close()


# --- paw agent add / list / remove ---

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("micropaw.cli.CONFIG_FILE", tmp_path / "config.json")
    return tmp_path


def run(*args):
    from micropaw.cli import main
    return CliRunner().invoke(main, list(args))


def test_agent_add_writes_a_valid_config_with_0600(home):
    r = run("agent", "add", "coder", "--model", "ollama:qwen3.5:9b", "--persona", "terse",
            "--allow", "shell", "--allow", "file_read", "--route", "telegram:-100")
    assert r.exit_code == 0, r.output
    f = home / "config.json"
    raw = json.loads(f.read_text())
    assert raw["agents"]["coder"] == {
        "model": "ollama:qwen3.5:9b", "persona": {"personality": "terse"},
        "tools": {"allow": ["shell", "file_read"], "deny": []},
    }
    assert raw["routes"] == {"telegram:-100": "coder"}
    if os.name != "nt":
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    cfg_loaded = config_mod.load_config()
    assert cfg_loaded.agents["coder"].model == "ollama:qwen3.5:9b"


def test_agent_add_refuses_bad_input_and_writes_nothing(home):
    r = run("agent", "add", "x", "--model", "nope:m")
    assert r.exit_code != 0 and "unknown provider" in r.output
    r = run("agent", "add", "x", "--model", "ollama:m", "--deny", "shel")
    assert r.exit_code != 0 and "Unknown tool name" in r.output
    r = run("agent", "add", "x", "--model", "ollama:m", "--fallback", "claude")
    assert r.exit_code != 0 and "allow_cloud" in r.output
    assert not (home / "config.json").exists()


def test_agent_add_accepts_mcp_patterns_and_refuses_duplicates(home):
    assert run("agent", "add", "x", "--model", "ollama:m", "--deny", "mcp_*").exit_code == 0
    r = run("agent", "add", "x", "--model", "ollama:m")
    assert r.exit_code != 0 and "already exists" in r.output


def test_agent_remove_drops_its_routes(home):
    run("agent", "add", "x", "--model", "ollama:m", "--route", "discord:*")
    assert run("agent", "remove", "x").exit_code == 0
    raw = json.loads((home / "config.json").read_text())
    assert raw["agents"] == {} and raw["routes"] == {}
    assert run("agent", "remove", "x").exit_code != 0


def test_agent_add_keeps_env_keys_out_of_the_file(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    run("agent", "add", "x", "--model", "ollama:m")
    assert "sk-from-env" not in (home / "config.json").read_text()


# --- schema migration ---

def test_old_config_is_backed_up_then_migrated_0600(home):
    f = home / "config.json"
    f.write_text(json.dumps({"config_version": "1.0", "provider": "ollama",
                             "calendar": {"enabled": True, "client_id": "cid"}}))
    os.chmod(f, 0o644)
    c = config_mod.load_config()
    assert c.provider == "ollama" and c.google.enabled and c.google.client_id == "cid"
    new = json.loads(f.read_text())
    assert new["schema_version"] == config_mod.SCHEMA_VERSION and "calendar" not in new and "config_version" not in new
    backup = home / "config.json.v0.bak"
    assert json.loads(backup.read_text())["calendar"]["client_id"] == "cid"
    if os.name != "nt":
        assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_current_config_is_not_rewritten(home):
    f = home / "config.json"
    f.write_text(json.dumps({"schema_version": config_mod.SCHEMA_VERSION, "provider": "ollama"}))
    before = f.stat().st_mtime_ns
    config_mod.load_config()
    assert f.stat().st_mtime_ns == before and not list(home.glob("*.bak"))


def test_corrupt_config_is_never_overwritten_by_migration(home):
    f = home / "config.json"
    f.write_text("{not json")
    config_mod.load_config()
    assert f.read_text() == "{not json"


# --- the gateway with several agents, end to end ---

async def test_gateway_runs_named_agents_and_probes_ollama_in_the_background(tmp_path, monkeypatch):
    import asyncio

    import micropaw.gateway as gw_mod
    from micropaw.providers.base import AIProvider, AIResponse

    class Said(AIProvider):
        def __init__(self, name):
            self.name, self.model = name, "m"

        async def chat(self, messages, tools=None):
            return AIResponse(content=f"hi from {self.name}", tool_calls=[], usage={})

    monkeypatch.setattr(gw_mod, "DB_FILE", tmp_path / "d.db")
    monkeypatch.setattr(gw_mod, "CONFIG_DIR", tmp_path)
    c = cfg(agents={"family": LOCAL_AGENT}, routes={"terminal:terminal-user": "family"})
    c.embedding.enabled = "false"
    registry = Registry(c)
    runtimes = build_runtimes(c, registry)
    runtimes["main"].provider = Said("main")
    runtimes["family"].provider = Said("family")
    seen: dict = {}

    class FakeTerminal:
        name = "terminal"

        def set_handler(self, h): pass
        def set_stream_handler(self, h): pass
        def set_access(self, h): pass

        async def stop(self):
            pass

        async def start(self):
            seen["agents"] = sorted(gw.agents)
            seen["reply"] = await gw._handle_message("hello", "terminal-user", "terminal", None)
            seen["main_reply"] = await gw._handle_message("hello", "someone", "terminal", None)
            seen["probe"] = gw._probe_task

    tools = base_registry("shell", "file_read")
    gw = Gateway(c, runtimes["main"].provider, tools, [FakeTerminal()], runtimes=runtimes, models=registry)
    monkeypatch.setattr("micropaw.scheduler.Scheduler.run", lambda self: asyncio.sleep(0))
    await gw.start()
    assert seen["agents"] == ["family", "main"]
    assert seen["reply"] == "hi from family" and seen["main_reply"] == "hi from main"
    assert gw.agent is gw.agents["main"]


async def test_private_agent_turns_cloud_embeddings_off(monkeypatch):
    from micropaw.memory.embeddings import OpenAIEmbedding

    async def no_ollama(host, model):
        return False

    monkeypatch.setattr("micropaw.memory.embeddings.probe_ollama", no_ollama)

    def gateway(agents):
        c = cfg(agents=agents)
        c.provider_config.openai.api_key = "k"
        return Gateway(c, MagicMock(), MagicMock(), [], runtimes=build_runtimes(c, Registry(c)))

    assert isinstance(await gateway({})._create_embedding_provider(), OpenAIEmbedding)
    assert not isinstance(await gateway({"family": LOCAL_AGENT})._create_embedding_provider(), OpenAIEmbedding)
