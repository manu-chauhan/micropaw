"""Service builders, PID and stop files, supervisor, install detection, and the new CLI commands."""

import asyncio
import os
import plistlib
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from micropaw import cli, config
from micropaw import service as svc


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "mp")
    monkeypatch.setattr(config, "DB_FILE", tmp_path / "mp" / "data.db")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


# ---- builders (pure, run on every OS) ----

def test_systemd_unit_has_restart_and_no_secrets():
    text = svc.systemd_unit(["/home/u/.local/bin/paw", "start", "--headless"], "/usr/bin:/home/u/bin")
    assert 'ExecStart="/home/u/.local/bin/paw" "start" "--headless"' in text
    assert "Restart=on-failure" in text and "RestartSec=5" in text and "NoNewPrivileges=yes" in text
    assert "WantedBy=default.target" in text
    assert "KEY" not in text and "TOKEN" not in text


def test_systemd_unit_escapes_percent_dollar_and_quotes():
    text = svc.systemd_unit(['/a b/p%d$x"y', "start"])
    assert r'"/a b/p%%d$$x\"y"' in text


def test_launchd_plist():
    data = plistlib.loads(svc.launchd_plist(["/u/paw", "start", "--headless"], "/u/log", "/opt/homebrew/bin:/usr/bin"))
    assert data["Label"] == "ai.micropaw.agent"
    assert data["ProgramArguments"] == ["/u/paw", "start", "--headless"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["ThrottleInterval"] == 10
    assert data["EnvironmentVariables"] == {"PATH": "/opt/homebrew/bin:/usr/bin"}
    assert data["StandardOutPath"] == "/u/log"


def test_task_xml_settings_and_quoted_command():
    argv = ["C:\\Program Files\\Py\\pythonw.exe", "-m", "micropaw", "start", "--headless"]
    root = ET.fromstring(svc.task_xml(argv, "PC\\me").split("\n", 1)[1])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    def text(path):
        return root.find(path, ns).text

    assert text("t:Settings/t:DisallowStartIfOnBatteries") == "false"
    assert text("t:Settings/t:StopIfGoingOnBatteries") == "false"
    assert text("t:Settings/t:ExecutionTimeLimit") == "PT0S"
    assert text("t:Settings/t:Hidden") == "true"
    assert text("t:Settings/t:RestartOnFailure/t:Interval") == "PT1M"
    assert text("t:Settings/t:RestartOnFailure/t:Count") == "10"
    assert text("t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
    assert root.find("t:Triggers/t:LogonTrigger", ns) is not None
    assert text("t:Actions/t:Exec/t:Command") == '"C:\\Program Files\\Py\\pythonw.exe"'
    assert text("t:Actions/t:Exec/t:Arguments") == "-m micropaw start --headless"


def test_schtasks_argv():
    assert svc.schtasks_argv("create", "x.xml") == ["schtasks", "/Create", "/XML", "x.xml", "/TN", "MicroPaw", "/F"]
    assert svc.schtasks_argv("delete") == ["schtasks", "/Delete", "/TN", "MicroPaw", "/F"]
    assert svc.schtasks_argv("run") == ["schtasks", "/Run", "/TN", "MicroPaw"]
    assert svc.schtasks_argv("end") == ["schtasks", "/End", "/TN", "MicroPaw"]


def test_cron_line_quotes_paths():
    line = svc.cron_line(["/a b/paw", "start", "--headless"])
    assert line.startswith("@reboot '/a b/paw' start --headless") and "service.log" in line


def test_service_argv_ends_with_headless():
    argv = svc.service_argv()
    assert argv[-2:] == ["start", "--headless"]
    if os.name == "nt":
        assert argv[1:3] == ["-m", "micropaw"]


# ---- install flows with a fake service manager ----

class FakeRun:
    def __init__(self, fail=()):
        self.calls, self.fail = [], fail

    def __call__(self, cmd):
        self.calls.append(cmd)
        rc = 1 if any(f in " ".join(cmd) for f in self.fail) else 0
        return subprocess.CompletedProcess(cmd, rc, "running\n", "boom" if rc else "")


@pytest.mark.skipif(sys.platform != "linux", reason="systemd path")
def test_linux_install_writes_unit_0600_and_enables(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(svc, "_run", fake)
    monkeypatch.setattr(svc, "systemd_available", lambda: True)
    lines = svc.install(["/u/paw", "start", "--headless"])
    assert svc.unit_path().exists()
    if os.name != "nt":
        assert svc.unit_path().stat().st_mode & 0o777 == 0o600
    cmds = [" ".join(c) for c in fake.calls]
    assert "systemctl --user daemon-reload" in cmds
    assert "systemctl --user enable --now micropaw.service" in cmds
    assert "loginctl enable-linger" in cmds
    assert lines == [f"Installed {svc.unit_path()}."]


@pytest.mark.skipif(sys.platform != "linux", reason="systemd path")
def test_linux_install_says_what_to_run_when_linger_fails(monkeypatch):
    monkeypatch.setattr(svc, "_run", FakeRun(fail=("enable-linger", "show-user")))
    monkeypatch.setattr(svc, "systemd_available", lambda: True)
    lines = svc.install(["/u/paw", "start", "--headless"])
    assert any("sudo loginctl enable-linger" in line for line in lines)


@pytest.mark.skipif(sys.platform != "linux", reason="systemd path")
def test_linux_without_systemd_suggests_docker_and_cron(monkeypatch):
    monkeypatch.setattr(svc, "systemd_available", lambda: False)
    with pytest.raises(svc.ServiceError) as e:
        svc.install(["/u/paw", "start", "--headless"])
    assert "Docker" in str(e.value) and "@reboot" in str(e.value)
    assert not svc.unit_path().exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd path")
def test_macos_install_bootstraps(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(svc, "_run", fake)
    svc.install(["/u/paw", "start", "--headless"])
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in fake.calls)
    assert svc.plist_path().stat().st_mode & 0o777 == 0o600


def test_existing_definition_reads_unit(monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setattr(svc, "_run", lambda c: subprocess.CompletedProcess(c, 1, "", ""))
    assert svc.existing_definition() is None
    if sys.platform != "win32":
        path = svc.plist_path() if sys.platform == "darwin" else svc.unit_path()
        path.parent.mkdir(parents=True)
        path.write_text("old")
        assert svc.existing_definition() == "old"


def test_service_install_asks_before_replacing(monkeypatch):
    monkeypatch.setattr(svc, "existing_definition", lambda: "OLD DEFINITION")
    called = []
    monkeypatch.setattr(svc, "install", lambda: called.append(1) or ["done"])
    r = CliRunner().invoke(cli.main, ["service", "install"], input="n\n")
    assert "OLD DEFINITION" in r.output and not called
    r = CliRunner().invoke(cli.main, ["service", "install", "--force"])
    assert called and "done" in r.output


# ---- PID file, stop file, health ----

def test_pid_alive_for_self_and_dead_pid():
    assert svc.pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not svc.pid_alive(proc.pid)
    assert not svc.pid_alive(0)


def test_acquire_refuses_second_gateway_and_release_removes(monkeypatch):
    svc.pid_file().parent.mkdir(parents=True)
    svc.pid_file().write_text("4242")
    monkeypatch.setattr(svc, "pid_alive", lambda pid: True)
    monkeypatch.setattr(svc, "_process_name", lambda pid: "/x/python /x/bin/paw start --headless")
    with pytest.raises(svc.ServiceError, match="already running"):
        svc.acquire_pid()
    monkeypatch.setattr(svc, "_process_name", lambda pid: "/usr/bin/vim notes")  # PID reused by something else
    svc.acquire_pid()
    assert svc.read_pid() == os.getpid()
    svc.release_pid()
    assert not svc.pid_file().exists()


def test_release_keeps_file_of_another_process():
    svc.pid_file().parent.mkdir(parents=True)
    svc.pid_file().write_text("1")
    svc.release_pid()
    assert svc.pid_file().exists()


def test_healthy_needs_pid_and_database(monkeypatch):
    ok, why = svc.healthy()
    assert not ok and "not running" in why
    monkeypatch.setattr(svc, "running_gateway", lambda: 99)
    ok, why = svc.healthy()
    assert not ok and "database" in why
    import sqlite3
    config.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_FILE)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (a)")
    con.commit()
    ok, why = svc.healthy()
    con.close()
    assert ok and "99" in why


def test_doctor_quiet_exit_codes(monkeypatch):
    monkeypatch.setattr(svc, "healthy", lambda: (False, "x"))
    r = CliRunner().invoke(cli.main, ["doctor", "--quiet"])
    assert r.exit_code == 1 and r.output == ""
    monkeypatch.setattr(svc, "healthy", lambda: (True, "x"))
    assert CliRunner().invoke(cli.main, ["doctor", "--quiet"]).exit_code == 0


# ---- supervisor ----

class FakeGateway:
    def __init__(self, script):
        self.script, self.stopped = script, False

    async def start(self):
        action = self.script.pop(0)
        if action == "crash":
            raise RuntimeError("boom")
        if action == "hang":
            await asyncio.sleep(3600)

    async def stop(self):
        self.stopped = True


def _supervise(script, **kw):
    made = []

    def make():
        gw = FakeGateway(script)
        made.append(gw)
        return gw

    kw.setdefault("first_delay", 0.01)
    kw.setdefault("max_delay", 0.02)
    return asyncio.run(svc.supervise(make, **kw)), made


def test_supervisor_restarts_after_crash_then_stops_cleanly():
    code, made = _supervise(["crash", "crash", "clean"])
    assert code == 0 and len(made) == 3 and made[0].stopped and made[1].stopped


def test_supervisor_gives_up_after_too_many_restarts():
    code, made = _supervise(["crash"] * 20, max_restarts=3)
    assert code == 1 and len(made) == 4


def test_supervisor_stops_on_stop_file():
    svc.stop_file().parent.mkdir(parents=True)
    script = ["hang"]
    code, made = None, []

    async def go():
        task = asyncio.create_task(svc.supervise(lambda: made.append(FakeGateway(script)) or made[-1], poll=0.01))
        await asyncio.sleep(0.1)
        svc.request_stop()
        return await asyncio.wait_for(task, 5)

    code = asyncio.run(go())
    assert code == 0 and made[0].stopped and not svc.stop_file().exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal handlers")
def test_signal_during_the_backoff_still_stops_the_supervisor():
    import signal

    class Crashing(FakeGateway):
        async def start(self):
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: None)  # what the gateway does
            raise RuntimeError("boom")

    async def go():
        task = asyncio.create_task(svc.supervise(lambda: Crashing([]), first_delay=30, poll=0.01))
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(go()) == 0


def test_backoff_doubles_up_to_cap(monkeypatch):
    delays = []

    async def fake_wait_for(coro, timeout):
        coro.close()
        delays.append(timeout)
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    _supervise(["crash"] * 6 + ["clean"], first_delay=5, max_delay=60)
    assert delays == [5, 10, 20, 40, 60, 60]


def test_run_headless_writes_and_removes_pid():
    seen = []

    def make():
        seen.append(svc.read_pid())
        return FakeGateway(["clean"])

    assert svc.run_headless(make) == 0
    assert seen == [os.getpid()] and not svc.pid_file().exists()


def test_run_headless_clears_stale_stop_file():
    svc.request_stop()
    assert svc.run_headless(lambda: FakeGateway(["clean"])) == 0


# ---- install detection ----

def test_extras_table_matches_pyproject():
    root = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(root.read_text())["project"]["optional-dependencies"]
    for name, (_, req) in svc.EXTRAS.items():
        assert declared[name] == [req]


def test_extras_commands(monkeypatch):
    monkeypatch.setattr(svc, "installed_extras", lambda: ["discord"])
    assert svc.extras_command("uv", ["telegram"]) == [
        "uv", "tool", "install", "--reinstall", "micropaw[discord,telegram]"]
    assert svc.extras_command("pipx", ["pdf"]) == ["pipx", "inject", "micropaw", "pypdf>=4.0"]
    assert svc.extras_command("docker", ["pdf"]) is None
    cmd = svc.extras_command("pip", ["pdf"])
    assert "pypdf>=4.0" in cmd and "install" in cmd


def test_update_commands():
    assert svc.update_command("uv") == ["uv", "tool", "upgrade", "micropaw"]
    assert svc.update_command("pipx") == ["pipx", "upgrade", "micropaw"]
    assert svc.update_command("docker") is None and svc.update_command("editable") is None
    assert "--upgrade" in svc.update_command("pip")


def test_install_method_from_prefix(monkeypatch):
    monkeypatch.delenv("MICROPAW_IN_DOCKER", raising=False)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(sys, "prefix", str(Path("home") / "u" / ".local" / "share" / "uv" / "tools" / "micropaw"))
    assert svc.install_method() == "uv"
    monkeypatch.setattr(sys, "prefix", str(Path("home") / "u" / ".local" / "pipx" / "venvs" / "micropaw"))
    assert svc.install_method() == "pipx"
    monkeypatch.setenv("MICROPAW_IN_DOCKER", "1")
    assert svc.install_method() == "docker"


def test_update_asks_and_stops_service_around_upgrade(monkeypatch):
    monkeypatch.setattr(svc, "install_method", lambda: "uv")
    monkeypatch.setattr(svc, "latest_version", lambda: "9.9.9")
    monkeypatch.setattr(svc, "running_gateway", lambda: 5)
    events = []
    monkeypatch.setattr(svc, "stop", lambda: events.append("stop") or True)
    monkeypatch.setattr(svc, "start", lambda: events.append("start"))
    monkeypatch.setattr(svc, "existing_definition", lambda: "x")
    monkeypatch.setattr(svc, "installed_version", lambda: "9.9.9")
    monkeypatch.setattr(cli, "_run_install_command", lambda cmd: events.append(cmd) or True)
    r = CliRunner().invoke(cli.main, ["update"], input="n\n")
    assert events == [] and "uv tool upgrade micropaw" in r.output
    r = CliRunner().invoke(cli.main, ["update", "--yes"])
    assert events == ["stop", ["uv", "tool", "upgrade", "micropaw"], "start"]
    assert "9.9.9" in r.output


def test_update_up_to_date_does_nothing(monkeypatch):
    monkeypatch.setattr(svc, "install_method", lambda: "uv")
    monkeypatch.setattr(svc, "latest_version", lambda: cli.__version__)
    ran = []
    monkeypatch.setattr(cli, "_run_install_command", lambda cmd: ran.append(cmd))
    r = CliRunner().invoke(cli.main, ["update", "--yes"])
    assert not ran and "up to date" in r.output


def test_extras_add_rejects_unknown_name():
    r = CliRunner().invoke(cli.main, ["extras", "add", "nope"])
    assert r.exit_code != 0


def test_start_without_tty_goes_headless(monkeypatch):
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    monkeypatch.setattr(cli, "_start_headless", lambda: 0)
    assert CliRunner().invoke(cli.main, ["start"]).exit_code == 0


def test_headless_refuses_second_instance(monkeypatch):
    monkeypatch.setattr(svc, "running_gateway", lambda: 77)
    assert cli._start_headless() == 1


def test_private_writers_open_in_binary_mode(tmp_path, monkeypatch):
    """Windows turns every 0x0A into 0D 0A without O_BINARY, which corrupts UTF-16 task XML."""
    from micropaw.tools import google_auth

    binary = getattr(os, "O_BINARY", 0x8000)  # the real flag on Windows, a stand-in elsewhere
    monkeypatch.setattr(os, "O_BINARY", binary, raising=False)
    flags = []
    real_open = os.open

    def spy(path, f, *a, **k):
        flags.append(f)
        return real_open(path, f if os.name == "nt" else f & ~binary, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    data = "<Task>\n<a/>\n</Task>\n".encode("utf-16")
    svc._write_private(tmp_path / "t.xml", data)
    config._write_bytes(tmp_path / "c.json", b"{}\n")
    monkeypatch.setattr(google_auth, "_TOKEN_FILE", tmp_path / "tok.json")
    monkeypatch.setattr(google_auth, "CONFIG_DIR", tmp_path)
    google_auth._save_tokens({"a": 1})
    assert len(flags) == 3 and all(f & binary for f in flags)
    assert (tmp_path / "t.xml").read_bytes().decode("utf-16").count("\n") == 3


def test_doctor_sandbox_row_is_a_warning_on_windows_and_the_report_does_not_fail(monkeypatch, tmp_path):
    from micropaw.tools import sandbox
    from micropaw.tools.sandbox import Sandbox

    monkeypatch.setattr(sandbox, "resolve", lambda: Sandbox("none", host_asks=True))
    monkeypatch.setattr(sandbox, "configure", lambda p: None)
    monkeypatch.setattr(svc, "running_gateway", lambda: None)  # gateway stopped
    # not os.name itself: pathlib would build WindowsPath
    monkeypatch.setattr(cli, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    out = CliRunner().invoke(cli.main, ["doctor"])
    row = next(line for line in out.output.splitlines() if "Shell sandbox" in line)
    assert "warn" in row and "FAIL" not in row
    assert out.exit_code == 0
    monkeypatch.setattr(cli, "os", os)
    row = next(line for line in CliRunner().invoke(cli.main, ["doctor"]).output.splitlines() if "Shell sandbox" in line)
    assert "FAIL" in row
