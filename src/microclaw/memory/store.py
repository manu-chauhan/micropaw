"""SQLite persistence layer with optional vector search."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

from microclaw.memory.embeddings import (
    EmbeddingProvider,
    NullEmbedding,
    cosine_similarity,
    deserialize_embedding,
    serialize_embedding,
)
from microclaw.providers.base import Message, ToolCall

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

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_memories_key ON memories(key);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
"""


class MemoryStore:
    def __init__(self, db_path: str | Path, embedding_provider: EmbeddingProvider | None = None) -> None:
        self.db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None
        self.embedder: EmbeddingProvider = embedding_provider or NullEmbedding()

    async def init(self) -> None:
        """Initialize database and create tables."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("Database initialized at %s (embeddings: %s)", self.db_path, self.embedder.name)

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not initialized. Call init() first."
        return self._db

    # --- Conversations ---

    async def get_or_create_conversation(self, conv_id: str, platform: str, user_id: str) -> str:
        async with self.db.execute("SELECT id FROM conversations WHERE id = ?", (conv_id,)) as cursor:
            row = await cursor.fetchone()

        if not row:
            await self.db.execute(
                "INSERT INTO conversations (id, platform, user_id) VALUES (?, ?, ?)",
                (conv_id, platform, user_id),
            )
        else:
            await self.db.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (conv_id,)
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

    async def save_message(self, conversation_id: str, message: Message) -> None:
        tool_calls_json = (
            json.dumps([{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in message.tool_calls])
            if message.tool_calls else None
        )
        await self.db.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_calls, tool_call_id) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, message.role, message.content, tool_calls_json, message.tool_call_id),
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

    # --- Memories ---

    async def save_memory(self, key: str, value: str, tags: list[str] | None = None, user_id: str = "_global") -> None:
        tags_str = ",".join(tags) if tags else ""
        embedding = await self.embedder.embed(f"{key}: {value}")
        embedding_json = serialize_embedding(embedding)

        await self.db.execute(
            "INSERT INTO memories (key, value, tags, embedding, user_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key, user_id) DO UPDATE SET value = ?, tags = ?, embedding = ?, updated_at = CURRENT_TIMESTAMP",
            (key, value, tags_str, embedding_json, user_id, value, tags_str, embedding_json),
        )
        await self.db.commit()

    async def search_memories(self, query: str, limit: int = 20, user_id: str = "_global") -> list[dict]:
        """Hybrid search: text LIKE + vector similarity. Paginated, not loading ALL."""
        q = f"%{query.lower()}%"
        async with self.db.execute(
            "SELECT key, value, tags, embedding FROM memories "
            "WHERE user_id = ? AND (LOWER(key) LIKE ? OR LOWER(value) LIKE ? OR LOWER(tags) LIKE ?) "
            "ORDER BY updated_at DESC LIMIT 100",
            (user_id, q, q, q),
        ) as cursor:
            text_rows = await cursor.fetchall()

        text_results = [{"key": r[0], "value": r[1], "tags": r[2], "embedding": r[3]} for r in text_rows]

        # Vector search if embeddings are active
        if self.embedder.name != "none":
            query_embedding = await self.embedder.embed(query)
            if query_embedding:
                # Paginated: only load recent 500, not ALL
                async with self.db.execute(
                    "SELECT key, value, tags, embedding FROM memories "
                    "WHERE user_id = ? AND embedding IS NOT NULL "
                    "ORDER BY updated_at DESC LIMIT 500",
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
            "SELECT key, value, tags FROM memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT 100",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"key": r[0], "value": r[1], "tags": r[2]} for r in rows]

    async def delete_memory(self, key: str, user_id: str = "_global") -> bool:
        """Delete a memory. Returns True if a row was deleted."""
        cursor = await self.db.execute("DELETE FROM memories WHERE key = ? AND user_id = ?", (key, user_id))
        await self.db.commit()
        return cursor.rowcount > 0

    async def get_recent_memories(self, limit: int = 10, user_id: str = "_global") -> list[dict]:
        async with self.db.execute(
            "SELECT key, value FROM memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"key": r[0], "value": r[1]} for r in rows]

    async def close(self) -> None:
        if self._db:
            await self._db.close()
