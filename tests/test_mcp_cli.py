"""Tests for `paw mcp add|list|test|trust`."""

from __future__ import annotations

import json
import sys
import textwrap

import pytest
from click.testing import CliRunner

import micropaw.config as config
from micropaw import mcp as mcp_module
from micropaw.cli import main


@pytest.fixture(autouse=True)
def _tmp_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)


def _entries():
    return json.loads(config.CONFIG_FILE.read_text()).get("mcp_servers", {})


def test_add_prints_the_command_and_writes_only_after_yes():
    runner = CliRunner()
    out = runner.invoke(main, ["mcp", "add", "rea", "--", "npx", "-y", "rea-agents@1.2.3"], input="n\n")
    assert "npx -y rea-agents@1.2.3" in out.output and "not sandboxed" in out.output
    assert "Nothing was written" in out.output and not config.CONFIG_FILE.exists()
    out = runner.invoke(main, ["mcp", "add", "rea", "--", "npx", "-y", "rea-agents@1.2.3"], input="y\n")
    assert out.exit_code == 0
    assert _entries()["rea"] == {"command": "npx", "args": ["-y", "rea-agents@1.2.3"], "enabled": True}


def test_add_url_with_env_header():
    runner = CliRunner()
    args = ["mcp", "add", "web", "--url", "https://mcp.example.com/mcp", "--header", "Authorization=Bearer ${TOK}"]
    assert runner.invoke(main, args, input="y\n").exit_code == 0
    assert _entries()["web"]["headers"] == {"Authorization": "Bearer ${TOK}"}


def test_add_refuses_a_secret_written_into_the_file():
    args = ["mcp", "add", "web", "--url", "https://mcp.example.com/mcp", "--header", "Authorization=Bearer abc"]
    out = CliRunner().invoke(main, args, input="y\n")
    assert out.exit_code != 0 and "ENV_NAME" in out.output and not config.CONFIG_FILE.exists()


def test_add_needs_something_to_run_and_uses_presets(monkeypatch):
    runner = CliRunner()
    out = runner.invoke(main, ["mcp", "add", "nothing"])
    assert out.exit_code != 0 and "Nothing to run" in out.output
    monkeypatch.setitem(mcp_module.PRESETS, "demo", {"command": "uvx", "args": ["demo-mcp==1.0.0"]})
    assert runner.invoke(main, ["mcp", "add", "demo"], input="y\n").exit_code == 0
    assert _entries()["demo"]["args"] == ["demo-mcp==1.0.0"]
    assert runner.invoke(main, ["mcp", "add", "bad name", "--", "x"]).exit_code != 0


def test_list_test_and_trust(tmp_path):
    script = tmp_path / "s.py"
    template = textwrap.dedent('''
        import json, sys
        for line in sys.stdin:
            m = json.loads(line)
            if m.get("method") == "tools/list":
                r = {"tools": [{"name": "echo", "description": "DESC"}]}
            elif "id" in m:
                r = {}
            else:
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
    ''')
    script.write_text(template.replace("DESC", "one"))
    runner = CliRunner()
    assert "No MCP servers" in runner.invoke(main, ["mcp", "list"]).output
    runner.invoke(main, ["mcp", "add", "s", "--", sys.executable, "-I", str(script)], input="y\n")
    assert "not pinned yet" in runner.invoke(main, ["mcp", "list"]).output
    out = runner.invoke(main, ["mcp", "test", "s"])
    assert "started with 1 tools" in out.output and "mcp_s_echo" in out.output
    listed = runner.invoke(main, ["mcp", "list"]).output
    assert "pinned" in listed and "not pinned" not in listed
    script.write_text(template.replace("DESC", "two"))
    out = runner.invoke(main, ["mcp", "test", "s"])
    assert out.exit_code != 0 and "paw mcp trust s" in out.output
    old = _entries()["s"]["tools_hash"]
    assert runner.invoke(main, ["mcp", "trust", "s"], input="n\n").exit_code == 0
    assert _entries()["s"]["tools_hash"] == old
    assert "Trusted" in runner.invoke(main, ["mcp", "trust", "s"], input="y\n").output
    assert _entries()["s"]["tools_hash"] != old
    assert "started with 1 tools" in runner.invoke(main, ["mcp", "test", "s"]).output
    assert runner.invoke(main, ["mcp", "test", "ghost"]).exit_code != 0
