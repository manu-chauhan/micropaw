"""Shared path validation for file-access tools."""

from __future__ import annotations

import sys
from pathlib import Path

from micropaw.config import CONFIG_DIR

BLOCKED_PREFIXES = ("/proc", "/sys", "/dev", "/boot", "/sbin")

BLOCKED_FILES = (
    "/etc/shadow", "/etc/passwd", "/etc/sudoers", "/etc/gshadow",
    "/etc/master.passwd", "/etc/security/opasswd",
)

# Directories under /etc/ that contain sensitive data
BLOCKED_ETC_DIRS = (
    "/etc/ssl/private", "/etc/ssh/",
    "/etc/cron", "/etc/sudoers.d",
)

SENSITIVE_DIRS = (
    "/.ssh/", "/.gnupg/", "/.aws/", "/.azure/", "/.config/gcloud/", "/.config/gh/",
    "/.docker/", "/.kube/", "/.password-store/", "/.local/share/keyrings/",
)

SENSITIVE_FILES = (
    ".env", ".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc",
    "id_rsa", "id_ecdsa", "id_ed25519", "id_dsa",
)

# Windows and macOS file systems ignore case, so compare lowercased there
_FOLD_CASE = sys.platform in ("win32", "darwin")


def _norm(p: str) -> str:
    """Forward slashes everywhere, lowercased on case-insensitive systems."""
    p = p.replace("\\", "/")
    return p.lower() if _FOLD_CASE else p


_CONFIG_DIR = _norm(str(CONFIG_DIR))


def resolve_path(path_str: str, workspace: str = "") -> Path:
    """Expand ~ and resolve; relative paths are taken from the workspace when there is one."""
    path = Path(path_str).expanduser()
    if workspace and not path.is_absolute():
        path = Path(workspace) / path
    return path.resolve()


def in_workspace(path: Path, workspace: str) -> bool:
    return bool(workspace) and path.is_relative_to(Path(workspace).expanduser().resolve())


def runs_code_later(path: Path) -> bool:
    """Files that tools or the owner will execute later (build files, CI, editor tasks)."""
    norm = _norm(str(path)).lower()
    return path.name.lower() in _EXEC_NAMES or any(d in norm for d in ("/.github/workflows/", "/.vscode/"))


_EXEC_NAMES = (
    "makefile", "justfile", "package.json", "conftest.py", "setup.py", "noxfile.py", "tox.ini",
    ".gitlab-ci.yml", "dockerfile", "docker-compose.yml",
)


def validate_file_path(path_str: str) -> str | None:
    """Validate a file path for security. Returns error string if blocked, None if safe."""
    path = Path(path_str).resolve()
    resolved = _norm(str(path))

    for prefix in BLOCKED_PREFIXES:
        if resolved.startswith(prefix):
            return f"Access denied — {prefix} is blocked."

    for blocked in BLOCKED_FILES:
        if resolved == blocked:
            return f"Access denied — {blocked} is a protected file."

    for etc_dir in BLOCKED_ETC_DIRS:
        if resolved.startswith(etc_dir):
            return f"Access denied — {etc_dir} contains sensitive data."

    if resolved.startswith(_CONFIG_DIR):
        return "Access denied — the MicroPaw config directory contains secrets."

    for pattern in SENSITIVE_DIRS:
        if pattern in resolved:
            return f"Access denied — {pattern.strip('/')} contains sensitive data."

    name = _norm(path.name)
    for sf in SENSITIVE_FILES:
        if name == sf or name.startswith(sf):
            return f"Access denied — {name} may contain secrets."

    raw = Path(path_str)
    if raw.exists() and raw.is_symlink():
        return "Access denied — following symlinks is not allowed."

    return None


def validate_write_path(path_str: str) -> str | None:
    """Extra checks for write operations."""
    base_error = validate_file_path(path_str)
    if base_error:
        return base_error

    path = Path(path_str).resolve()
    resolved = _norm(str(path))
    name = _norm(path.name)

    if name in _LOGIN_FILES:
        return f"Access denied — writing to {path.name} is blocked (login script)."

    if name == "authorized_keys":
        return "Access denied — writing to authorized_keys is blocked."

    if "/cron" in resolved or resolved.startswith("/etc/cron"):
        return "Access denied — writing to cron directories is blocked."

    # Files that run code later: services, startup items, PATH, git hooks, Python startup
    if name.endswith(".pth") or any(d in resolved for d in _EXEC_DIRS):
        return f"Access denied — {path.name} would run code automatically later."

    return None


_LOGIN_FILES = (
    ".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile", ".zshenv", ".zlogin",
    ".bash_login", ".bash_logout", ".bash_aliases", ".kshrc", ".cshrc", ".tcshrc",
)

_EXEC_DIRS = (
    "/autostart/", "/.config/systemd/", "/.config/fish/", "/.local/bin/", "/.git/hooks/",
    "/site-packages/", "/dist-packages/", "/library/launchagents/", "/library/launchdaemons/",
    "/start menu/programs/startup/",
)
