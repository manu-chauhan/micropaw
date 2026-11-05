"""Skills in the agentskills.io layout: a folder with a SKILL.md (front matter name and description, then the body).

The prompt carries only a short index. The `skill` tool loads one body on demand. Only `name` and
`description` are read from the front matter: nothing a skill says can widen permissions.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from micropaw import config

NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
MAX_INDEX = 40
DESC_CAP = 160
FILE_CAP = 64 * 1024
AGENT_MARK = ".agent-origin"  # in the folder of a skill the agent proposed
TEXT_SUFFIXES = frozenset(
    {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".csv", ".py", ".sh", ".js", ".html", ".css"})
BUNDLED_DIR = Path(__file__).resolve().parent / "skills"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    folder: Path
    origin: str  # bundled | owner | agent

    @property
    def file(self) -> Path:
        return self.folder / "SKILL.md"


def user_dir() -> Path:
    return config.CONFIG_DIR / "skills"


def parse(text: str) -> tuple[dict[str, str], str]:
    """(name and description from the front matter, the body). Every other key is ignored."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    key = ""
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:]).strip()
        if line[:1] in (" ", "\t"):  # continuation of the key above (folded or block text)
            if key in ("name", "description") and line.strip():
                meta[key] = f"{meta.get(key, '')} {line.strip()}".strip()
            continue
        k, sep, v = line.partition(":")
        key = k.strip().lower() if sep else ""
        if key in ("name", "description"):
            v = v.strip()
            if v[:1] in (">", "|"):
                v = ""
            meta[key] = v.strip("\"'")
    return {}, text  # never closed: not a skill header


def _clean(text: str) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())


def _load(folder: Path, origin: str) -> Skill | None:
    name = folder.name
    if not NAME_RE.fullmatch(name) or folder.is_symlink() or not folder.is_dir():
        return None
    file = folder / "SKILL.md"
    if file.is_symlink() or not file.is_file():
        return None
    try:
        with file.open(encoding="utf-8", errors="replace") as f:
            meta, _ = parse(f.read(FILE_CAP))
    except OSError:
        return None
    description = _clean(meta.get("description", ""))[:DESC_CAP]
    if meta.get("name") != name or not description:
        return None
    if origin == "owner" and (folder / AGENT_MARK).exists():
        origin = "agent"
    return Skill(name, description, folder, origin)


def _scan(root: Path, origin: str) -> dict[str, Skill]:
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return {}
    found = (_load(Path(e.path), origin) for e in entries)
    return {s.name: s for s in found if s}


# The SKILLS.md that older setups copied into ~/.micropaw unchanged; only an edited copy is worth keeping
_OLD_DEFAULT_SHA256 = "f56d471c175abe4c11fb2c47037087a73718dd30556d9c33a82ef94b995b7807"


def migrate_legacy() -> str | None:
    """Move a hand-edited ~/.micropaw/SKILLS.md (v0.3 and older) into skills/my-notes once. Returns a notice."""
    import hashlib

    old = config.CONFIG_DIR / "SKILLS.md"
    target = user_dir() / "my-notes"
    if not old.is_file() or old.is_symlink() or target.exists():
        return None
    data = old.read_bytes()
    if hashlib.sha256(data).hexdigest() == _OLD_DEFAULT_SHA256:
        old.rename(old.with_name("SKILLS.md.old"))
        return None
    target.mkdir(parents=True)
    head = "---\nname: my-notes\ndescription: Your own workflow notes, moved from SKILLS.md\n---\n\n"
    (target / "SKILL.md").write_text(head + data.decode("utf-8", errors="replace"), encoding="utf-8")
    old.rename(old.with_name("SKILLS.md.old"))
    return "Your SKILLS.md is now the skill 'my-notes' (~/.micropaw/skills/my-notes/SKILL.md)."


def discover() -> dict[str, Skill]:
    """Bundled skills, then the owner's (and approved agent proposals) on top. Sorted by name."""
    found = _scan(BUNDLED_DIR, "bundled") | _scan(user_dir(), "owner")
    return dict(sorted(found.items()))


def _signature() -> tuple:
    def mtime(p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0

    sig = []
    for root in (BUNDLED_DIR, user_dir()):
        try:
            sig += [(e.path, mtime(Path(e.path)), mtime(Path(e.path, "SKILL.md"))) for e in os.scandir(root)]
        except OSError:
            continue
    return tuple(sorted(sig))


_cache: tuple[tuple, str] | None = None


def index_text() -> str:
    """The prompt block: one line per skill, sorted, no dates, so the prompt stays cacheable.

    Rebuilt only when a skill folder changes."""
    global _cache
    sig = _signature()
    if _cache and _cache[0] == sig:
        return _cache[1]
    found = list(discover().values())
    text = ""
    if found:
        lines = [f"- {s.name}: {s.description}" for s in found[:MAX_INDEX]]
        if len(found) > MAX_INDEX:
            lines.append(f"- ...and {len(found) - MAX_INDEX} more (the skill tool, action list, shows all)")
        text = ("Skills. When a request fits one, load it first with the skill tool (action read, name):\n"
                + "\n".join(lines))
    _cache = (sig, text)
    return text


def listing() -> str:
    found = discover()
    if not found:
        return "No skills installed."
    label = {"bundled": "", "owner": " (yours)", "agent": " (proposed by the agent, approved by you)"}
    return "\n".join(f"- {s.name}{label[s.origin]}: {s.description}" for s in found.values())


def read_file(skill: Skill, rel: str) -> str:
    """A text file beside SKILL.md. Raises ValueError with the reason when it is not allowed."""
    path = Path(rel)
    if not rel or path.is_absolute() or ".." in path.parts or "\\" in rel or ":" in rel:
        raise ValueError("The file must be a relative path inside the skill folder.")
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError(f"Only text files can be read ({', '.join(sorted(TEXT_SUFFIXES))}).")
    cur = skill.folder
    for part in path.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError("Symlinks are not followed.")
    if not cur.is_file() or not cur.resolve().is_relative_to(skill.folder.resolve()):
        raise ValueError(f"No such file in skill {skill.name}: {rel}")
    data = cur.read_bytes()[: FILE_CAP + 1]
    text = data[:FILE_CAP].decode("utf-8", errors="replace")
    return text + ("\n... (cut at 64 KB)" if len(data) > FILE_CAP else "")


def read_body(skill: Skill) -> str:
    _, body = parse(read_file(skill, "SKILL.md"))
    return body


def files_beside(skill: Skill) -> list[str]:
    """Readable text files in the skill folder besides SKILL.md (no symlinks), at most 20."""
    out = []
    for dirpath, _, names in os.walk(skill.folder):  # symlinked folders are listed but not entered
        for n in names:
            p = Path(dirpath, n)
            rel = p.relative_to(skill.folder).as_posix()
            if p.suffix.lower() in TEXT_SUFFIXES and rel != "SKILL.md" and not p.is_symlink():
                out.append(rel)
    return sorted(out)[:20]
