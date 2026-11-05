"""SQLite persistence layer with sqlite-vec accelerated vector search."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path

import aiosqlite

from micropaw.memory.chunker import chunk_text
from micropaw.memory.embeddings import (
    EmbeddingProvider,
    NullEmbedding,
    cosine_similarity,
    deserialize_embedding,
    deserialize_embedding_blob,
    is_local_embedder,
    serialize_embedding,
    serialize_embedding_blob,
)
from micropaw.providers.base import Message, ToolCall

logger = logging.getLogger(__name__)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT,
    tool_call_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    tags TEXT DEFAULT '',
    embedding TEXT,
    user_id TEXT NOT NULL DEFAULT '_global',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(key, user_id)
);

CREATE TABLE IF NOT EXISTS document_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding BLOB,
    user_id TEXT NOT NULL DEFAULT '_global',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, chunk_index, user_id)
);

CREATE TABLE IF NOT EXISTS kv_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    company TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    local_only INTEGER DEFAULT 0,
    created_at REAL DEFAULT (unixepoch('now')),
    updated_at REAL DEFAULT (unixepoch('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_memories_key ON memories(key);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON document_chunks(source, user_id);
CREATE INDEX IF NOT EXISTS idx_memories_user_updated ON memories(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chunks_user_created ON document_chunks(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name COLLATE NOCASE);
"""


def _local_now() -> int:
    """1 when data written now is made in local or airgapped mode: a cloud model must never be sent it later."""
    from micropaw import privacy
    return int(privacy.local_only())


