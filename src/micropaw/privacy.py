"""Privacy mode: one setting, read once, frozen for the process. Stricter always wins.

Other modules ask `mode()`, `airgapped()`, `local_only()`. The egress guard and the locality
policy live here too (built in round 3).
"""

from __future__ import annotations

import os

_ORDER = {"normal": 0, "local": 1, "airgapped": 2}
_mode: str | None = None


def _strictest(*modes: str) -> str:
    valid = [m for m in modes if m in _ORDER]
    return max(valid, key=_ORDER.__getitem__) if valid else "normal"


def freeze(config_mode: str) -> str:
    """Set the mode for this process once. The MICROPAW_PRIVACY env var can only tighten it."""
    global _mode
    if _mode is None:
        _mode = _strictest(config_mode, os.environ.get("MICROPAW_PRIVACY", "").strip().lower())
    else:
        _mode = _strictest(_mode, config_mode)  # never loosens while running
    return _mode


def mode() -> str:
    if _mode is None:
        from micropaw.config import load_config
        return freeze(load_config().privacy.mode)
    return _mode


def airgapped() -> bool:
    return mode() == "airgapped"


def local_only() -> bool:
    """True in local and airgapped: models, embeddings and speech must be on this machine or the LAN."""
    return _ORDER[mode()] >= 1


def _reset_for_tests() -> None:
    global _mode
    _mode = None
