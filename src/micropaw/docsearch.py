"""Keyword search over the document chunks (SQLite FTS5), so your own files answer a search with no network."""

from __future__ import annotations

import codecs
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 10 * 1024 * 1024
SUFFIXES = (".txt", ".md", ".pdf")

_TRIGGERS = (
    ("ai", "AFTER INSERT ON document_chunks",
     "INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);"),
    ("ad", "AFTER DELETE ON document_chunks",
     "INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);"),
    ("au", "AFTER UPDATE OF content ON document_chunks",
     "INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content); "
     "INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);"),
)


async def ensure_index(db) -> bool:
    """Create chunks_fts over document_chunks (external content, kept current by triggers), once.
    False when this SQLite has no FTS5."""
    async with db.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks_fts'") as cur:
        if await cur.fetchone():
            return True
    try:
        await db.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(content, content='document_chunks', "
                         "content_rowid='id')")
    except Exception as e:  # no FTS5 in this SQLite build
        logger.debug("chunks_fts not available: %s", e)
        return False
    try:  # FTS5 keeps the words of deleted rows in its index until a merge, unless told to wipe them
        await db.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES('secure-delete', 1)")
    except Exception as e:  # SQLite older than 3.42: retention and forget merge or drop the index instead
        logger.debug("FTS5 secure-delete is not available: %s", e)
    for name, when, body in _TRIGGERS:
        await db.execute(f"CREATE TRIGGER IF NOT EXISTS chunks_fts_{name} {when} BEGIN {body} END")
    await db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")  # index what is already there
    await db.commit()
    return True


async def search(store, query: str, limit: int = 5, users: tuple[str, ...] = ("_global",),
                 include_local: bool = True) -> list[dict]:
    """[{source, snippet}] best first. Falls back to the store's own search when FTS5 is missing.
    include_local=False leaves out what was indexed in local or air-gapped mode."""
    if not await ensure_index(store.db):
        return [{"source": r["source"], "snippet": r["content"][:300]}
                for u in users for r in await store.search_documents(query, limit, u, include_local)][:limit]
    fts = store.fts_query(query)
    if not fts:
        return []
    marks = ",".join("?" * len(users))
    async with store.db.execute(
        "SELECT c.source, snippet(chunks_fts, 0, '[', ']', '...', 32) FROM chunks_fts "
        "JOIN document_chunks c ON c.id = chunks_fts.rowid "
        f"WHERE chunks_fts MATCH ? AND c.user_id IN ({marks}){'' if include_local else ' AND c.local_only = 0'} "
        "ORDER BY rank LIMIT ?",
        (fts, *users, limit),
    ) as cur:
        return [{"source": s, "snippet": text} for s, text in await cur.fetchall()]


async def has_documents(store, users: tuple[str, ...] = ("_global",), include_local: bool = True) -> bool:
    marks = ",".join("?" * len(users))
    only = "" if include_local else " AND local_only = 0"
    sql = f"SELECT 1 FROM document_chunks WHERE user_id IN ({marks}){only} LIMIT 1"
    async with store.db.execute(sql, users) as cur:
        return await cur.fetchone() is not None


def read_file(path: Path) -> str:
    """Text of a .txt, .md or .pdf file. Raises ValueError with a plain reason."""
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("PDF files need the pdf extra: paw extras add pdf") from None
        try:
            return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        except Exception:  # pypdf has its own errors for broken, encrypted and odd files
            raise ValueError("could not read this PDF") from None
    data = path.read_bytes()
    boms = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)  # Notepad's "Unicode" text
    text = data.decode("utf-16" if data.startswith(boms) else "utf-8-sig", errors="replace")
    if "\x00" in text:
        raise ValueError("this is not a text file this tool can read")
    return text


def find_files(folder: Path) -> list[Path]:
    from micropaw.tools.path_security import validate_file_path
    return sorted(p for p in folder.rglob("*")
                  if p.is_file() and not p.is_symlink() and p.suffix.lower() in SUFFIXES
                  and not validate_file_path(str(p)))
