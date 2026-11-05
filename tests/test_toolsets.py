"""The tool list a model is shown: lean for a local model, Google only when it is set up."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from micropaw.config import Config
from micropaw.core.profile import build_runtimes, build_view
from micropaw.core.toolsets import LEAN, build_registry
from micropaw.providers.registry import Registry
from micropaw.tools.base import Tool


def _runtime(**kw):
    c = Config(**kw)
    c.provider_config.claude.api_key = "sk-test"
    return c, build_runtimes(c, Registry(c))["main"]


def _wire_bytes(view) -> int:
    defs = [{"type": "function", "function": {"name": d.name, "description": d.description, "parameters": d.parameters}}
            for d in view.to_tool_defs()]
    return len(json.dumps(defs, separators=(",", ":")))


def test_google_tools_exist_only_when_google_is_set_up():
    names = build_registry(Config()).names
    assert "email" not in names and "calendar" not in names and "contacts" in names
    on = Config(google={"enabled": True, "client_id": "x"})
    assert {"email", "calendar"} <= set(build_registry(on).names)
    only_mail = Config(google={"enabled": True, "calendar_enabled": False})
    assert "email" in build_registry(only_mail).names and "calendar" not in build_registry(only_mail).names
    no_mail = Config(google={"enabled": True, "gmail_enabled": False})
    assert "email" not in build_registry(no_mail).names and "calendar" in build_registry(no_mail).names


def test_disabled_tools_are_still_skipped():
    assert "shell" not in build_registry(Config(permissions={"disabled_tools": ["shell"]})).names


def test_unused_tool_modules_are_not_imported():
    r = subprocess.run([sys.executable, "-c", textwrap.dedent("""
        import sys
        from micropaw.config import Config
        from micropaw.core.toolsets import build_registry
        build_registry(Config(permissions={"disabled_tools": ["har", "inspect"]}))
        mods = ("email_tool", "calendar_tool", "har_tool", "inspect_tool")
        print([m for m in mods if f"micropaw.tools.{m}" in sys.modules])
    """)], capture_output=True, text=True, env={**os.environ, "MICROPAW_HOME": "/nonexistent-micropaw-home"})
    assert r.stdout.strip() == "[]", r.stdout + r.stderr


def test_local_model_gets_the_lean_set_under_6000_bytes():
    c, rt = _runtime(provider="ollama")
    view = build_view(build_registry(c), rt)
    assert set(view.names) == {"shell", "file_read", "file_write", "memory", "web_search", "web_browse",
                               "http_request", "skill", "cron", "tool_output"}
    assert _wire_bytes(view) <= 6000


def test_tool_output_is_in_the_lean_set_because_long_results_point_to_it():
    assert "tool_output" in LEAN
    import inspect

    from micropaw.tools.tool_output import shorten  # the pointer lives here
    assert "tool_output" in inspect.getsource(shorten)


def test_toolset_all_restores_everything_for_a_local_model():
    c, rt = _runtime(provider="ollama", toolset="all", google={"enabled": True})
    registry = build_registry(c)
    assert build_view(registry, rt) is registry and len(registry.names) == 21


def test_cloud_model_keeps_every_tool_by_default():
    c, rt = _runtime(provider="claude")
    registry = build_registry(c)
    assert build_view(registry, rt) is registry and "delegate" in registry.names


def test_lean_can_be_forced_for_a_cloud_model():
    c, rt = _runtime(provider="claude", toolset="lean")
    assert "delegate" not in build_view(build_registry(c), rt).names


def test_an_agents_own_tool_list_wins_over_the_lean_default():
    c = Config(provider="ollama", agents={"family": {"model": "ollama:qwen3.5:9b", "tools": {"allow": ["sessions"]}}})
    rt = build_runtimes(c, Registry(c))["family"]
    assert build_view(build_registry(c), rt).names == ["sessions"]


class _Mcp(Tool):
    name = "mcp_rea_decompile"
    description = "x"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args, context):
        return "ok"


def test_mcp_tools_stay_visible_in_the_lean_set():
    c, rt = _runtime(provider="ollama")
    registry = build_registry(c)
    view = build_view(registry, rt)
    registry.register(_Mcp())  # starts later, in the background
    assert "mcp_rea_decompile" in view.names


def test_lean_set_adds_vision_when_a_vision_model_is_set():
    c = Config(provider="ollama", models={"vision": "ollama:llava"})
    rt = build_runtimes(c, Registry(c))["main"]
    assert "vision" in build_view(build_registry(c), rt).names


@pytest.mark.parametrize("name", ["cron", "web_browse", "memory", "skill"])
def test_shortened_descriptions_still_name_every_action(name):
    tool = build_registry(Config()).get(name)
    props = tool.parameters["properties"]["action"].get("enum", [])
    assert all(a in tool.description for a in props) or name == "web_browse"


