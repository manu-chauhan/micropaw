"""Console entry point. Reads the privacy mode and installs the egress guard before the rest of MicroPaw loads."""

from __future__ import annotations

import os
import sys


def _file_mode() -> str:
    """privacy.mode as written in config.json (load_config also folds in MICROPAW_PRIVACY, which is per run)."""
    import json

    from micropaw import privacy
    from micropaw.config import CONFIG_FILE
    try:
        mode = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("privacy", {}).get("mode", "normal")
    except (OSError, ValueError, AttributeError):
        return "normal"
    return privacy.parse_mode(mode) or "normal"  # a misspelt mode raises: it must not mean "no guard"


def prepare(install_guard: bool = True) -> str:
    """Freeze the mode, set the environment libraries read at import time, and install the guard.
    Returns the mode. Exits with a plain message when air-gapped or local mode cannot be enforced."""
    if os.name != "nt":
        os.umask(0o077)  # files MicroPaw creates are private to this user
    from micropaw import privacy
    try:
        file_mode = _file_mode()
        privacy.env_mode()
    except privacy.PrivacyError as e:
        sys.exit(str(e))
    try:
        from micropaw.config import load_config
        config = load_config()
    except Exception:
        config = None  # the command itself reports a broken config; the environment can still tighten the mode
    mode, note = privacy.remembered(file_mode)  # what the file says, not the environment
    if config is not None:
        note = "\n".join(n for n in (note, privacy.hold_loosened(config, mode)) if n)  # before the guard reads it
    if note:
        print(note, file=sys.stderr)
    mode = privacy.freeze(mode)  # the environment may tighten it, never loosen
    privacy.boot_env(mode)
    if install_guard and mode != "normal":
        if config is None:
            sys.exit(f"privacy.mode is {mode}, but the config cannot be read. Not starting.")
        if why := privacy.ensure_guard(config):
            sys.exit(f"privacy.mode is {mode}, but the network guard is not working: {why}. Not starting.")
    elif config is not None:
        privacy.bind_config(config)
    return mode


def main() -> None:
    prepare()
    from micropaw.cli import main as cli_main
    cli_main()
