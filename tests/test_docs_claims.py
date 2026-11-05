"""The README and PYPI_README keep to what the code does: no old privacy claim, real settings, real commands."""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

from micropaw.cli import main
from micropaw.config import Config, DisplayConfig, PrivacyConfig, SearchConfig

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "PYPI_README.md"]


@pytest.fixture(params=DOCS, ids=lambda p: p.name)
def text(request) -> str:
    return request.param.read_text(encoding="utf-8")


@pytest.mark.parametrize("claim", [
    "No data leaves your machine", "never phones home", "MicroPaw never phones", "Zero data collection",
    "phones home, ever", "nothing phones home, ever",
])
def test_the_old_privacy_claims_are_gone(text, claim):
    assert claim.lower() not in text.lower()


def test_every_privacy_and_search_setting_named_in_the_docs_exists(text):
    for name in set(re.findall(r"`privacy\.(\w+)`", text)):
        assert name in PrivacyConfig.model_fields, f"privacy.{name} is in the docs but not in PrivacyConfig"
    for name in set(re.findall(r"`search\.(\w+)`", text)):
        assert name in SearchConfig.model_fields, f"search.{name} is in the docs but not in SearchConfig"
    for name in set(re.findall(r"`display\.(\w+)`", text)):
        assert name in DisplayConfig.model_fields


def _command(path: list[str]):
    cmd: click.Command = main
    for part in path:
        cmd = cmd.commands[part]  # type: ignore[attr-defined]
    return cmd


def test_every_paw_command_and_flag_the_docs_show_exists(text):
    seen = set()
    for m in re.finditer(r"paw (models|docs) (\w+)([^`\n|]*)", text):
        group, sub, rest = m.groups()
        seen.add((group, sub))
        cmd = _command([group, sub])
        flags = {o for p in cmd.params for o in getattr(p, "opts", [])}
        for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]*", rest):
            assert flag in flags, f"paw {group} {sub} {flag} is in the docs but not in the CLI"
    assert {("models", "add"), ("models", "remove"), ("models", "scan"), ("models", "test"), ("docs", "add")} <= seen
    for m in re.finditer(r"paw (privacy|doctor)([^`\n|]*)", text):
        cmd = _command([m.group(1)])
        flags = {o for p in cmd.params for o in getattr(p, "opts", [])}
        for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]*", m.group(2)):
            assert flag in flags


def test_the_example_config_in_the_models_section_loads(text):
    import json
    block = re.search(r'```json\n(\{\n  "providers".*?\n\})\n```', text, re.S)
    assert block
    cfg = json.loads(block.group(1))
    config = Config(**cfg)
    assert config.providers["desktop"].base_url == "http://192.168.1.50:11434"
    assert config.agents["main"].model == "desktop:qwen3.6:27b"
