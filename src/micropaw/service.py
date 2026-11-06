"""Run MicroPaw in the background: service files, PID file, stop file, supervisor, install detection."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import importlib.metadata
import importlib.util
import json
import logging
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from micropaw import config
from micropaw.health import db_problem, is_paw, pid_alive

logger = logging.getLogger(__name__)

LABEL = "ai.micropaw.agent"  # launchd
TASK_NAME = "MicroPaw"  # Windows Task Scheduler
UNIT_NAME = "micropaw.service"  # systemd --user
HEADLESS_ARGS = ["start", "--headless"]

# extra name -> (module to import, pip requirement); same requirements as pyproject.toml
EXTRAS: dict[str, tuple[str, str]] = {
    "telegram": ("telegram", "python-telegram-bot>=21.0"),
    "discord": ("discord", "discord.py>=2.3"),
    "slack": ("slack_bolt", "slack-bolt>=1.18"),
    "whatsapp": ("aiohttp", "aiohttp>=3.9"),
    "browser": ("playwright", "playwright>=1.40"),
    "pdf": ("pypdf", "pypdf>=4.0"),
    "embeddings": ("fastembed", "fastembed>=0.4"),
}


class ServiceError(RuntimeError):
    """A problem the user can act on; the message is printed as is."""


def pid_file() -> Path:
    return config.CONFIG_DIR / "micropaw.pid"


def stop_file() -> Path:
    return config.CONFIG_DIR / "stop"


def log_file() -> Path:
    return config.CONFIG_DIR / "logs" / "gateway.log"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


# Service definitions (pure functions: they return text or argv)

def service_argv() -> list[str]:
    """The command a service manager runs: the console script next to this Python, or python -m."""
    exe = Path(sys.executable)
    if os.name == "nt":
        pythonw = exe.with_name("pythonw.exe")  # no console window
        return [str(pythonw if pythonw.exists() else exe), "-m", "micropaw", *HEADLESS_ARGS]
    paw = exe.with_name("paw")
    found = str(paw) if paw.exists() else shutil.which("paw")
    return [found, *HEADLESS_ARGS] if found else [str(exe), "-m", "micropaw", *HEADLESS_ARGS]


def _sd_quote(arg: str) -> str:
    arg = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{arg}"'


def systemd_unit(argv: list[str], path_env: str = "") -> str:
    """A systemd user unit. No keys: the service reads ~/.micropaw/config.json and .env itself."""
    lines = [
        "[Unit]",
        "Description=MicroPaw agent",
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "ExecStart=" + " ".join(_sd_quote(a) for a in argv),
        "Restart=on-failure",
        "RestartSec=5",
        "NoNewPrivileges=yes",
        "WorkingDirectory=%h",
        "Environment=PYTHONUNBUFFERED=1",
    ]
    if path_env:
        lines.append(f'Environment="PATH={path_env.replace("%", "%%")}"')
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def launchd_plist(argv: list[str], log_path: str, path_env: str = "") -> bytes:
    """A launchd agent plist. launchd has a bare PATH, so the install-time PATH is written in."""
    data: dict = {
        "Label": LABEL,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    if path_env:
        data["EnvironmentVariables"] = {"PATH": path_env}
    return plistlib.dumps(data)


def task_xml(argv: list[str], user: str = "") -> str:
    """Task Scheduler XML: at logon, hidden, no time limit, runs on battery, restarts on failure."""
    command = escape(f'"{argv[0]}"')
    arguments = escape(subprocess.list2cmdline(argv[1:]))
    who = f"<UserId>{escape(user)}</UserId>" if user else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>MicroPaw agent</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      {who}
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      {who}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>10</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def schtasks_argv(action: str, xml_path: str = "") -> list[str]:
    """schtasks arguments for create, delete, query, run, end, or xml (print the stored definition)."""
    if action == "create":
        return ["schtasks", "/Create", "/XML", xml_path, "/TN", TASK_NAME, "/F"]
    if action == "xml":
        return ["schtasks", "/Query", "/TN", TASK_NAME, "/XML"]
    return ["schtasks", f"/{action.capitalize()}", "/TN", TASK_NAME, *(["/F"] if action == "delete" else [])]


def cron_line(argv: list[str]) -> str:
    return f"@reboot {shlex.join(argv)} >> ~/.micropaw/logs/service.log 2>&1"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ServiceError(f"Could not run {cmd[0]}: {e}") from e


def _check(cmd: list[str]) -> str:
    r = _run(cmd)
    if r.returncode != 0:
        raise ServiceError(f"'{' '.join(cmd)}' failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        os.write(fd, data)
    finally:
        os.close(fd)


def _uid_domain() -> str:
    return f"gui/{os.getuid()}"


def systemd_available() -> bool:
    if sys.platform != "linux" or not shutil.which("systemctl") or not Path("/run/systemd/system").is_dir():
        return False
    try:
        return _run(["systemctl", "--user", "is-system-running"]).stdout.strip() in {"running", "degraded", "starting"}
    except ServiceError:
        return False


def _no_systemd_message(argv: list[str]) -> str:
    return (
        "systemd is not running here (a container, or WSL without systemd).\n"
        "Options: run MicroPaw in Docker (see docker-compose.yml), or add this line with 'crontab -e':\n"
        f"  {cron_line(argv)}"
    )


def linger_enabled() -> bool:
    r = _run(["loginctl", "show-user", getpass.getuser(), "--property=Linger"])
    return r.stdout.strip() == "Linger=yes"


def existing_definition() -> str | None:
    """What is installed now (for the 'replace it?' question), or None."""
    if sys.platform == "win32":
        r = _run(schtasks_argv("xml"))
        return r.stdout.strip() if r.returncode == 0 else None
    path = plist_path() if sys.platform == "darwin" else unit_path()
    return path.read_text(errors="replace") if path.exists() else None


def install(argv: list[str] | None = None) -> list[str]:
    """Install and start the service. Returns lines to show the user."""
    argv = argv or service_argv()
    path_env = os.environ.get("PATH", "")
    if sys.platform == "win32":
        # A logon trigger without a user means "any user", which needs admin rights
        name = os.environ.get("USERNAME", "")
        user = f"{os.environ['USERDOMAIN']}\\{name}" if "USERDOMAIN" in os.environ and name else name
        xml = config.CONFIG_DIR / "task.xml"
        _write_private(xml, task_xml(argv, user).encode("utf-16"))
        try:
            _check(schtasks_argv("create", str(xml)))
        finally:
            xml.unlink(missing_ok=True)
        _run(schtasks_argv("run"))
        return [f"Task '{TASK_NAME}' registered: it starts at logon, hidden, and restarts after a failure.",
                "Put API keys in ~/.micropaw/.env or config.json. 'paw service stop' stops it."]
    if sys.platform == "darwin":
        log = config.CONFIG_DIR / "logs" / "service.log"
        config.private_dir(log.parent)
        _write_private(plist_path(), launchd_plist(argv, str(log), path_env))
        _run(["launchctl", "bootout", f"{_uid_domain()}/{LABEL}"])  # fails when not loaded; fine
        _check(["launchctl", "bootstrap", _uid_domain(), str(plist_path())])
        return [f"Installed {plist_path()}.",
                "launchd does not see keys exported in .zshrc. Put them in ~/.micropaw/.env or config.json."]
    if not systemd_available():
        raise ServiceError(_no_systemd_message(argv))
    _write_private(unit_path(), systemd_unit(argv, path_env).encode())
    _check(["systemctl", "--user", "daemon-reload"])
    _check(["systemctl", "--user", "enable", "--now", UNIT_NAME])
    lines = [f"Installed {unit_path()}."]
    if _run(["loginctl", "enable-linger"]).returncode != 0 and not linger_enabled():
        lines.append("Could not turn on linger, so the service stops when you log out. Run: "
                     f"sudo loginctl enable-linger {getpass.getuser()}")
    return lines


def uninstall() -> list[str]:
    if existing_definition() is None:
        return ["No service is installed."]
    stop()
    if sys.platform == "win32":
        _check(schtasks_argv("delete"))
    elif sys.platform == "darwin":
        plist_path().unlink(missing_ok=True)
    else:
        if systemd_available():
            _run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
        unit_path().unlink(missing_ok=True)
        if systemd_available():
            _run(["systemctl", "--user", "daemon-reload"])
    return ["Service removed."]


def state() -> str:
    """Short service state: 'not installed', or what the manager says."""
    if existing_definition() is None:
        return "not installed"
    if sys.platform == "win32":
        for line in _run(schtasks_argv("query") + ["/FO", "LIST"]).stdout.splitlines():
            if line.startswith("Status:"):
                return line.split(":", 1)[1].strip().lower()
        return "installed"
    if sys.platform == "darwin":
        return "loaded" if _run(["launchctl", "print", f"{_uid_domain()}/{LABEL}"]).returncode == 0 else "not loaded"
    return _run(["systemctl", "--user", "is-active", UNIT_NAME]).stdout.strip() or "unknown"


def start() -> None:
    if existing_definition() is None:
        raise ServiceError("No service is installed. Run 'paw service install'.")
    if sys.platform == "win32":
        _check(schtasks_argv("run"))
    elif sys.platform == "darwin":
        if _run(["launchctl", "print", f"{_uid_domain()}/{LABEL}"]).returncode != 0:
            _check(["launchctl", "bootstrap", _uid_domain(), str(plist_path())])
    else:
        _check(["systemctl", "--user", "start", UNIT_NAME])


def stop(timeout: float = 30.0) -> bool:
    """Stop the gateway if it runs. Returns True when none is left running."""
    pid = running_gateway()
    try:
        managed = existing_definition() is not None
    except ServiceError:  # air-gapped mode blocks schtasks: a gateway that runs can still be asked to stop
        managed = False
    if sys.platform == "darwin" and managed:
        _run(["launchctl", "bootout", f"{_uid_domain()}/{LABEL}"])
    elif sys.platform == "linux" and managed and systemd_available():
        _run(["systemctl", "--user", "stop", UNIT_NAME])
    elif pid and os.name != "nt":
        os.kill(pid, signal.SIGTERM)  # a gateway started by hand: running_gateway() checked it is ours
    elif pid:
        request_stop()  # Windows has no signal that stops a process cleanly
    if pid and not wait_exit(pid, timeout):
        if sys.platform == "win32" and managed:
            _run(schtasks_argv("end"))
        return wait_exit(pid, 5.0)
    return True


def tail_lines(path: Path, count: int) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()[-count:]
    except OSError:
        return []


def read_pid() -> int | None:
    try:
        return int(pid_file().read_text().strip())
    except (OSError, ValueError):
        return None


def running_gateway() -> int | None:
    pid = read_pid()
    if pid and pid != os.getpid() and pid_alive(pid) and is_paw(pid):
        return pid
    return None


def acquire_pid() -> None:
    """Write our PID, or refuse when another gateway runs (two would fight over Telegram polling)."""
    other = running_gateway()
    if other:
        raise ServiceError(f"MicroPaw is already running (PID {other}). Stop it with 'paw service stop'. "
                           f"If that PID is wrong, delete {pid_file()}.")
    pid_file().parent.mkdir(parents=True, exist_ok=True)
    pid_file().write_text(str(os.getpid()))


def release_pid() -> None:
    if read_pid() == os.getpid():
        pid_file().unlink(missing_ok=True)


def request_stop() -> None:
    stop_file().parent.mkdir(parents=True, exist_ok=True)
    stop_file().write_text("")


def wait_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while pid_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.25)
    return True


def healthy() -> tuple[bool, str]:
    """True when the gateway process is alive and the database opens. Used by Docker and doctor."""
    pid = running_gateway()
    if not pid:
        return False, "not running"
    if problem := db_problem(config.DB_FILE):
        return False, problem
    return True, f"running, PID {pid}"


async def _attempt(make_gateway: Callable) -> None:
    gw = make_gateway()
    try:
        await gw.start()
    except (Exception, asyncio.CancelledError):
        with contextlib.suppress(Exception):
            await gw.stop()  # close channels and the database before a restart or exit
        raise


def _stop_on_signals(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)  # the gateway swaps in its own while it runs


async def _watch_stop_file(stop: asyncio.Event, poll: float) -> None:
    while True:
        if stop_file().exists():
            with contextlib.suppress(OSError):
                stop_file().unlink()
            logger.info("Stop file found, shutting down.")
            stop.set()
            return
        await asyncio.sleep(poll)


async def supervise(
    make_gateway: Callable,
    *,
    first_delay: float = 5.0,
    max_delay: float = 60.0,
    max_restarts: int = 10,
    window: float = 3600.0,
    poll: float | None = None,
) -> int:
    """Run the gateway; restart it after a crash (5 s, doubling to 60 s, at most 10 per hour).

    Returns 0 after a clean stop (signal or stop file) and 1 when it gave up. `paw stop` sends SIGTERM
    on Linux and macOS, so only Windows (poll=15 s by default) looks for the stop file; poll=0 never does.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    if poll is None:
        poll = 15.0 if os.name == "nt" else 0.0  # an idle daemon should not wake up just to look for a file
    watcher = asyncio.create_task(_watch_stop_file(stop, poll)) if poll else None
    restarts: deque[float] = deque()
    delay = first_delay
    try:
        while not stop.is_set():
            _stop_on_signals(loop, stop)
            started = time.monotonic()
            task = asyncio.ensure_future(_attempt(make_gateway))
            waiter = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            if not task.done() or task.cancelled():
                return 0
            err = task.exception()
            if err is None:
                return 0  # the gateway stopped itself after a signal
            _stop_on_signals(loop, stop)  # the gateway replaced the handlers; the back-off must still stop on a signal
            logger.error("Gateway stopped unexpectedly: %r", err, exc_info=err)
            now = time.monotonic()
            if now - started > 300:
                delay = first_delay  # it ran for a while; start the back-off over
            while restarts and now - restarts[0] > window:
                restarts.popleft()
            if len(restarts) >= max_restarts:
                logger.error("%d restarts in an hour; giving up.", max_restarts)
                return 1
            restarts.append(now)
            logger.warning("Restarting in %.0f s.", delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), delay)
            delay = min(delay * 2, max_delay)
        return 0
    finally:
        if watcher:
            watcher.cancel()


