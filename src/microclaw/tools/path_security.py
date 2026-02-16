"""Shared path validation for file-access tools."""

from __future__ import annotations

from pathlib import Path

from microclaw.config import CONFIG_DIR

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
    "/.ssh/", "/.gnupg/", "/.aws/", "/.config/gcloud/",
    "/.docker/", "/.kube/",
)

SENSITIVE_FILES = (".env", ".netrc", ".pgpass")

_CONFIG_DIR = str(CONFIG_DIR)


def validate_file_path(path_str: str) -> str | None:
    """Validate a file path for security. Returns error string if blocked, None if safe."""
    path = Path(path_str).resolve()
    resolved = str(path)

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
        return "Access denied — the MicroClaw config directory contains secrets."

    for pattern in SENSITIVE_DIRS:
        if pattern in resolved:
            return f"Access denied — {pattern.strip('/')} contains sensitive data."

    name = path.name
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
    resolved = str(path)

    login_files = (
        ".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile",
        ".bash_login", ".bash_logout",
    )
    if path.name in login_files:
        return f"Access denied — writing to {path.name} is blocked (login script)."

    if path.name == "authorized_keys":
        return "Access denied — writing to authorized_keys is blocked."

    if "/cron" in resolved or resolved.startswith("/etc/cron"):
        return "Access denied — writing to cron directories is blocked."

    if "/autostart/" in resolved:
        return "Access denied — writing to autostart directories is blocked."

    return None
