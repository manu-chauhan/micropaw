"""paw --version and bare paw --help answer without loading the config, the HTTP client or the providers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from micropaw import __version__
from micropaw.cli import main

GOLDEN = Path(__file__).parent / "golden" / "paw_help.txt"


def _importtime(args: list[str], home: Path) -> tuple[subprocess.CompletedProcess, set[str]]:
    env = {**os.environ, "MICROPAW_HOME": str(home)}
    r = subprocess.run([sys.executable, "-X", "importtime", "-m", "micropaw", *args],
                       capture_output=True, text=True, env=env, timeout=60)
    modules = {line.split("|")[-1].strip() for line in r.stderr.splitlines() if line.startswith("import time:")}
    return r, modules


def _loaded(modules: set[str], *prefixes: str) -> list[str]:
    return sorted(m for m in modules if any(m == p or m.startswith(p + ".") for p in prefixes))


def test_version_loads_no_pydantic_httpx_rich_config_or_providers(tmp_path):
    r, modules = _importtime(["--version"], tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == f"MicroPaw, version {__version__}", r.stdout + r.stderr[-300:]
    assert not _loaded(modules, "pydantic", "httpx", "rich", "click", "micropaw.config", "micropaw.providers",
                       "micropaw.cli", "micropaw.privacy")


def test_help_loads_no_pydantic_httpx_rich_or_providers(tmp_path):
    r, modules = _importtime(["--help"], tmp_path)
    assert r.returncode == 0 and "Commands:" in r.stdout, r.stdout + r.stderr[-300:]
    assert not _loaded(modules, "pydantic", "httpx", "rich", "micropaw.providers", "micropaw.core", "micropaw.utils")


def test_a_real_command_still_sets_up_privacy(tmp_path):
    r, modules = _importtime(["status"], tmp_path)  # goes through prepare(), which reads the config
    assert "micropaw.config" in modules and "micropaw.privacy" in modules, r.stderr[-300:]


def test_fast_version_text_is_what_click_prints():
    assert CliRunner().invoke(main, ["--version"], prog_name="paw").output == f"MicroPaw, version {__version__}\n"


def test_help_text_is_the_golden_file():
    out = CliRunner().invoke(main, ["--help"], prog_name="paw", terminal_width=80).output
    assert out == GOLDEN.read_text(encoding="utf-8"), "paw --help changed; if meant, rewrite tests/golden/paw_help.txt"