def run_headless(make_gateway: Callable) -> int:
    """Single-instance supervised gateway with a PID file. Returns the exit code."""
    acquire_pid()
    stop_file().unlink(missing_ok=True)  # a stale stop request must not stop this run
    try:
        return asyncio.run(supervise(make_gateway))
    except KeyboardInterrupt:
        return 0
    finally:
        release_pid()


def install_method() -> str:
    """docker, uv, pipx, editable, or pip."""
    if os.environ.get("MICROPAW_IN_DOCKER") or Path("/.dockerenv").exists():
        return "docker"
    parts = [p.lower() for p in Path(sys.prefix).parts]
    if "uv" in parts and "tools" in parts:
        return "uv"
    if "pipx" in parts and "venvs" in parts:
        return "pipx"
    try:
        direct = json.loads(importlib.metadata.distribution("micropaw").read_text("direct_url.json") or "{}")
    except (importlib.metadata.PackageNotFoundError, ValueError):
        direct = {}
    return "editable" if direct.get("dir_info", {}).get("editable") else "pip"


def installed_extras() -> list[str]:
    return [name for name, (module, _) in EXTRAS.items() if importlib.util.find_spec(module)]


def _pip_cmd(args: list[str]) -> list[str]:
    if importlib.util.find_spec("pip") is None and shutil.which("uv"):
        return ["uv", "pip", *args, "--python", sys.executable]  # a venv made by uv has no pip
    return [sys.executable, "-m", "pip", *args]


