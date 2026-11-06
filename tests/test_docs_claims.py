"""The README and PYPI_README keep to what the code does: no old privacy claim, real settings, real commands."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import click
import pytest

from micropaw.cli import main
from micropaw.config import Config, DisplayConfig, PrivacyConfig, SearchConfig

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "PYPI_README.md"]


def _fields(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


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
        assert name in _fields(PrivacyConfig), f"privacy.{name} is in the docs but not in PrivacyConfig"
    for name in set(re.findall(r"`search\.(\w+)`", text)):
        assert name in _fields(SearchConfig), f"search.{name} is in the docs but not in SearchConfig"
    for name in set(re.findall(r"`display\.(\w+)`", text)):
        assert name in _fields(DisplayConfig)


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


def test_every_paw_command_named_in_a_code_span_exists(text):
    for name in set(re.findall(r"`paw ([a-z][a-z-]*)", text)):
        assert name in main.commands, f"`paw {name}` is in the docs but not in the CLI"
    setup_flags = {o for p in main.commands["setup"].params for o in getattr(p, "opts", [])}
    for flag in re.findall(r"`paw setup[^`]*?(--[a-z-]+)", text):
        assert flag in setup_flags


def test_settings_and_safeguards_the_docs_name_from_other_branches_exist(text):
    from micropaw.config import EmbeddingConfig
    for name in set(re.findall(r"`embedding\.(\w+)`", text)):
        assert name in _fields(EmbeddingConfig), f"embedding.{name} is in the docs but not in EmbeddingConfig"
    storage = getattr(__import__("micropaw.config", fromlist=["Config"]), "StorageConfig", None)
    for name in set(re.findall(r"`storage\.(\w+)`", text)):
        assert storage and name in _fields(storage), f"storage.{name} is in the docs but not in the config"
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src").rglob("*.py"))
    ci = (ROOT / ".github" / "workflows" / "ci.yml")
    if "connection leaves loopback" in text or "enforced in the process" in text:
        assert "addaudithook" in src and ci.exists() and "canary" in ci.read_text(encoding="utf-8").lower()
    if "link previews off" in text.lower() or "turns link previews off" in text:
        assert "disable_web_page_preview" in src or "link_preview" in src


def test_the_docs_explain_how_to_loosen_a_held_setting_and_what_share_history_shows(text):
    for needle in ("MICROPAW_CONFIRM_PRIVACY=local paw chat", "$env:MICROPAW_CONFIRM_PRIVACY='local'",
                   "set MICROPAW_CONFIRM_PRIVACY=local", "paw privacy allow", "paw privacy share-history",
                   "hidden from cloud models"):
        assert needle in text, needle


def test_ci_builds_images_only_for_platforms_the_uv_image_ships():
    # The Dockerfile copies uv from ghcr.io/astral-sh/uv, which has linux/amd64 and linux/arm64 only.
    assert "COPY --from=ghcr.io/astral-sh/uv:" in (ROOT / "Dockerfile").read_text()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    platforms = set(re.findall(r"platform: (linux/\S+)", ci))
    assert platforms == {"linux/arm64"}
    assert "linux/arm/v7" not in (ROOT / "CHANGELOG.md").read_text()


@pytest.mark.parametrize("name", ["README.md", "PYPI_README.md"])
def test_docs_do_not_blame_a_dependency_the_project_dropped(name):
    assert "pydantic" not in (ROOT / name).read_text()
    core = (ROOT / "pyproject.toml").read_text().split("[project.optional-dependencies]")[0]
    assert "pydantic" not in core


@pytest.mark.parametrize("stale", ["path security", "(bounded queues)"])
def test_the_architecture_block_does_not_claim_what_was_removed(stale):
    assert stale not in (ROOT / "README.md").read_text(encoding="utf-8")
