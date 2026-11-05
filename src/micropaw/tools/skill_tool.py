"""Skill tool: list and read skills, and propose a new one for the owner to approve."""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any

from micropaw import skills
from micropaw.tools.base import Tool, ToolContext

# The owner sees all of it in one approval message, so the whole file must fit in one chat message
MAX_PROPOSAL = 3500


class SkillTool(Tool):
    name = "skill"
    description = (
        "Skills are short how-to guides. Actions: 'list' (names and descriptions), "
        "'read' (load one skill by name, and optionally a text file beside it with 'file'), "
        "'propose' (suggest a new skill with name, description and body; the owner must approve it)."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "read", "propose"]},
            "name": {"type": "string", "description": "Skill name: lowercase letters, digits and dashes"},
            "file": {"type": "string", "description": "For read: a text file in the skill folder"},
            "description": {"type": "string", "description": "For propose: one line, at most 160 characters"},
            "body": {"type": "string", "description": "For propose: the instructions, kept short"},
        },
        "required": ["action"],
    }

    @staticmethod
    def _proposal(args: dict[str, Any]) -> tuple[str, str] | str:
        """(name, file text) for a valid proposal, else the reason it is refused."""
        name = str(args.get("name") or "")
        desc = " ".join(str(args.get("description") or "").split())
        body = str(args.get("body") or "").strip()
        if not skills.NAME_RE.fullmatch(name):
            return "The name must be 1 to 40 characters: lowercase letters, digits and dashes."
        if not desc or len(desc) > skills.DESC_CAP:
            return f"The description must be one line of 1 to {skills.DESC_CAP} characters."
        if not body:
            return "The body is empty."
        text = f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}\n"
        if len(text) > MAX_PROPOSAL:
            return f"The proposal is {len(text):,} characters; the limit is {MAX_PROPOSAL:,}. Shorten it."
        if name in skills.discover():
            return f"A skill called {name} already exists."
        return name, text

    def needs_approval(self, args: dict[str, Any], context: ToolContext) -> str | None:
        if args.get("action") != "propose":
            return None
        proposal = self._proposal(args)
        if isinstance(proposal, str):
            return None  # execute() refuses with the reason
        name, text = proposal
        return (f"The agent proposes a new skill called {name}. It will be loaded as outside content. "
                f"Full text:\n\n{text}")

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        action = args.get("action")
        if action == "list":
            return skills.listing()
        if action == "propose":
            return await asyncio.to_thread(self._propose, args)
        if action != "read":
            return f"Unknown action: {action}"
        found = skills.discover().get(str(args.get("name") or ""))
        if found is None:
            return f"No skill called '{args.get('name', '')}'. Use action list."
        if found.origin == "agent":
            context.marked = True  # the agent wrote it, possibly after reading outside content
        try:
            if args.get("file"):
                return await asyncio.to_thread(skills.read_file, found, str(args["file"]))
            body = await asyncio.to_thread(skills.read_body, found)
        except (ValueError, OSError) as e:
            return f"Error: {e}"
        extra = await asyncio.to_thread(skills.files_beside, found)
        if extra:
            body += f"\n\nFiles beside this skill (read with the file argument): {', '.join(extra)}"
        return body

    @staticmethod
    def _propose(args: dict[str, Any]) -> str:
        """Runs only after the owner approved the full text: stage it, then move it into place."""
        proposal = SkillTool._proposal(args)
        if isinstance(proposal, str):
            return f"Error: {proposal}"
        name, text = proposal
        staged = skills.user_dir() / "_proposed" / name
        dest = skills.user_dir() / name
        try:
            shutil.rmtree(staged, ignore_errors=True)
            staged.mkdir(parents=True)
            (staged / "SKILL.md").write_text(text, encoding="utf-8")
            (staged / skills.AGENT_MARK).write_text("proposed by the agent\n", encoding="utf-8")
            os.replace(staged, dest)
        except OSError as e:
            return f"Error: could not save the skill: {e}"
        return f"Skill {name} saved. It loads as outside content because the agent wrote it."