def extras_command(method: str, names: list[str]) -> list[str] | None:
    """The command that adds extras for this install method, or None when it must be done by hand."""
    if method == "docker":
        return None
    if method == "uv":
        wanted = ",".join(sorted({*installed_extras(), *names}))
        return ["uv", "tool", "install", "--reinstall", f"micropaw[{wanted}]"]
    reqs = [EXTRAS[n][1] for n in names]
    if method == "pipx":
        return ["pipx", "inject", "micropaw", *reqs]
    return _pip_cmd(["install", *reqs])


def update_command(method: str) -> list[str] | None:
    if method == "uv":
        return ["uv", "tool", "upgrade", "micropaw"]
    if method == "pipx":
        return ["pipx", "upgrade", "micropaw"]
    if method == "pip":
        return _pip_cmd(["install", "--upgrade", "micropaw"])
    return None  # docker and editable installs are updated by hand


def latest_version() -> str | None:
    """Latest release on PyPI (read-only, 5 s timeout), or None. None at once in air-gapped mode; in local
    mode the owner's command opens pypi.org for a short time."""
    import httpx

    from micropaw import net, privacy
    if privacy.owner_command_gate("The update check", ("pypi.org",)):
        return None
    try:
        with net.sync_client("https://pypi.org", timeout=5) as client:
            r = client.get("https://pypi.org/pypi/micropaw/json")
        r.raise_for_status()
        return r.json()["info"]["version"]
    except (httpx.HTTPError, ValueError, KeyError, OSError):
        return None


def installed_version() -> str:
    """The version on disk now (a fresh interpreter, because this process may be out of date)."""
    code = "import importlib.metadata as m; print(m.version('micropaw'))"
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return r.stdout.strip() or "unknown"
