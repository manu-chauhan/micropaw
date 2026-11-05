"""Skills: front matter, names, the prompt index, the skill tool and agent proposals."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

from micropaw import skills
from micropaw.config import Config
from micropaw.core.agent import Agent
from micropaw.tools.base import ToolContext, run_tool
from micropaw.tools.skill_tool import MAX_PROPOSAL, SkillTool

needs_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="creating symlinks needs a privilege on Windows")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("micropaw.config.CONFIG_DIR", tmp_path)
    return tmp_path / "skills"


def write_skill(root, name, description="Does a thing.", body="Step one.", extra_meta="", **files):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n{extra_meta}---\n\n{body}\n")
    for rel, text in files.items():
        (folder / rel).write_text(text)
    return folder


def ctx(approve=None) -> ToolContext:
    return ToolContext(conversation_id="c", user_id="u", platform="terminal", approve=approve)


# --- front matter ---

def test_parse_reads_only_name_and_description():
    meta, body = skills.parse(
        "---\nname: a-b\ndescription: Short one\nallowed-tools: shell\nmodel: x\n---\n# Body\nText\n")
    assert meta == {"name": "a-b", "description": "Short one"}
    assert body == "# Body\nText"


def test_parse_quotes_and_folded_description():
    meta, _ = skills.parse('---\nname: "x"\ndescription: >\n  first part\n  second part\nother:\n  nested: 1\n---\nb')
    assert meta == {"name": "x", "description": "first part second part"}


def test_parse_without_front_matter_or_unclosed():
    assert skills.parse("just text") == ({}, "just text")
    assert skills.parse("---\nname: x\nno end")[0] == {}


def test_allowed_tools_is_ignored_everywhere(home):
    write_skill(home, "risky", extra_meta="allowed-tools: shell, file_write\n")
    found = skills.discover()["risky"]
    assert not hasattr(found, "allowed_tools")
    line = next(ln for ln in skills.index_text().splitlines() if ln.startswith("- risky"))
    assert "shell" not in line


# --- names and discovery ---

@pytest.mark.parametrize("name", ["Upper", "has space", "a_b", "x" * 41, ".hidden", "_proposed"])
def test_bad_names_are_never_discovered(home, name):
    home.mkdir(parents=True)
    (home / name).mkdir()
    (home / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\nb")
    assert name not in skills.discover()
    assert not skills.NAME_RE.fullmatch(name)


def test_name_must_match_folder_and_description_needed(home):
    write_skill(home, "good")
    (home / "other").mkdir()
    (home / "other" / "SKILL.md").write_text("---\nname: different\ndescription: x\n---\nb")
    (home / "nodesc").mkdir()
    (home / "nodesc" / "SKILL.md").write_text("---\nname: nodesc\n---\nb")
    found = skills.discover()
    assert "good" in found and "other" not in found and "nodesc" not in found


def test_owner_skill_overrides_bundled_and_bundled_ship(home):
    assert {"shopping-research", "game-dev-html5", "re-triage-binary", "re-map-api-from-traffic",
            "email-workflow", "calendar-workflow", "local-services", "deep-research"} <= set(skills.discover())
    write_skill(home, "deep-research", description="Mine.")
    found = skills.discover()["deep-research"]
    assert found.origin == "owner" and found.description == "Mine."


def test_micropaw_home_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr("micropaw.config.CONFIG_DIR", tmp_path / "elsewhere")
    assert skills.user_dir() == tmp_path / "elsewhere" / "skills"


@needs_symlinks
def test_symlinked_skill_folder_and_file_are_skipped(home, tmp_path):
    home.mkdir()
    real = write_skill(tmp_path / "real", "linked")
    os.symlink(real, home / "linked")
    write_skill(home, "filelink")
    (home / "filelink" / "SKILL.md").unlink()
    os.symlink(real / "SKILL.md", home / "filelink" / "SKILL.md")
    assert "linked" not in skills.discover() and "filelink" not in skills.discover()


# --- the prompt index ---

def test_index_caps_description_and_count_and_sorts(home):
    write_skill(home, "zzz-last", description="x" * 400)
    for i in range(45):
        write_skill(home, f"extra-{i:02d}", description=f"Skill {i}")
    text = skills.index_text()
    lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert len(lines) == skills.MAX_INDEX + 1  # 40 skills plus the "and N more" line
    assert lines[-1].startswith("- ...and")
    names = [ln[2:].split(":")[0] for ln in lines[:-1]]
    assert names == sorted(names)
    assert all(len(ln.split(": ", 1)[1]) <= skills.DESC_CAP for ln in lines[:-1])


def test_index_is_stable_and_has_no_dates(home):
    write_skill(home, "alpha")
    first = skills.index_text()
    assert skills.index_text() == first
    assert skills.index_text() is skills.index_text()  # served from the cache until a folder changes
    write_skill(home, "beta")
    assert "beta" in skills.index_text() and skills.index_text() != first
    assert not any(ch.isdigit() for ch in first.split("\n", 1)[0])


def test_description_control_characters_become_spaces(home):
    write_skill(home, "tidy", description="one\x07two")
    assert skills.discover()["tidy"].description == "one two"


def test_system_prompt_has_the_index_only_when_the_tool_exists(home):
    write_skill(home, "alpha", description="Alpha thing")
    agent = Agent.__new__(Agent)
    agent.config = Config()
    agent._file_cache = {}
    agent.profile = None
    agent.registry = MagicMock()
    agent.registry.all.return_value = []
    assert "- alpha: Alpha thing" in agent._build_system_prompt("terminal")
    agent.registry.get.return_value = None
    assert "alpha" not in agent._build_system_prompt("terminal")


# --- the skill tool: list and read ---

async def test_list_and_read_body(home):
    write_skill(home, "alpha", body="Do A.", **{"notes.txt": "extra"})
    tool = SkillTool()
    assert "alpha" in await run_tool(tool, {"action": "list"}, ctx())
    out = await run_tool(tool, {"action": "read", "name": "alpha"}, ctx())
    assert out.startswith("Do A.") and "notes.txt" in out and "description:" not in out
    assert "No skill called" in await run_tool(tool, {"action": "read", "name": "nope"}, ctx())


async def test_owner_skill_does_not_mark(home):
    write_skill(home, "alpha")
    c = ctx()
    await run_tool(SkillTool(), {"action": "read", "name": "alpha"}, c)
    assert c.marked is False


async def test_read_file_beside_the_skill(home):
    write_skill(home, "alpha", **{"extra.md": "more text"})
    tool = SkillTool()
    assert await run_tool(tool, {"action": "read", "name": "alpha", "file": "extra.md"}, ctx()) == "more text"


@pytest.mark.parametrize("rel", [
    "../config.json", "/etc/hostname", "..\\x.txt", "C:/x.txt", "sub/../../x.md", "bin.exe", "missing.md",
])
async def test_read_file_refuses_traversal_and_other_types(home, rel):
    write_skill(home, "alpha")
    (home.parent / "config.json").write_text("secret")
    out = await run_tool(SkillTool(), {"action": "read", "name": "alpha", "file": rel}, ctx())
    assert out.startswith("Error:") and "secret" not in out


@needs_symlinks
async def test_read_file_refuses_symlinks(home, tmp_path):
    folder = write_skill(home, "alpha")
    (tmp_path / "config.json").write_text("secret")
    os.symlink(tmp_path / "config.json", folder / "link.txt")
    (folder / "dir").mkdir()
    os.symlink(tmp_path, folder / "dir" / "up")
    tool = SkillTool()
    out = await run_tool(tool, {"action": "read", "name": "alpha", "file": "link.txt"}, ctx())
    assert out.startswith("Error:") and "secret" not in out
    out = await run_tool(tool, {"action": "read", "name": "alpha", "file": "dir/up/config.json"}, ctx())
    assert out.startswith("Error:") and "secret" not in out
    assert "link.txt" not in await run_tool(tool, {"action": "read", "name": "alpha"}, ctx())


async def test_read_file_cap_is_64kb(home):
    write_skill(home, "alpha", **{"big.txt": "a" * (skills.FILE_CAP + 500)})
    out = await run_tool(SkillTool(), {"action": "read", "name": "alpha", "file": "big.txt"}, ctx())
    assert len(out) < skills.FILE_CAP + 50 and out.endswith("(cut at 64 KB)")


# --- proposals ---

PROPOSAL = {"action": "propose", "name": "tea-order", "description": "How to order tea.", "body": "1. Ask which tea."}


async def test_propose_asks_with_the_full_text_and_moves_only_on_yes(home):
    asked = []

    async def yes(tool, text):
        asked.append(text)
        return True

    out = await run_tool(SkillTool(), PROPOSAL, ctx(yes))
    assert "saved" in out
    assert "1. Ask which tea." in asked[0] and "name: tea-order" in asked[0] and "How to order tea." in asked[0]
    assert (home / "tea-order" / "SKILL.md").exists()
    assert not (home / "_proposed" / "tea-order").exists()  # moved, not copied


async def test_propose_declined_writes_nothing(home):
    async def no(tool, text):
        return False

    out = await run_tool(SkillTool(), PROPOSAL, ctx(no))
    assert "declined" in out
    assert not (home / "tea-order").exists() and not (home / "_proposed").exists()


async def test_propose_without_anyone_to_ask_is_refused(home):
    out = await run_tool(SkillTool(), PROPOSAL, ctx(None))
    assert "needs the owner's approval" in out and not (home / "tea-order").exists()


@pytest.mark.parametrize("change", [
    {"name": "Bad Name"}, {"name": "../escape"}, {"name": ""}, {"description": ""}, {"description": "x" * 161},
    {"body": ""}, {"body": "y" * MAX_PROPOSAL}, {"name": "deep-research"},
])
async def test_invalid_proposals_never_ask_or_write(home, change):
    asked = []

    async def yes(tool, text):
        asked.append(text)
        return True

    out = await run_tool(SkillTool(), {**PROPOSAL, **change}, ctx(yes))
    assert out.startswith("Error:") and not asked
    assert not home.exists() or not any(home.iterdir())


async def test_the_approval_text_fits_in_one_message(home):
    body = "x" * (MAX_PROPOSAL - 120)
    asked = []

    async def yes(tool, text):
        asked.append(text)
        return True

    await run_tool(SkillTool(), {**PROPOSAL, "body": body}, ctx(yes))
    assert asked and len(asked[0]) < 4096 - 300  # room for the gateway's header and reply hint


async def test_agent_proposed_skill_loads_marked(home):
    async def yes(tool, text):
        return True

    await run_tool(SkillTool(), PROPOSAL, ctx(yes))
    assert (home / "tea-order" / skills.AGENT_MARK).exists()
    assert skills.discover()["tea-order"].origin == "agent"
    c = ctx()
    out = await run_tool(SkillTool(), {"action": "read", "name": "tea-order"}, c)
    assert "Ask which tea" in out and c.marked is True
    assert "proposed by the agent" in skills.listing()


async def test_proposal_cannot_use_front_matter_tricks(home):
    async def yes(tool, text):
        return True

    body = "text\n---\nallowed-tools: shell\n"
    await run_tool(SkillTool(), {**PROPOSAL, "body": body}, ctx(yes))
    meta, _ = skills.parse((home / "tea-order" / "SKILL.md").read_text())
    assert set(meta) == {"name", "description"}