class MemoryStore:
    def __init__(
        self, db_path: str | Path, embedding_provider: EmbeddingProvider | None = None,
        *, secure_delete: bool = True, max_chunks: int = 0,
    ) -> None:
        self.db_path = str(db_path)
        self.secure_delete = secure_delete  # deleted rows are overwritten with zeros, not left in free pages
        self.max_chunks = max_chunks  # most document chunks kept per user, oldest go first; 0 = no limit
        self._db: aiosqlite.Connection | None = None
        self.embedder: EmbeddingProvider = embedding_provider or NullEmbedding()
        self._compact_locks: dict[str, asyncio.Lock] = {}  # per-conversation compaction locks
        self._ingest_lock = asyncio.Lock()
        self._embed_slot = asyncio.Semaphore(1)  # one ingest embeds at a time, so it never competes with a chat model
        self._tasks: set[asyncio.Task] = set()  # background work; asyncio keeps only weak references
        self._has_vec: bool = False
        self._vec_dims: int = 0
        self.has_fts: bool = False
        self._fts_task: asyncio.Task | None = None

    async def init(self) -> None:
        """Initialize database, load sqlite-vec, create/sync vector index."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; one fsync less per commit
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute(f"PRAGMA secure_delete={'ON' if self.secure_delete else 'OFF'}")
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()
        await self._ensure_fts()

        # No embeddings, no vector index: skip loading the extension (also keeps boards with no wheel quiet)
        self._has_vec = self.embedder.dimensions > 0 and await self._try_load_vec()

        if self._has_vec:
            await self._ensure_vec_table()

        model = getattr(self.embedder, "model_name", None) if self.embedder.name == "local" else None
        if model and await self._get_meta("embedding_model") != f"local:{model}":
            await self._set_meta("embedding_model", f"local:{model}")  # a restart keeps the model the vectors use

        logger.info(
            "Database initialized at %s (embeddings: %s, sqlite-vec: %s)",
            self.db_path, self.embedder.name, self._has_vec,
        )

    async def _migrate(self) -> None:
        """Add columns that older databases don't have."""
        for table, new in (("conversations", ("marked",)), ("memories", ("marked", "local_only")),
                           ("messages", ("local_only",)), ("document_chunks", ("local_only",)),
                           ("contacts", ("local_only",))):
            async with self.db.execute(f"PRAGMA table_info({table})") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            for column in new:
                if column not in columns:
                    await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER DEFAULT 0")

    async def _ensure_fts(self) -> None:
        """Full-text index over what people and the assistant said (not tool output), kept in step by triggers.

        An existing database is indexed in the background, a batch at a time, so start never waits for it."""
        try:
            async with self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'") as cur:
                is_new = await cur.fetchone() is None
            if is_new:
                await self.db.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content)")
            for name, event, row, body in (
                ("ai", "INSERT", "new", "INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content)"),
                ("ad", "DELETE", "old", "DELETE FROM messages_fts WHERE rowid = old.id"),
            ):
                await self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS messages_fts_{name} AFTER {event} ON messages "
                    f"WHEN {row}.role IN ('user', 'assistant') AND {row}.content != '' BEGIN {body}; END"
                )
            try:  # FTS5 keeps the words of deleted rows in its index until a merge, unless told to wipe them
                await self.db.execute("INSERT INTO messages_fts(messages_fts, rank) VALUES('secure-delete', ?)",
                                      (int(self.secure_delete),))
            except Exception as e:  # SQLite older than 3.42: retention and forget merge the index instead
                logger.debug("FTS5 secure-delete is not available: %s", e)
            if is_new:
                async with self.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages") as cur:
                    top = (await cur.fetchone())[0]
                await self._set_meta("fts_backfill_to", str(top))
                await self._set_meta("fts_backfill_at", "0")
            await self.db.commit()
        except Exception as e:  # SQLite built without FTS5
            logger.warning("Message search is off: %s", e)
            return
        self.has_fts = True
        self._fts_task = self.spawn(self._backfill_fts())

    async def _backfill_fts(self, batch: int = 2000) -> None:
        try:
            at = int(await self._get_meta("fts_backfill_at") or 0)
            top = int(await self._get_meta("fts_backfill_to") or 0)
            while at < top:
                end = min(at + batch, top)
                await self.db.execute("DELETE FROM messages_fts WHERE rowid > ? AND rowid <= ?", (at, end))
                await self.db.execute(
                    "INSERT INTO messages_fts(rowid, content) SELECT id, content FROM messages "
                    "WHERE id > ? AND id <= ? AND role IN ('user', 'assistant') AND content != ''", (at, end))
                at = end
                await self._set_meta("fts_backfill_at", str(at))
                await asyncio.sleep(0)  # let a turn run between batches
        except Exception as e:
            logger.warning("Indexing old messages for search failed: %s", e)

    @staticmethod
    def fts_query(text: str) -> str:
        """Plain words, each quoted and ANDed, so nothing the user typed is read as MATCH syntax."""
        clean = ("".join(c for c in w if c.isprintable())[:64] for w in text.split())
        return " ".join('"' + w.replace('"', '""') + '"' for w in [w for w in clean if re.search(r"\w", w)][:12])

    async def search_messages(
        self, text: str, scope: str = "", limit: int = 10, include_local: bool = True,
    ) -> list[dict]:
        """Best matches first. `scope` is an agent prefix ("agent:coder:"); "" means the shared history only,
        so one agent never reads another's conversations."""
        query = self.fts_query(text)
        if not (self.has_fts and query):
            return []
        where = "substr(m.conversation_id, 1, ?) = ?" if scope else "substr(m.conversation_id, 1, 6) != ?"
        args = (query, len(scope), scope) if scope else (query, "agent:")
        if not include_local:
            where += " AND m.local_only = 0"
        async with self.db.execute(
            "SELECT m.conversation_id, m.role, m.created_at, snippet(messages_fts, 0, '[', ']', '...', 16) "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            f"WHERE messages_fts MATCH ? AND {where} ORDER BY rank LIMIT ?",
            (*args, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [{"conversation": r[0], "role": r[1], "when": r[2], "snippet": r[3]} for r in rows]

    async def is_marked(self, conversation_id: str) -> bool:
        """True once this conversation has read outside content (web, email, files, commands).

        A summary made from marked messages keeps the mark too, even if the conversation row is reset.
        """
        async with self.db.execute("SELECT marked FROM conversations WHERE id = ?", (conversation_id,)) as cur:
            row = await cur.fetchone()
        return bool(row and row[0]) or await self._get_meta(f"summary_marked:{conversation_id}") is not None

    async def set_marked(self, conversation_id: str) -> None:
        await self.db.execute("UPDATE conversations SET marked = 1 WHERE id = ?", (conversation_id,))
        await self.db.commit()

    async def _try_load_vec(self) -> bool:
        """Load the sqlite-vec extension. Falls back gracefully if unavailable."""
        try:
            import sqlite_vec
        except ImportError:
            logger.info("sqlite-vec not installed — falling back to brute-force cosine search")
            return False
        try:
            # Must run on aiosqlite's background thread (where _conn was created)
            def _load(conn):
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)

            await self._db._execute(_load, self._db._conn)
            return True
        except Exception as e:
            logger.warning("Failed to load sqlite-vec: %s", e)
            return False

    async def _try_load_vec_table(self) -> bool:
        """True when the database has a vector table and the extension to reach it loaded."""
        async with self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_chunks'") as cur:
            if await cur.fetchone() is None:
                return False
        return await self._try_load_vec()

    async def _get_meta(self, key: str) -> str | None:
        async with self.db.execute("SELECT value FROM kv_meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def _set_meta(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = ?",
            (key, value, value),
        )
        await self.db.commit()

    async def _ensure_vec_table(self) -> None:
        """Create or recreate vec_chunks for current embedding dimensions."""
        dims = self.embedder.dimensions
        stored = await self._get_meta("vec_dims")

        if stored and int(stored) == dims:
            self._vec_dims = dims
            return

        if stored and int(stored) != dims:
            logger.warning(
                "Embedding dimensions changed (%s → %d) — clearing embeddings for re-index",
                stored, dims,
            )
            await self.db.execute("UPDATE document_chunks SET embedding = NULL")
            await self.db.execute("UPDATE memories SET embedding = NULL")
            try:
                await self.db.execute("DROP TABLE IF EXISTS vec_chunks")
            except Exception:
                pass

        await self.db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
            f"chunk_id INTEGER PRIMARY KEY, "
            f"embedding float[{dims}] distance_metric=cosine, chunk_size=64"  # small tables stay small
            f")"
        )
        await self._set_meta("vec_dims", str(dims))
        await self.db.commit()
        self._vec_dims = dims

        self.spawn(self._sync_vec_index())

    def spawn(self, coro) -> asyncio.Task:
        """Run background work with a reference kept, and cancel it in close()."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _sync_vec_index(self) -> None:
        """Populate vec_chunks from existing document_chunks (background)."""
        try:
            # Insert existing embeddings into vec index
            async with self.db.execute(
                "SELECT id, embedding FROM document_chunks WHERE embedding IS NOT NULL"
            ) as cursor:
                rows = await cursor.fetchall()

            count = 0
            for chunk_id, emb_blob in rows:
                if emb_blob and len(emb_blob) // 4 == self._vec_dims:
                    try:
                        await self.db.execute(
                            "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)",
                            (chunk_id, emb_blob),
                        )
                        count += 1
                    except Exception:
                        pass  # Already indexed or dimension mismatch

            if count:
                await self.db.commit()
                logger.info("Synced %d existing chunks to vec index", count)

            # Re-embed chunks cleared by dimension change. Text saved in local mode never goes to an embedder
            # that is not local: those chunks stay with keyword search only.
            local = await asyncio.to_thread(is_local_embedder, self.embedder)
            async with self.db.execute(
                "SELECT id, content FROM document_chunks WHERE embedding IS NULL"
                + ("" if local else " AND local_only = 0")
            ) as cursor:
                null_rows = await cursor.fetchall()

            if null_rows:
                logger.info("Re-embedding %d chunks with new model...", len(null_rows))
                for chunk_id, content in null_rows:
                    try:
                        embedding = await self.embedder.embed(content)
                        blob = serialize_embedding_blob(embedding)
                        if blob:
                            await self.db.execute(
                                "UPDATE document_chunks SET embedding = ? WHERE id = ?",
                                (blob, chunk_id),
                            )
                            await self.db.execute(
                                "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)",
                                (chunk_id, blob),
                            )
                    except Exception as e:
                        logger.error("Re-embed chunk %d: %s", chunk_id, e)
                await self.db.commit()
                logger.info("Re-embedding complete")

        except Exception as e:
            logger.error("Vec index sync failed: %s", e)

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not initialized. Call init() first."
        return self._db

    # --- Conversations ---

    async def get_or_create_conversation(self, conv_id: str, platform: str, user_id: str) -> str:
        """Create the row on first use. An existing row is left alone (save_messages bumps updated_at),
        so a normal turn opens no write transaction before the model answers."""
        async with self.db.execute("SELECT id FROM conversations WHERE id = ?", (conv_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            await self.db.execute(
                "INSERT INTO conversations (id, platform, user_id) VALUES (?, ?, ?)",
                (conv_id, platform, user_id),
            )
            await self.db.commit()
        return conv_id

    async def list_conversations(self) -> list[dict]:
        """Return all conversations with metadata."""
        async with self.db.execute(
            "SELECT id, platform, user_id, created_at, updated_at "
            "FROM conversations ORDER BY updated_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {"id": r[0], "platform": r[1], "user_id": r[2], "created_at": r[3], "updated_at": r[4]}
            for r in rows
        ]

    async def export_messages(self, conversation_id: str, include_local: bool = True) -> list[dict]:
        """Export all messages for a conversation as plain dicts."""
        async with self.db.execute(
            "SELECT role, content, tool_calls, tool_call_id, created_at "
            f"FROM messages WHERE conversation_id = ?{'' if include_local else ' AND local_only = 0'} ORDER BY id ASC",
            (conversation_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {"role": r[0], "content": r[1], "tool_calls": r[2], "tool_call_id": r[3], "created_at": r[4]}
            for r in rows
        ]

    # --- Messages ---

    @staticmethod
    def _message_row(conversation_id: str, message: Message, local: int = 0) -> tuple:
        tool_calls_json = (
            json.dumps([{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in message.tool_calls])
            if message.tool_calls else None
        )
        return (conversation_id, message.role, message.content, tool_calls_json, message.tool_call_id, local)

    async def save_message(self, conversation_id: str, message: Message, local: bool = False) -> None:
        await self.save_messages(conversation_id, [message], local)

    async def has_local_data(self, conversation_id: str, user_id: str) -> bool:
        """True when anything saved in local or airgapped mode could reach a model in this conversation:
        its messages, the user's memories, document pieces and contacts, a summary, a scheduled job."""
        for sql, args in (
            ("SELECT 1 FROM messages WHERE conversation_id = ? AND local_only = 1 LIMIT 1", (conversation_id,)),
            ("SELECT 1 FROM memories WHERE user_id = ? AND local_only = 1 LIMIT 1", (user_id,)),
            ("SELECT 1 FROM document_chunks WHERE user_id = ? AND local_only = 1 LIMIT 1", (user_id,)),
            ("SELECT 1 FROM contacts WHERE local_only = 1 LIMIT 1", ()),
            ("SELECT 1 FROM kv_meta WHERE key = ? LIMIT 1", (f"summary_local:{conversation_id}",)),
            ("SELECT 1 FROM cron_jobs WHERE local_only = 1 LIMIT 1", ()),
        ):
            try:
                async with self.db.execute(sql, args) as cursor:
                    if await cursor.fetchone():
                        return True
            except aiosqlite.OperationalError:
                pass  # the scheduler has not made its table in this database
        return False

    async def save_messages(self, conversation_id: str, messages: list[Message], local: bool = False) -> None:
        """Save a turn's messages in one transaction (one commit). local=True flags them local_only
        whatever the mode: the turn's model saw local data, so its words may repeat it."""
        if not messages:
            await self.db.commit()  # still ends the turn's open transaction
            return
        local = int(local) or _local_now()
        await self.db.executemany(
            "INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id, local_only) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [self._message_row(conversation_id, m, local) for m in messages],
        )
        await self.db.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (conversation_id,),
        )
        await self.db.commit()

    async def get_messages(self, conversation_id: str, limit: int = 50, include_local: bool = True) -> list[Message]:
        """Newest `limit` messages, oldest first. include_local=False leaves out what was said in local or
        airgapped mode, for a model that is not on this side."""
        async with self.db.execute(
            "SELECT role, content, tool_calls, tool_call_id FROM messages "
            f"WHERE conversation_id = ?{'' if include_local else ' AND local_only = 0'} ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()

        messages = []
        for role, content, tool_calls_json, tool_call_id in reversed(rows):
            tool_calls = []
            if tool_calls_json:
                try:
                    for tc in json.loads(tool_calls_json):
                        tool_calls.append(ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"]))
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    logger.warning("Corrupt tool_calls JSON in conversation %s: %s", conversation_id, e)
            messages.append(Message(role=role, content=content, tool_calls=tool_calls, tool_call_id=tool_call_id))
        return messages

    async def keep_boundary(self, conversation_id: str, keep_recent: int, include_local: bool = True) -> int | None:
        """Id of the oldest of the last `keep_recent` messages (compaction deletes everything before it).
        include_local=False counts only what a model that is not local was shown."""
        async with self.db.execute(
            "SELECT MIN(id) FROM (SELECT id FROM messages WHERE conversation_id = ?"
            f"{'' if include_local else ' AND local_only = 0'} ORDER BY id DESC LIMIT ?)",
            (conversation_id, keep_recent),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def compact_messages(
        self, conversation_id: str, summary: str, keep_recent: int, marked: bool = False,
        before_id: int | None = None, include_local: bool = True,
    ) -> None:
        """Delete old messages, keep the last N, and persist the summary.

        include_local=False is a summary made by a model that is not local: it never saw local or airgapped
        talk, so only the other messages go, and it is kept apart from a summary of local talk.

        `before_id` (from keep_boundary, taken before the summary was written) limits the delete to what
        the summary covers, so messages saved meanwhile survive. `marked` keeps the mark on the summary.
        Uses a per-conversation lock to prevent concurrent compaction from corrupting the message history.
        """
        lock = self._compact_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            await self._compact_messages_inner(conversation_id, summary, keep_recent, marked, before_id, include_local)

    async def _compact_messages_inner(
        self, conversation_id: str, summary: str, keep_recent: int, marked: bool, before_id: int | None,
        include_local: bool = True,
    ) -> None:
        if before_id is None:
            before_id = await self.keep_boundary(conversation_id, keep_recent, include_local)
        if before_id is None:
            return
        local_summary = False
        if include_local:
            async with self.db.execute(
                "SELECT 1 FROM messages WHERE conversation_id = ? AND id < ? AND local_only = 1 LIMIT 1",
                (conversation_id, before_id),
            ) as cursor:
                local_summary = _local_now() == 1 or await cursor.fetchone() is not None

        # Delete the messages older than the kept ones (a summary made without the local ones leaves them)
        await self.db.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND id < ?"
            f"{'' if include_local else ' AND local_only = 0'}",
            (conversation_id, before_id),
        )
        # Store summary in kv_meta (avoids ID ordering issues with messages table).
        # get_messages_with_summary() injects it before the kept messages.
        if marked:
            await self._set_meta(f"summary_marked:{conversation_id}", "1")
        if local_summary:
            await self._set_meta(f"summary_local:{conversation_id}", "1")  # sticky: it holds what the old one held
        key = f"summary:{conversation_id}"
        if not include_local and await self._get_meta(f"summary_local:{conversation_id}") is not None:
            key = f"summary_cloud:{conversation_id}"  # the summary of local talk stays as it is
        elif include_local:  # this one was made from the other, so it holds it
            await self.db.execute("DELETE FROM kv_meta WHERE key = ?", (f"summary_cloud:{conversation_id}",))
        await self._set_meta(key, summary)
        await self.db.commit()

    async def checkpoint(self) -> None:
        """Fold the write-ahead log into the database and empty it, so deleted text is not left in -wal."""
        await self.db.commit()
        try:
            await self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except aiosqlite.Error as e:  # a background task is reading on this connection: the next one will do it
            logger.debug("Checkpoint skipped: %s", e)

    async def compact_storage(self) -> None:
        """After a big delete: empty the log, and give the space back when over a tenth of the file is free
        (VACUUM rewrites the whole file, which costs a Pi a few seconds per 100 MB)."""
        await self.checkpoint()
        async with self.db.execute("PRAGMA freelist_count") as cur:
            free = (await cur.fetchone())[0]
        async with self.db.execute("PRAGMA page_count") as cur:
            pages = (await cur.fetchone())[0]
        if pages and free / pages > 0.10:
            try:
                await self.db.execute("VACUUM")
            except aiosqlite.Error as e:  # another task has a transaction open: the next sweep will do it
                logger.debug("VACUUM skipped: %s", e)

    async def purge_older_than(self, days: int) -> dict[str, int]:
        """Delete messages, summaries, document chunks and email records older than `days`.
        Memories and contacts stay: only the owner removes those (/forget)."""
        age = f"-{days} days"
        counts: dict[str, int] = {}
        cur = await self.db.execute("DELETE FROM messages WHERE created_at < datetime('now', ?)", (age,))
        counts["messages"] = cur.rowcount  # the full-text index follows through its trigger
        # A summary is as old as its conversation's last message
        cur = await self.db.execute(
            "DELETE FROM kv_meta WHERE key LIKE 'summary%:%' AND substr(key, instr(key, ':') + 1) IN "
            "(SELECT id FROM conversations WHERE updated_at < datetime('now', ?))", (age,))
        counts["summaries"] = cur.rowcount
        async with self.db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE created_at < datetime('now', ?)", (age,),
        ) as cur2:
            counts["chunks"] = (await cur2.fetchone())[0]
        await self._delete_chunks("created_at < datetime('now', ?)", (age,))
        cutoff = time.time() - days * 86400
        for table in ("email_seen", "email_runs"):
            try:
                cur = await self.db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                counts[table] = cur.rowcount
            except aiosqlite.Error:
                pass  # the scheduler has not made its tables yet
        await self.db.commit()
        if self.has_fts:
            try:
                # merge the index, so old words do not stay in blocks of their own
                await self.db.execute("INSERT INTO messages_fts(messages_fts) VALUES('optimize')")
                await self.db.commit()
            except aiosqlite.Error as e:
                logger.debug("Search index merge skipped: %s", e)
        await self.compact_storage()
        return counts

    async def get_summary(self, conversation_id: str, include_local: bool = True) -> str | None:
        """Get the compaction summary for a conversation, if any. A summary that holds local or airgapped
        talk is withheld when include_local is False (a summary made since by a non-local model is not)."""
        cloud = await self._get_meta(f"summary_cloud:{conversation_id}")
        if await self._get_meta(f"summary_local:{conversation_id}") is not None:
            if not include_local:
                return cloud
            return "\n\n".join(t for t in (await self._get_meta(f"summary:{conversation_id}"), cloud) if t) or None
        return await self._get_meta(f"summary:{conversation_id}")

    # --- Memories ---

    async def save_memory(
        self, key: str, value: str, tags: list[str] | None = None, user_id: str = "_global", marked: bool = False,
        local: bool = False,
    ) -> None:
        """Save a fact. marked=True when it was written by a conversation that had read outside content.
        local=True keeps it local_only whatever the mode (the turn's model saw local data)."""
        tags_str = ",".join(tags) if tags else ""
        embedding = await self.embedder.embed(f"{key}: {value}")
        embedding_json = serialize_embedding(embedding)

        local = int(local) or _local_now()
        await self.db.execute(
            "INSERT INTO memories (key, value, tags, embedding, user_id, marked, local_only) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key, user_id) DO UPDATE SET value = ?, tags = ?, embedding = ?, marked = ?,"
            " local_only = ?, updated_at = CURRENT_TIMESTAMP",
            (key, value, tags_str, embedding_json, user_id, int(marked), local,
             value, tags_str, embedding_json, int(marked), local),
        )
        await self.db.commit()

    async def search_memories(
        self, query: str, limit: int = 20, user_id: str = "_global", include_local: bool = True,
    ) -> list[dict]:
        """Hybrid search: text LIKE + vector similarity. No artificial caps."""
        q = f"%{query.lower()}%"
        only = "" if include_local else " AND local_only = 0"
        async with self.db.execute(
            "SELECT key, value, tags FROM memories "
            f"WHERE user_id = ?{only} AND (LOWER(key) LIKE ? OR LOWER(value) LIKE ? OR LOWER(tags) LIKE ?) "
            "ORDER BY updated_at DESC LIMIT 100",
            (user_id, q, q, q),
        ) as cursor:
            text_rows = await cursor.fetchall()

        text_results = [{"key": r[0], "value": r[1], "tags": r[2]} for r in text_rows]

        if self.embedder.name != "none":
            query_embedding = await self.embedder.embed(query)
            if query_embedding:
                # Scan all memories for this user, but rank off the event loop: parsing and scoring thousands
                # of vectors in Python takes seconds on a small board
                async with self.db.execute(
                    f"SELECT key, value, tags, embedding FROM memories WHERE user_id = ?{only} "
                    "AND embedding IS NOT NULL",
                    (user_id,),
                ) as cursor:
                    candidates = await cursor.fetchall()
                results, seen_keys = await asyncio.to_thread(_rank_memories, query_embedding, candidates, limit)
                for tr in text_results:
                    if tr["key"] not in seen_keys and len(results) < limit:
                        results.append(tr)
                return results[:limit]

        return text_results[:limit]

    async def get_all_memories(self, user_id: str = "_global", include_local: bool = True) -> list[dict]:
        async with self.db.execute(
            "SELECT key, value, tags, marked FROM memories "
            f"WHERE user_id = ?{'' if include_local else ' AND local_only = 0'} ORDER BY updated_at DESC LIMIT 100",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"key": r[0], "value": r[1], "tags": r[2], "marked": bool(r[3])} for r in rows]

    async def any_marked_memory(self, keys: list[str], user_id: str = "_global") -> bool:
        """True if any of these memories was saved by a conversation that had read outside content."""
        if not keys:
            return False
        marks = ",".join("?" * len(keys))
        async with self.db.execute(
            f"SELECT 1 FROM memories WHERE user_id = ? AND marked = 1 AND key IN ({marks}) LIMIT 1",
            (user_id, *keys),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def delete_memory(self, key: str, user_id: str = "_global") -> bool:
        """Delete a memory. Returns True if a row was deleted."""
        cursor = await self.db.execute("DELETE FROM memories WHERE key = ? AND user_id = ?", (key, user_id))
        await self.db.commit()
        if cursor.rowcount > 0:
            await self.checkpoint()  # the old text must not stay behind in -wal
        return cursor.rowcount > 0

    async def get_recent_memories(
        self, limit: int = 10, user_id: str = "_global", include_local: bool = True,
    ) -> list[dict]:
        async with self.db.execute(
            "SELECT key, value, marked FROM memories "
            f"WHERE user_id = ?{'' if include_local else ' AND local_only = 0'} ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"key": r[0], "value": r[1], "marked": bool(r[2])} for r in rows]

    # --- Document chunks (RAG) ---

    async def ingest_document(
        self, source: str, content: str, user_id: str = "_global", max_chunks: int = 0, local: bool = False,
    ) -> int:
        """Chunk, embed, and store a document. Dual-writes to vec_chunks when available.

        Re-ingesting the same source replaces all previous chunks. max_chunks > 0 keeps only the first
        that many chunks of this document. With self.max_chunks set, the oldest chunks of the user go first.
        """
        chunks = chunk_text(content)
        if max_chunks > 0:
            chunks = chunks[:max_chunks]
        if not chunks:
            return 0

        # Embed everything first (one network call per chunk), so the write below is short and never half-done.
        # One ingest at a time: two big documents must not both load the embedding model next to the chat model.
        async with self._embed_slot:
            blobs = [serialize_embedding_blob(await self.embedder.embed(text)) for text in chunks]

        local = int(local) or _local_now()
        async with self._ingest_lock:  # two ingests of one source must not interleave
            try:
                await self._delete_chunks("source = ? AND user_id = ?", (source, user_id))
                for i, (text, blob) in enumerate(zip(chunks, blobs)):
                    cursor = await self.db.execute(
                        "INSERT INTO document_chunks (source, chunk_index, content, embedding, user_id, local_only) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (source, i, text, blob, user_id, local),
                    )
                    chunk_id = cursor.lastrowid

                    # Dual-write to sqlite-vec index
                    if self._has_vec and blob and chunk_id:
                        try:
                            await self.db.execute(
                                "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)",
                                (chunk_id, blob),
                            )
                        except Exception as e:
                            logger.debug("vec insert for chunk %d: %s", chunk_id, e)

                if self.max_chunks > 0:
                    await self._delete_chunks(
                        "id IN (SELECT id FROM document_chunks WHERE user_id = ? ORDER BY created_at ASC, id ASC "
                        "LIMIT max(0, (SELECT COUNT(*) FROM document_chunks WHERE user_id = ?) - ?))",
                        (user_id, user_id, self.max_chunks),
                    )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
        logger.info("RAG: ingested %d chunks from %s", len(chunks), hashlib.sha256(source.encode()).hexdigest()[:8])
        return len(chunks)

    async def _delete_chunks(self, where: str, args: tuple) -> None:
        """Delete document chunks and their vector rows (the caller commits)."""
        if not self._has_vec and await self._try_load_vec_table():
            self._has_vec = True  # a table made while embeddings were on: its rows go too
        if self._has_vec:
            async with self.db.execute(f"SELECT id FROM document_chunks WHERE {where}", args) as cursor:
                old_ids = [r[0] for r in await cursor.fetchall()]
            for i in range(0, len(old_ids), 500):
                batch = old_ids[i:i + 500]
                try:
                    await self.db.execute(
                        f"DELETE FROM vec_chunks WHERE chunk_id IN ({','.join('?' * len(batch))})", batch)
                except Exception:
                    pass
        await self.db.execute(f"DELETE FROM document_chunks WHERE {where}", args)

    async def search_documents(
        self, query: str, limit: int = 5, user_id: str = "_global", include_local: bool = True,
    ) -> list[dict]:
        """Search document chunks: sqlite-vec ANN, then brute-force cosine, then text LIKE."""
        only = "" if include_local else " AND local_only = 0"
        # Nothing stored for this user: skip the embedding call (a network trip or a model load per message)
        async with self.db.execute(
            f"SELECT 1 FROM document_chunks WHERE user_id = ?{only} LIMIT 1", (user_id,),
        ) as cursor:
            if await cursor.fetchone() is None:
                return []

        # One embedding of the query serves both vector paths
        query_embedding = await self.embedder.embed(query) if self.embedder.name != "none" else []

        # Path 1: sqlite-vec ANN search (fast, O(log n))
        if self._has_vec and query_embedding:
            query_blob = serialize_embedding_blob(query_embedding)
            if query_blob:
                try:
                    # Over-fetch to allow post-filtering by user_id
                    async with self.db.execute(
                        "SELECT chunk_id, distance FROM vec_chunks "
                        "WHERE embedding MATCH ? AND k = ?",
                        (query_blob, limit * 5),
                    ) as cursor:
                        vec_rows = await cursor.fetchall()

                    if vec_rows:
                        chunk_ids = [r[0] for r in vec_rows]
                        placeholders = ",".join("?" * len(chunk_ids))
                        async with self.db.execute(
                            f"SELECT id, source, chunk_index, content FROM document_chunks "
                            f"WHERE id IN ({placeholders}) AND user_id = ?{only}",
                            (*chunk_ids, user_id),
                        ) as cursor:
                            rows = await cursor.fetchall()

                        # Preserve vec ordering (nearest first)
                        id_to_row = {r[0]: r for r in rows}
                        results = []
                        for chunk_id, _ in vec_rows:
                            if chunk_id in id_to_row:
                                r = id_to_row[chunk_id]
                                results.append({"source": r[1], "chunk": r[2], "content": r[3]})
                                if len(results) >= limit:
                                    break
                        if results:
                            return results
                except Exception as e:
                    logger.warning("sqlite-vec search failed, falling back to brute-force: %s", e)

        # Path 2: Brute-force cosine similarity (no row cap)
        if query_embedding:
            async with self.db.execute(
                f"SELECT source, chunk_index, content, embedding FROM document_chunks "
                f"WHERE user_id = ?{only} AND embedding IS NOT NULL",
                (user_id,),
            ) as cursor:
                candidates = await cursor.fetchall()

            scored: list[tuple[float, dict]] = []
            for source, idx, content, emb_blob in candidates:
                emb = deserialize_embedding_blob(emb_blob)
                if emb:
                    score = cosine_similarity(query_embedding, emb)
                    scored.append((score, {"source": source, "chunk": idx, "content": content}))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [item for _, item in scored[:limit]]

        # Path 3: Text fallback (no embeddings)
        q = f"%{query.lower()}%"
        async with self.db.execute(
            "SELECT source, chunk_index, content FROM document_chunks "
            f"WHERE user_id = ?{only} AND LOWER(content) LIKE ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, q, limit),
        ) as cursor:
            rows = await cursor.fetchall()

        return [{"source": r[0], "chunk": r[1], "content": r[2]} for r in rows]

    # --- Contacts ---

    @staticmethod
    def _contact_row(r) -> dict:
        return {"id": r[0], "name": r[1], "email": r[2], "phone": r[3], "company": r[4], "notes": r[5]}

    async def add_contact(self, name: str, email: str = "", phone: str = "", company: str = "", notes: str = "") -> int:
        cursor = await self.db.execute(
            "INSERT INTO contacts (name, email, phone, company, notes, local_only) VALUES (?, ?, ?, ?, ?, ?)",
            (name, email, phone, company, notes, _local_now()),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def search_contacts(self, query: str, include_local: bool = True) -> list[dict]:
        q = f"%{query}%"
        async with self.db.execute(
            "SELECT id, name, email, phone, company, notes FROM contacts "
            "WHERE (name LIKE ? COLLATE NOCASE OR email LIKE ? COLLATE NOCASE OR company LIKE ? COLLATE NOCASE)"
            f"{'' if include_local else ' AND local_only = 0'} "
            "ORDER BY name COLLATE NOCASE LIMIT 50",
            (q, q, q),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._contact_row(r) for r in rows]

    async def get_all_contacts(self, limit: int = 50, include_local: bool = True) -> list[dict]:
        async with self.db.execute(
            "SELECT id, name, email, phone, company, notes FROM contacts"
            f"{'' if include_local else ' WHERE local_only = 0'} ORDER BY name COLLATE NOCASE LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._contact_row(r) for r in rows]

    async def update_contact(self, contact_id: int, **fields) -> bool:
        allowed = {"name", "email", "phone", "company", "notes"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return False
        updates["updated_at"] = "strftime('%s','now')"
        sets = ", ".join(f"{k} = ?" for k in updates if k != "updated_at")
        sets += ", updated_at = CAST(strftime('%s','now') AS REAL)"
        vals = [updates[k] for k in updates if k != "updated_at"]
        cursor = await self.db.execute(f"UPDATE contacts SET {sets} WHERE id = ?", (*vals, contact_id))
        await self.db.commit()
        return cursor.rowcount > 0

    async def delete_contact(self, contact_id: int) -> bool:
        cursor = await self.db.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._db:
            await self._db.close()


def _rank_memories(query: list[float], rows: list[tuple], limit: int) -> tuple[list[dict], set[str]]:
    """The `limit` rows closest to the query, and the keys of every row that had a usable vector."""
    scored: list[tuple[float, dict]] = []
    seen: set[str] = set()
    for key, value, tags, emb_json in rows:
        emb = deserialize_embedding(emb_json)
        if emb:
            scored.append((cosine_similarity(query, emb), {"key": key, "value": value, "tags": tags}))
            seen.add(key)
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]], seen
