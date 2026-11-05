"""Data at rest: owner-only files, forgetting everything, and the helpers retention uses.

The database side of retention lives in MemoryStore.purge_older_than; this module holds what works on
files and on a closed database (the `paw forget` command).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_AGE = re.compile(r"^\s*(\d+)\s*([dw]?)\s*$", re.IGNORECASE)

# Every table holding what was said, read or saved. Usage counts and scheduled jobs are not content.
_TABLES = (
    "messages", "conversations", "memories", "document_chunks", "contacts",
    "email_seen", "email_runs", "chat_agent",
)


def parse_age(text: str) -> int:
    """Days from "30", "30d" or "4w". Raises ValueError for anything else."""
    m = _AGE.match(text)
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"'{text}' is not an age. Write it like 30d or 4w.")
    return int(m.group(1)) * (7 if m.group(2).lower() == "w" else 1)


def harden_home(home: Path) -> None:
    """Owner-only from now on (umask 077) and for what is already there: files 0600, folders 0700.
    POSIX only: Windows files take their rights from the folder, and nothing is changed there."""
    if os.name == "nt":
        return
    os.umask(0o077)
    try:
        entries = [home, *home.rglob("*")]
    except OSError:
        return
    for p in entries:
        try:
            if p.is_symlink():
                continue
            p.chmod(0o700 if p.is_dir() else 0o600)
        except OSError:
            pass  # a drive that has no modes (WSL on /mnt, a FAT stick)


def drop_config_backups(home: Path) -> None:
    """config.json.v0.bak and its kind hold keys in an older layout. They are not needed once a start worked."""
    for backup in home.glob("config.json.v*.bak"):
        try:
            backup.unlink()
        except OSError:
            pass


def forget_all(db_file: Path, home: Path) -> list[str]:
    """Delete everything MicroPaw remembers: the rows, the vector index, the audit log, saved outputs and logs.
    The gateway must not be running. Returns what could not be cleared."""
    problems: list[str] = []
    if db_file.exists():
        con = sqlite3.connect(db_file, timeout=10)
        try:
            con.execute("PRAGMA secure_delete=ON")
            vec_ok = _load_vec(con)
            for trigger in ("messages_fts_ai", "messages_fts_ad"):
                con.execute(f"DROP TRIGGER IF EXISTS {trigger}")  # the next start makes them and the index again
            try:
                con.execute("DROP TABLE IF EXISTS messages_fts")  # its words sit in index blocks, not only in rows
            except sqlite3.Error as e:  # SQLite without FTS5 cannot even open the table
                problems.append(f"The search index was not cleared ({e}). It holds the words you wrote.")
            for table in _TABLES:
                try:
                    con.execute(f"DELETE FROM {table}")
                except sqlite3.Error:
                    pass  # an older database without that table
            con.execute("DELETE FROM kv_meta WHERE key LIKE 'summary%' OR key LIKE 'fts_backfill%'")
            if _has_table(con, "vec_chunks"):
                try:
                    if not vec_ok:
                        raise sqlite3.Error("sqlite-vec is not installed")
                    con.execute("DROP TABLE vec_chunks")
                    con.execute("DELETE FROM kv_meta WHERE key = 'vec_dims'")
                except sqlite3.Error as e:
                    problems.append(f"The vector index was not cleared ({e}). It holds numbers made from your text.")
            con.commit()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("VACUUM")
        except sqlite3.Error as e:
            problems.append(f"The database was not fully cleared: {e}")
        finally:
            con.close()
    for pattern in ("audit.jsonl*",):
        for f in home.glob(pattern):
            _remove(f, problems)
    shutil.rmtree(home / "outputs", ignore_errors=True)
    for f in (home / "logs").glob("*.log*") if (home / "logs").is_dir() else []:
        _remove(f, problems)
    return problems


def share_local_history(db_file: Path) -> dict[str, int]:
    """Let a model that is not local see what was said and saved in local or airgapped mode: clear the flag
    on every row and summary. This is the owner's choice (`paw privacy share-history`), never automatic."""
    counts: dict[str, int] = {}
    con = sqlite3.connect(db_file, timeout=10)
    try:
        for table in ("messages", "memories", "document_chunks"):
            cur = con.execute(f"UPDATE {table} SET local_only = 0 WHERE local_only = 1")
            counts[table] = cur.rowcount
        cur = con.execute("DELETE FROM kv_meta WHERE key LIKE 'summary_local:%'")
        counts["summaries"] = cur.rowcount
        con.commit()
    finally:
        con.close()
    return counts


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def _load_vec(con: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        return True
    except (ImportError, AttributeError, sqlite3.Error):
        return False


def _remove(path: Path, problems: list[str]) -> None:
    try:
        path.unlink()
    except OSError:
        try:
            path.write_bytes(b"")  # Windows: a file that is open here cannot be deleted, but can be emptied
        except OSError as e:
            problems.append(f"Could not remove {path.name}: {e}")
