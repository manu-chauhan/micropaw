"""MicroPaw — Your AI. Your machine. Your keys."""

import sys

# httpx imports a command-line tool (click, rich, pygments) that MicroPaw never uses: 5 MB and 60 ms.
# Blocking the module makes httpx fall back to its own no-op main(). The `httpx` command runs in its own process.
sys.modules.setdefault("httpx._main", None)  # type: ignore[arg-type]

__version__ = "0.4.0"
