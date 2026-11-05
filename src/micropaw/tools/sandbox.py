"""Run shell commands in a sandbox: bwrap (Linux), sandbox-exec (macOS, experimental), Docker or Podman when chosen.

`auto` picks only bwrap or sandbox-exec. Probes run once per process, each capped at PROBE_TIMEOUT.
A sandbox that fails never falls back to the host silently: see `mark_failed`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from micropaw.config import CONFIG_DIR, PermissionsConfig

PROBE_TIMEOUT = 0.2
PULL_TIMEOUT = 300

# Only these go into a bwrap sandbox; HOME is set to the workspace
_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "TERM", "TZ")
_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc")
_HIDDEN_DIRS = ("/tmp", "/home", "/root", "/run", "/var")
# Under $HOME on macOS, reads are denied except the workspace and these toolchain dirs
_MAC_TOOLCHAINS = (".local/bin", ".cargo/bin", ".pyenv", ".nvm", ".rustup", ".volta")


class SandboxError(Exception):
    pass


@dataclass(frozen=True)
class Sandbox:
    kind: str  # bwrap | seatbelt | docker | podman | none
    network: bool = False
    image: str = ""
    error: str = ""  # a sandbox was asked for but cannot run; shell refuses

    @property
    def active(self) -> bool:
        return self.kind != "none"

    @property
    def label(self) -> str:
        """Where a command runs, for the approval text."""
        if not self.active:
            return "On this computer:"
        return f"In sandbox ({'network on' if self.network else 'no network'}):"

    @property
    def may_skip_ask(self) -> bool:
        """Seatbelt is experimental and never skips the ask."""
        return self.kind != "seatbelt"


@dataclass
class Wrapped:
    argv: list[str]
    env: dict[str, str] | None = None  # None = the argv carries its own environment
    cleanup: list[str] = field(default_factory=list)  # run after a timeout


_config: PermissionsConfig | None = None
_probes: dict[str, bool] = {}
_pulled: set[str] = set()


def configure(permissions: PermissionsConfig) -> None:
    global _config
    _config = permissions
    _probes.clear()


def _settings() -> PermissionsConfig:
    global _config
    if _config is None:
        try:
            from micropaw.config import load_config
            _config = load_config().permissions
        except Exception:
            _config = PermissionsConfig()
    return _config


def mark_failed(kind: str) -> None:
    """A sandbox that passed the probe failed for real: stop using it in this process."""
    _probes[kind] = False


def _run(argv: list[str]) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=PROBE_TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _available(kind: str) -> bool:
    if kind in _probes:
        return _probes[kind]
    if kind == "bwrap":
        ok = sys.platform.startswith("linux") and shutil.which("bwrap") is not None and _run(
            _bwrap(Sandbox("bwrap"), "true", Path(tempfile.gettempdir()).resolve(), {}).argv)
    elif kind == "seatbelt":
        ok = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None and _run(
            ["sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true"])
    else:
        ok = shutil.which(kind) is not None and _run([kind, "info"])
    _probes[kind] = ok
    return ok


def resolve() -> Sandbox:
    """The sandbox in effect for the current settings (probes cached for the process)."""
    cfg = _settings()
    net, image, mode = cfg.sandbox_network, cfg.sandbox_image, cfg.sandbox
    if mode == "off":
        return Sandbox("none")
    if mode == "auto":
        for kind in ("bwrap", "seatbelt"):
            if _available(kind):
                return Sandbox(kind, net)
        return Sandbox("none")
    if mode == "docker" and Path("/.dockerenv").exists() and Path("/var/run/docker.sock").exists():
        return Sandbox("none", error="MicroPaw runs in a container with the Docker socket mounted, "
                                     "so the docker sandbox is refused.")
    if _available(mode):
        return Sandbox(mode, net, image)
    return Sandbox("none", error=f"The {mode} sandbox is not available on this computer. "
                                 "Fix it or set permissions.sandbox to auto or off.")


def describe() -> str:
    """One line for `paw doctor`."""
    sb = resolve()
    if sb.error:
        return sb.error
    if not sb.active:
        extra = " Every command asks first." if os.name == "nt" else ""
        return f"No sandbox: shell commands run on this computer.{extra}"
    exp = " (experimental, always asks)" if not sb.may_skip_ask else ""
    return f"Shell runs in {sb.kind}{exp}, {'network on' if sb.network else 'no network'}."


def _check_workspace(ws: Path, home: Path) -> None:
    if ws == Path(ws.anchor) or ws == home or ws in home.parents or ws in CONFIG_DIR.resolve().parents:
        raise SandboxError(f"The workspace {ws} is too wide for a sandbox (it holds your home or MicroPaw's files).")


def _bwrap(sb: Sandbox, command: str, ws: Path, env: dict[str, str]) -> Wrapped:
    a = ["bwrap", "--unshare-all"]
    if sb.network:
        a += ["--share-net"]
    a += ["--die-with-parent", "--new-session"]
    bound = list(_SYSTEM_DIRS)
    prefix = Path(sys.base_prefix)
    if prefix.is_dir() and not any(prefix.is_relative_to(d) for d in (*bound, *_HIDDEN_DIRS)):
        bound.append(str(prefix))
    for d in bound:  # symlinks (merged /usr) are recreated, not followed
        p = Path(d)
        if p.is_symlink():
            a += ["--symlink", os.readlink(p), d]
        elif p.is_dir():
            a += ["--ro-bind", d, d]
    for d in _HIDDEN_DIRS:
        a += ["--tmpfs", d]
    a += ["--proc", "/proc", "--dev", "/dev", "--bind", str(ws), str(ws)]
    a += ["--clearenv", "--setenv", "HOME", str(ws)]
    safe = {k: env[k] for k in _ENV_ALLOW if k in env}
    safe.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    for k, v in safe.items():
        a += ["--setenv", k, v]
    return Wrapped([*a, "--chdir", str(ws), "--", "/bin/sh", "-c", command])


def seatbelt_profile(sb: Sandbox, ws: Path, home: Path) -> str:
    def q(p: Path | str) -> str:
        s = str(p)
        if '"' in s or "\\" in s or "\n" in s:
            raise SandboxError(f"Path not allowed in a sandbox profile: {s!r}")
        return f'"{s}"'

    allowed = " ".join(f"(subpath {q(p)})" for p in [ws, *(home / t for t in _MAC_TOOLCHAINS)])
    denied = " ".join(f"(subpath {q(p)})" for p in dict.fromkeys([home, CONFIG_DIR]))
    return "\n".join([
        "(version 1)",
        "(allow default)",
        "(allow network*)" if sb.network else "(deny network*)",
        "(deny file-write*)",
        f'(allow file-write* (subpath {q(ws)}) (subpath "/private/tmp") (subpath "/private/var/folders")'
        ' (literal "/dev/null") (literal "/dev/tty"))',
        f"(deny file-read* {denied})",
        f"(allow file-read* {allowed})",
    ])


def _container(sb: Sandbox, command: str, ws: Path) -> Wrapped:
    if "," in str(ws):
        raise SandboxError("A workspace path with a comma cannot be mounted in a container.")
    name = f"micropaw-{uuid.uuid4().hex[:12]}"
    a = [sb.kind, "run", "--rm", "-i", "--name", name, "--network", "bridge" if sb.network else "none",
         "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
         "--pids-limit", "256", "--memory", "1g", "--mount", f"type=bind,src={ws},dst=/work", "-w", "/work",
         "--tmpfs", "/tmp", "-e", "HOME=/tmp"]
    if os.name != "nt":
        a += ["--user", f"{os.getuid()}:{os.getgid()}"]
    return Wrapped([*a, sb.image, "sh", "-c", command], None, [sb.kind, "rm", "-f", name])


def wrap(sb: Sandbox, command: str, workspace: Path, env: dict[str, str], home: Path | None = None) -> Wrapped:
    ws = workspace.resolve()
    home = (home or Path.home()).resolve()
    _check_workspace(ws, home)
    if sb.kind == "bwrap":
        return _bwrap(sb, command, ws, env)
    if sb.kind == "seatbelt":
        return Wrapped(["sandbox-exec", "-p", seatbelt_profile(sb, ws, home), "/bin/sh", "-c", command], env)
    if sb.kind in ("docker", "podman"):
        return _container(sb, command, ws)
    raise SandboxError(f"No sandbox of kind {sb.kind}")


def failure(sb: Sandbox, returncode: int | None, stderr: str) -> str:
    """The reason when the sandbox itself failed to start the command, else ''."""
    first = stderr.strip().splitlines()[0] if stderr.strip() else ""
    if sb.kind == "bwrap" and returncode and first.startswith("bwrap:"):
        return first
    if sb.kind == "seatbelt" and returncode and first.startswith("sandbox-exec:"):
        return first
    if sb.kind in ("docker", "podman") and returncode == 125:
        return first or f"{sb.kind} could not start the container"
    return ""


async def prepare(sb: Sandbox) -> str:
    """Pull the container image once, outside the command timeout. Returns a note for the result."""
    if sb.kind not in ("docker", "podman") or sb.image in _pulled:
        return ""

    async def run(*argv: str, timeout: float) -> int:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            return await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            proc.kill()
            return -1

    if await run(sb.kind, "image", "inspect", sb.image, timeout=30) == 0:
        _pulled.add(sb.image)
        return ""
    if await run(sb.kind, "pull", sb.image, timeout=PULL_TIMEOUT) != 0:
        raise SandboxError(f"Could not pull the sandbox image {sb.image}.")
    _pulled.add(sb.image)
    return f"(preparing sandbox image: pulled {sb.image})\n"
