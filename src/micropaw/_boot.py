"""Console entry point. Reads the privacy mode and installs the egress guard before the rest of MicroPaw loads."""

from __future__ import annotations

import os
import sys


def prepare(install_guard: bool = True) -> str:
    """Freeze the mode, set the environment libraries read at import time, and install the guard.
    Returns the mode. Exits with a plain message when air-gapped or local mode cannot be enforced."""
    if os.name != "nt":
        os.umask(0o077)  # files MicroPaw creates are private to this user
    from micropaw import privacy
    try:
        from micropaw.config import load_config
        config = load_config()
    except Exception:
        config = None  # the command itself reports a broken config; the environment can still tighten the mode
    mode = privacy.freeze(config.privacy.mode if config else "normal")
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
