"""Children with no network: the shell and MCP sandbox in air-gapped and local mode."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from micropaw import privacy
from micropaw.config import MCPServerConfig, PermissionsConfig, PrivacyConfig
from micropaw.mcp import MCPError, MCPServer
from micropaw.tools import sandbox
from micropaw.tools.sandbox import Sandbox

LINUX = sys.platform.startswith("linux")


def _bwrap_works() -> bool:
    sandbox._probes.clear()
    return LINUX and sandbox._available("bwrap")


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="bwrap missing or user namespaces blocked")


@pytest.fixture(autouse=True)
def _fresh_probes():
    sandbox._probes.clear()
    sandbox._broken.clear()
    sandbox.configure(PermissionsConfig())
    yield
    sandbox._probes.clear()


def mode(name, **kw):
    privacy._reset_for_tests()
    privacy.freeze(name)
    privacy.configure(PrivacyConfig(mode=name, **kw))


def fake_stub(platform):
    return SimpleNamespace(platform=platform, base_prefix=sys.base_prefix, prefix=sys.prefix,
                           executable=sys.executable)


# --- the self test ------------------------------------------------------------------------------------------------

@needs_bwrap
def test_selftest_passes_with_a_real_wrapper():
    assert sandbox._selftest() is True


@pytest.mark.parametrize("report,ok", [
    ({"net": "blocked", "bus": "hidden", "config": "hidden"}, True),
    ({"net": "timeout", "bus": "hidden", "config": "hidden"}, False),   # a timeout is not proof
    ({"net": "connected", "bus": "hidden", "config": "hidden"}, False),
    ({"net": "blocked", "bus": "visible", "config": "hidden"}, False),
    ({"net": "blocked", "bus": "hidden", "config": "visible"}, False),
])
def test_selftest_reads_the_report(monkeypatch, report, ok):
    done = SimpleNamespace(returncode=0, stdout=json.dumps(report) + "\n")
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: done)
    assert sandbox._selftest() is ok


def test_selftest_fails_when_the_wrapper_cannot_start(monkeypatch):
    def boom(*a, **k):
        raise OSError("no bwrap")
    monkeypatch.setattr(sandbox.subprocess, "run", boom)
    assert sandbox._selftest() is False


# --- resolve in each mode -------------------------------------------------------------------------------------

@pytest.mark.parametrize("platform,word", [("win32", "Windows"), ("darwin", "macOS")])
def test_airgapped_refuses_shell_where_children_cannot_be_cut_off(monkeypatch, platform, word):
    mode("airgapped")
    monkeypatch.setattr(sandbox, "sys", fake_stub(platform))
    sb = sandbox.resolve()
    assert sb.error.startswith(word) and "air-gapped" in sb.error and not sb.active


def test_airgapped_refuses_docker_and_podman():
    mode("airgapped")
    for kind in ("docker", "podman"):
        sandbox.configure(PermissionsConfig(sandbox=kind))
        assert "pull images" in sandbox.resolve().error


def test_airgapped_never_shares_the_network(monkeypatch):
    mode("airgapped")
    sandbox.configure(PermissionsConfig(sandbox_network=True))
    monkeypatch.setattr(sandbox, "isolation_error", lambda: "")
    sb = sandbox.resolve()
    assert sb == Sandbox("bwrap", False)


def test_airgapped_without_bwrap_has_no_shell(monkeypatch):
    mode("airgapped")
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    sandbox.configure(PermissionsConfig(sandbox="off"))
    assert "bubblewrap" in sandbox.resolve().error or sandbox.resolve().error


def test_local_mode_ignores_sandbox_network_unless_allowed(monkeypatch):
    monkeypatch.setattr(sandbox, "_available", lambda kind: kind == "bwrap")
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    sandbox.configure(PermissionsConfig(sandbox_network=True))
    mode("local")
    assert sandbox.resolve().network is False
    mode("local", allow=["shell_network"])
    assert sandbox.resolve().network is True
    mode("normal")
    assert sandbox.resolve().network is True


def test_local_shell_with_network_always_asks(monkeypatch):
    from micropaw.tools.base import ToolContext
    from micropaw.tools.shell import ShellTool
    ctx = ToolContext("c", "u", "terminal", allow_shell=True, confirm_dangerous=False, workspace="/tmp")
    tool = ShellTool()
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("bwrap", False))
    mode("local")
    assert tool.needs_approval({"command": "ls"}, ctx) is None          # no network: may skip the ask
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("bwrap", True))
    assert tool.needs_approval({"command": "ls"}, ctx)                   # network on: always asks
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("none"))
    assert tool.needs_approval({"command": "ls"}, ctx)                   # no isolation: always asks
    mode("normal")
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("bwrap", True))
    assert tool.needs_approval({"command": "ls"}, ctx) is None          # normal mode is unchanged


# --- the MCP wrapper ----------------------------------------------------------------------------------------------

def test_mcp_argv_uses_the_allow_list_layout(tmp_path):
    cfg = MCPServerConfig(command="/usr/bin/python3", args=["s.py"], paths=[str(tmp_path)], env={
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus", "SSH_AUTH_SOCK": "/x", "KEEP": "1"})
    a = sandbox.mcp_wrap("/usr/bin/python3", ["s.py"], cfg, {"PATH": "/usr/bin", "SECRET_KEY": "k"}).argv
    pairs = [a[i:i + 2] for i in range(len(a))]
    assert not any(a[i:i + 3] == ["--bind", "/", "/"] or a[i:i + 3] == ["--ro-bind", "/", "/"] for i in range(len(a)))
    assert "--unshare-all" in a and "--share-net" not in a and "--clearenv" in a
    for hidden in ("/run", "/home", "/var", "/tmp"):
        assert ["--tmpfs", hidden] in pairs
    assert ["--ro-bind", str(tmp_path)] == a[a.index(str(tmp_path)) - 1:a.index(str(tmp_path))] + [str(tmp_path)]
    setenv = {a[i + 1]: a[i + 2] for i in range(len(a)) if a[i] == "--setenv"}
    assert setenv["KEEP"] == "1" and "SSH_AUTH_SOCK" not in setenv and "DBUS_SESSION_BUS_ADDRESS" not in setenv
    assert "SECRET_KEY" not in setenv
    assert a[a.index("--") + 1:] == ["/usr/bin/python3", "s.py"]


def test_mcp_share_net_only_in_local_mode_when_the_server_asks():
    cfg = MCPServerConfig(command="/usr/bin/true", network=True)
    mode("airgapped")
    assert "--share-net" not in sandbox.mcp_wrap("/usr/bin/true", [], cfg, {}).argv
    mode("local")
    assert "--share-net" in sandbox.mcp_wrap("/usr/bin/true", [], cfg, {}).argv
    cfg.network = False
    assert "--share-net" not in sandbox.mcp_wrap("/usr/bin/true", [], cfg, {}).argv


@pytest.mark.parametrize("command,args,refused", [
    ("npx", ["-y", "pkg"], True), ("uvx", ["pkg"], True), ("pipx", ["run", "pkg"], True),
    ("npx.cmd", ["-y", "pkg"], True), ("python", ["server.py"], False),
])
def test_downloading_commands_are_refused_in_airgapped(command, args, refused):
    mode("airgapped")
    why = privacy.mcp_refusal("x", MCPServerConfig(command=command, args=args))
    assert bool(why) is refused
    if refused:
        assert "download" in why
    mode("local")
    assert privacy.mcp_refusal("x", MCPServerConfig(command=command, args=args)) is None


def test_mcp_http_servers_need_to_be_trusted_in_airgapped():
    srv = MCPServerConfig(url="http://localhost:9000/mcp")
    mode("airgapped")
    assert "trusted_local_services" in privacy.mcp_refusal("fetch", srv)
    mode("airgapped", trusted_local_services=["fetch"])
    assert privacy.mcp_refusal("fetch", srv) is None
    lan = MCPServerConfig(url="http://192.168.1.9:9000/mcp")
    mode("airgapped", trusted_local_services=["lan"])
    assert "not on this machine" in privacy.mcp_refusal("lan", lan)  # the agent's tools never reach the LAN
    mode("local")
    assert privacy.mcp_refusal("fetch", srv) is None
    assert "not on this machine" in privacy.mcp_refusal("lan", lan)
    mode("normal")
    assert privacy.mcp_refusal("lan", lan) is None


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_airgapped_stdio_mcp_is_refused_on_windows_and_macos(monkeypatch, platform, tmp_path):
    mode("airgapped")
    monkeypatch.setattr(sandbox, "sys", fake_stub(platform))
    srv = MCPServer("x", MCPServerConfig(command=sys.executable))
    with pytest.raises(MCPError, match="air-gapped"):
        asyncio.run(srv._launch())


SERVER = textwrap.dedent('''
    import json, os, socket, sys
    def probe():
        r = {}
        s = socket.socket(); s.settimeout(1.0)
        try:
            s.connect(("127.0.0.1", int(os.environ["PROBE_PORT"]))); r["loopback"] = "open"
        except OSError:
            r["loopback"] = "closed"
        r["config"] = os.path.exists(os.environ["PROBE_CONFIG"])
        r["bus"] = "DBUS_SESSION_BUS_ADDRESS" in os.environ
        try:
            open(os.environ["PROBE_CONFIG"] + ".new", "w").write("{}"); r["wrote"] = True
        except OSError:
            r["wrote"] = False
        return r
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m["method"] == "tools/list":
            res = {"tools": [{"name": "probe", "description": "p", "inputSchema": {"type": "object"}}]}
        elif m["method"] == "tools/call":
            res = {"content": [{"type": "text", "text": json.dumps(probe())}]}
        else:
            res = {}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)
''')


def _run_probe(tmp_path, monkeypatch, **cfg_kw) -> dict:
    (tmp_path / "server.py").write_text(SERVER)
    cfg_dir = tmp_path / "micropaw-home"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "config.json").write_text("{}")
    monkeypatch.setattr(sandbox, "CONFIG_DIR", cfg_dir)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    python = Path(sys.executable).resolve()
    cfg = MCPServerConfig(
        command=str(python), args=[str(tmp_path / "server.py")],
        paths=[str(tmp_path), sys.base_prefix],
        env={"PROBE_PORT": str(listener.getsockname()[1]), "PROBE_CONFIG": str(cfg_dir / "config.json"),
             "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}, **cfg_kw)

    async def go():
        srv = MCPServer("probe", cfg)
        try:
            await srv.start()
            return json.loads((await srv.request("tools/call", {"name": "probe", "arguments": {}}))
                              ["content"][0]["text"])
        finally:
            await srv.close()

    try:
        return asyncio.run(go())
    finally:
        listener.close()


@needs_bwrap
def test_airgapped_mcp_child_has_no_network_no_config_no_bus(tmp_path, monkeypatch):
    mode("airgapped")
    got = _run_probe(tmp_path, monkeypatch)
    assert got == {"loopback": "closed", "config": False, "bus": False, "wrote": got["wrote"]}
    assert not (tmp_path / "micropaw-home" / "config.json.new").exists()  # nothing reached the real folder


@needs_bwrap
def test_airgapped_mcp_network_flag_is_ignored(tmp_path, monkeypatch):
    mode("airgapped")
    assert _run_probe(tmp_path, monkeypatch, network=True)["loopback"] == "closed"


@needs_bwrap
def test_local_mcp_child_gets_network_only_when_it_asks(tmp_path, monkeypatch):
    mode("local")
    assert _run_probe(tmp_path, monkeypatch)["loopback"] == "closed"
    assert _run_probe(tmp_path, monkeypatch, network=True)["loopback"] == "open"
    assert _run_probe(tmp_path, monkeypatch, network=True)["config"] is False  # still cannot see the config


# --- the same through the real hook ---------------------------------------------------------------------------------

@needs_bwrap
def test_airgapped_child_interpreter_runs_only_isolated_commands(tmp_path):
    code = textwrap.dedent(f'''
        import subprocess, sys
        from pathlib import Path
        from micropaw import privacy, egress
        from micropaw.config import PrivacyConfig, PermissionsConfig
        privacy.freeze("airgapped")
        privacy.configure(PrivacyConfig())
        assert privacy.ensure_guard() is None
        from micropaw.tools import sandbox
        sandbox.configure(PermissionsConfig())
        sb = sandbox.resolve()
        assert sb.kind == "bwrap" and not sb.network, sb
        ws = Path({str(tmp_path / "ws")!r}); ws.mkdir(exist_ok=True)
        argv = sandbox.wrap(sb, "echo isolated", ws, {{"PATH": "/usr/bin:/bin"}}).argv
        print(subprocess.run(argv, capture_output=True, text=True).stdout.strip())
        try:
            subprocess.run([sys.executable, "-c", "pass"])
        except egress.EgressBlocked:
            print("blocked")
        fake = [argv[0], "--die-with-parent", "--", "/bin/sh", "-c", "echo --unshare-net"]
        try:
            subprocess.run(fake)
        except egress.EgressBlocked:
            print("fake blocked")
    ''')
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "MICROPAW_HOME": str(tmp_path.parent / (tmp_path.name + "-home"))})
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["isolated", "blocked", "fake", "blocked"]
