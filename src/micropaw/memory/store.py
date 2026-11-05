"""SQLite persistence layer with sqlite-vec accelerated vector search."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiosqlite

from micropaw.memory.chunker import chunk_text
from micropaw.memory.embeddings import (
    EmbeddingProvider,
    NullEmbedding,
    cosine_similarity,
    deserialize_embedding,
    deserialize_embedding_blob,
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


class MemoryStore:
    def __init__(self, db_path: str | Path, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None
        self.embedder: EmbeddingProvider = embedding_provider or NullEmbedding()
        self._compact_locks: dict[str, asyncio.Lock] = {}  # per-conversation compaction locks
        self._has_vec: bool = False
        self._vec_dims: int = 0

    async def init(self) -> None:
        """Initialize database, load sqlite-vec, create/sync vector index."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; one fsync less per commit
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(_SCHEMA)
        await self._migrate()
        await self._db.commit()

        self._has_vec = await self._try_load_vec()

        if self._has_vec and self.embedder.dimensions > 0:
            await self._ensure_vec_table()

        logger.info(
            "Database initialized at %s (embeddings: %s, sqlite-vec: %s)",
            self.db_path, self.embedder.name, self._has_vec,
        )

    async def _migrate(self) -> None:
        """Add columns that older databases don't have."""
        for table in ("conversations", "memories"):
            async with self.db.execute(f"PRAGMA table_info({table})") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            if "marked" not in columns:
                await self.db.execute(f"ALTER TABLE {table} ADD COLUMN marked INTEGER DEFAULT 0")

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
            f"embedding float[{dims}] distance_metric=cosine"
            f")"
        )
        await self._set_meta("vec_dims", str(dims))
        await self.db.commit()
        self._vec_dims = dims

        asyncio.create_task(self._sync_vec_index())

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

            # Re-embed chunks cleared by dimension change
            async with self.db.execute(
                "SELECT id, content FROM document_chunks WHERE embedding IS NULL"
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

    async def export_messages(self, conversation_id: str) -> list[dict]:
        """Export all messages for a conversation as plain dicts."""
        async with self.db.execute(
            "SELECT role, content, tool_calls, tool_call_id, created_at "
            "FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {"role": r[0], "content": r[1], "tool_calls": r[2], "tool_call_id": r[3], "created_at": r[4]}
            for r in rows
        ]

    # --- Messages ---

    @staticmethod
    def _message_row(conversation_id: str, message: Message) -> tuple:
        tool_calls_json = (
            json.dumps([{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in message.tool_calls])
            if message.tool_calls else None
        )
        return (conversation_id, message.role, message.content, tool_calls_json, message.tool_call_id)

    async def save_message(self, conversation_id: str, message: Message) -> None:
        await self.save_messages(conversation_id, [message])

    async def save_messages(self, conversation_id: str, messages: list[Message]) -> None:
        """Save a turn's messages in one transaction (one commit)."""
        if not messages:
            await self.db.commit()  # still ends the turn's open transaction
            return
        await self.db.executemany(
            "INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id) VALUES (?, ?, ?, ?, ?)",
            [self._message_row(conversation_id, m) for m in messages],
        )
        await self.db.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (conversation_id,),
        )
        await self.db.commit()

    async def get_messages(self, conversation_id: str, limit: int = 50) -> list[Message]:
        async with self.db.execute(
            "SELECT role, content, tool_calls, tool_call_id FROM messages "
            "WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
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

    async def keep_boundary(self, conversation_id: str, keep_recent: int) -> int | None:
        """Id of the oldest of the last `keep_recent` messages (compaction deletes everything before it)."""
        async with self.db.execute(
            "SELECT MIN(id) FROM (SELECT id FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?)",
            (conversation_id, keep_recent),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def compact_messages(
        self, conversation_id: str, summary: str, keep_recent: int, marked: bool = False,
        before_id: int | None = None,
    ) -> None:
        """Delete old messages, keep the last N, and persist the summary.

        `before_id` (from keep_boundary, taken before the summary was written) limits the delete to what
        the summary covers, so messages saved meanwhile survive. `marked` keeps the mark on the summary.
        Uses a per-conversation lock to prevent concurrent compaction from corrupting the message history.
        """
        lock = self._compact_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            await self._compact_messages_inner(conversation_id, summary, keep_recent, marked, before_id)

    async def _compact_messages_inner(
        self, conversation_id: str, summary: str, keep_recent: int, marked: bool, before_id: int | None,
    ) -> None:
        if before_id is None:
            before_id = await self.keep_boundary(conversation_id, keep_recent)
        if before_id is None:
            return

        # Delete all messages older than the kept ones
        await self.db.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND id < ?",
            (conversation_id, before_id),
        )
        # Store summary in kv_meta (avoids ID ordering issues with messages table).
        # get_messages_with_summary() injects it before the kept messages.
        if marked:
            await self._set_meta(f"summary_marked:{conversation_id}", "1")
        await self._set_meta(f"summary:{conversation_id}", summary)
        await self.db.commit()

    async def get_summary(self, conversation_id: str) -> str | None:
        """Get the compaction summary for a conversation, if any."""
        return await self._get_meta(f"summary:{conversation_id}")

    # --- Memories ---

    async def save_memory(
        self, key: str, value: str, tags: list[str] | None = None, user_id: str = "_global", marked: bool = False,
    ) -> None:
        """Save a fact. marked=True when it was written by a conversation that had read outside content."""
        tags_str = ",".join(tags) if tags else ""
        embedding = await self.embedder.embed(f"{key}: {value}")
        embedding_json = serialize_embedding(embedding)

        await self.db.execute(
            "INSERT INTO memories (key, value, tags, embedding, user_id, marked) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key, user_id) DO UPDATE SET value = ?, tags = ?, embedding = ?, marked = ?,"
            " updated_at = CURRENT_TIMESTAMP",
            (key, value, tags_str, embedding_json, user_id, int(marked),
             value, tags_str, embedding_json, int(marked)),
        )
        await self.db.commit()

    async def search_memories(self, query: str, limit: int = 20, user_id: str = "_global") -> list[dict]:
        """Hybrid search: text LIKE + vector similarity. No artificial caps."""
        q = f"%{query.lower()}%"
        async with self.db.execute(
            "SELECT key, value, tags, embedding FROM memories "
            "WHERE user_id = ? AND (LOWER(key) LIKE ? OR LOWER(value) LIKE ? OR LOWER(tags) LIKE ?) "
            "ORDER BY updated_at DESC LIMIT 100",
            (user_id, q, q, q),
        ) as cursor:
            text_rows = await cursor.fetchall()

        text_results = [{"key": r[0], "value": r[1], "tags": r[2], "embedding": r[3]} for r in text_rows]

        if self.embedder.name != "none":
            query_embedding = await self.embedder.embed(query)
            if query_embedding:
                # Scan all memories for this user — no row cap
                async with self.db.execute(
                    "SELECT key, value, tags, embedding FROM memories "
                    "WHERE user_id = ? AND embedding IS NOT NULL",
                    (user_id,),
                ) as cursor:
                    candidates = await cursor.fetchall()

                scored: list[tuple[float, dict]] = []
                seen_keys: set[str] = set()
                for key, value, tags, emb_json in candidates:
                    emb = deserialize_embedding(emb_json)
                    if emb:
                        score = cosine_similarity(query_embedding, emb)
                        scored.append((score, {"key": key, "value": value, "tags": tags}))
                        seen_keys.add(key)

                scored.sort(key=lambda x: x[0], reverse=True)
                results = [item for _, item in scored[:limit]]
                for tr in text_results:
                    if tr["key"] not in seen_keys and len(results) < limit:
                        results.append({"key": tr["key"], "value": tr["value"], "tags": tr["tags"]})
                return results[:limit]

        return [{"key": r["key"], "value": r["value"], "tags": r["tags"]} for r in text_results[:limit]]

    async def get_all_memories(self, user_id: str = "_global") -> list[dict]:
        async with self.db.execute(
            "SELECT key, value, tags, marked FROM memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT 100",
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
        return cursor.rowcount > 0

    async def get_recent_memories(self, limit: int = 10, user_id: str = "_global") -> list[dict]:
        async with self.db.execute(
            "SELECT key, value, marked FROM memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"key": r[0], "value": r[1], "marked": bool(r[2])} for r in rows]

    # --- Document chunks (RAG) ---

    async def ingest_document(self, source: str, content: str, user_id: str = "_global") -> int:
        """Chunk, embed, and store a document. Dual-writes to vec_chunks when available.

        Re-ingesting the same source replaces all previous chunks.
        """
        chunks = chunk_text(content)
        if not chunks:
            return 0

        # Cascade delete: remove from vec index first, then main table
        if self._has_vec:
            async with self.db.execute(
                "SELECT id FROM document_chunks WHERE source = ? AND user_id = ?",
                (source, user_id),
            ) as cursor:
                old_ids = [r[0] for r in await cursor.fetchall()]
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                try:
                    await self.db.execute(
                        f"DELETE FROM vec_chunks WHERE chunk_id IN ({placeholders})", old_ids
                    )
                except Exception:
                    pass

        await self.db.execute(
            "DELETE FROM document_chunks WHERE source = ? AND user_id = ?",
            (source, user_id),
        )

        for i, text in enumerate(chunks):
            embedding = await self.embedder.embed(text)
            blob = serialize_embedding_blob(embedding)
            cursor = await self.db.execute(
                "INSERT INTO document_chunks (source, chunk_index, content, embedding, user_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (source, i, text, blob, user_id),
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

        await self.db.commit()
        logger.info("RAG: ingested %d chunks from %s", len(chunks), source)
        return len(chunks)

    async def search_documents(self, query: str, limit: int = 5, user_id: str = "_global") -> list[dict]:
        """Search document chunks: sqlite-vec ANN → brute-force cosine → text LIKE."""

        # Path 1: sqlite-vec ANN search (fast, O(log n))
        if self._has_vec and self.embedder.name != "none":
            query_embedding = await self.embedder.embed(query)
            if query_embedding:
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
                                f"WHERE id IN ({placeholders}) AND user_id = ?",
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
        if self.embedder.name != "none":
            query_embedding = await self.embedder.embed(query)
            if query_embedding:
                async with self.db.execute(
                    "SELECT source, chunk_index, content, embedding FROM document_chunks "
                    "WHERE user_id = ? AND embedding IS NOT NULL",
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
            "WHERE user_id = ? AND LOWER(content) LIKE ? "
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
            "INSERT INTO contacts (name, email, phone, company, notes) VALUES (?, ?, ?, ?, ?)",
            (name, email, phone, company, notes),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def search_contacts(self, query: str) -> list[dict]:
        q = f"%{query}%"
        async with self.db.execute(
            "SELECT id, name, email, phone, company, notes FROM contacts "
            "WHERE name LIKE ? COLLATE NOCASE OR email LIKE ? COLLATE NOCASE OR company LIKE ? COLLATE NOCASE "
            "ORDER BY name COLLATE NOCASE LIMIT 50",
            (q, q, q),
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._contact_row(r) for r in rows]

    async def get_all_contacts(self, limit: int = 50) -> list[dict]:
        async with self.db.execute(
            "SELECT id, name, email, phone, company, notes FROM contacts ORDER BY name COLLATE NOCASE LIMIT ?",
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
        if self._db:
            await self._db.close()
