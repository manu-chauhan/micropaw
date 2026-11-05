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


# ---------------------------------------------------------------------------
# Shared interface (round 3). Stream A replaces these bodies with the full policy;
# the signatures stay so other modules can rely on them.
# ---------------------------------------------------------------------------

def is_local_url(url: str) -> bool:
    """True when the URL's host is this machine or (if allowed) the LAN or tailnet."""
    from micropaw.providers.registry import is_local_url as _registry_local
    return _registry_local(url)


def is_local_provider(provider: object) -> bool:
    """True when every model behind this provider (chains and wrappers included) is local."""
    from micropaw.utils.prices import is_local_provider as _prices_local
    return _prices_local(provider)


def check_role(spec: str, role: str) -> str | None:
    """A plain refusal when this model spec is not allowed for the role in the current mode, else None."""
    return None
