"""Data at rest: secure deletion, retention, `paw forget`, owner-only files."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from micropaw import cli, config, privacy, storage
from micropaw import service as svc
from micropaw.config import Config
from micropaw.gateway import Gateway
from micropaw.memory.embeddings import NullEmbedding
from micropaw.memory.store import MemoryStore
from micropaw.providers.base import Message

CANARY = "CANARY-5d41402abc4b"


class Vec4(NullEmbedding):
    name, dimensions = "fake", 4

    async def embed(self, text):
        h = hash(text) & 0xFFFFFFFF
        return [((h >> (i * 8)) & 0xFF) / 255.0 + 0.01 for i in range(4)]


@pytest.fixture(autouse=True)
def _normal_mode():
    privacy._reset_for_tests()
    privacy.freeze("normal")
    old_umask = os.umask(0o022)  # harden_home sets 077 for the whole process: put it back after each test
    yield
    os.umask(old_umask)
    privacy._reset_for_tests()


def _raw(db: Path) -> bytes:
    return b"".join(p.read_bytes() for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"))
                    if p.exists())


async def _fill(db: Path, embedder=None) -> MemoryStore:
    store = MemoryStore(db, embedder or NullEmbedding())
    await store.init()
    await store.get_or_create_conversation("dm:owner", "dm", "owner")  # the agent does this when it loads history
    await store.save_messages("dm:owner", [Message(role="user", content=f"my number is {CANARY}"),
                                           Message(role="assistant", content=f"noted {CANARY}")])
    await store.save_memory("secret", f"value {CANARY}")
    await store.ingest_document("/tmp/notes.txt", f"document text {CANARY} " * 20, "_global")
    await store.compact_messages("dm:owner", f"summary {CANARY}", keep_recent=1)
    await store.add_contact(f"Name {CANARY}", email="a@b.example")
    return store


async def test_forget_all_leaves_no_canary_in_the_database_or_its_log(tmp_path):
    db = tmp_path / "data.db"
    store = await _fill(db)
    assert CANARY.encode() in _raw(db)  # it was really written
    await store.close()
    assert storage.forget_all(db, tmp_path) == []
    assert CANARY.encode() not in _raw(db)
    con = sqlite3.connect(db)
    assert all(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
               for t in ("messages", "memories", "document_chunks", "contacts", "conversations"))
    assert con.execute("SELECT COUNT(*) FROM kv_meta WHERE key LIKE 'summary%'").fetchone()[0] == 0
    con.close()


async def test_forget_all_clears_the_vector_index_too(tmp_path):
    pytest.importorskip("sqlite_vec")
    db = tmp_path / "data.db"
    store = await _fill(db, Vec4())
    assert store._has_vec
    await store.close()
    assert storage.forget_all(db, tmp_path) == []
    con = sqlite3.connect(db)
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_chunks'").fetchone() is None
    con.close()
    store = MemoryStore(db, Vec4())  # and a new start rebuilds it
    await store.init()
    assert store._has_vec
    await store.close()


async def test_forget_all_removes_audit_outputs_and_log_rotations(tmp_path):
    (tmp_path / "audit.jsonl").write_text(f'{{"approval": "{CANARY}"}}\n')
    (tmp_path / "audit.jsonl.1").write_text(CANARY)
    (tmp_path / "outputs" / "dm_owner").mkdir(parents=True)
    (tmp_path / "outputs" / "dm_owner" / "1.txt").write_text(CANARY)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "gateway.log").write_text(CANARY)
    (tmp_path / "logs" / "gateway.log.1").write_text(CANARY)
    (tmp_path / "config.json").write_text("{}")
    assert storage.forget_all(tmp_path / "data.db", tmp_path) == []
    assert not list(tmp_path.glob("audit.jsonl*")) and not (tmp_path / "outputs").exists()
    assert not list((tmp_path / "logs").glob("*")) and (tmp_path / "config.json").exists()


async def test_deleting_a_memory_overwrites_it_and_empties_the_log(tmp_path):
    db = tmp_path / "data.db"
    store = MemoryStore(db)
    await store.init()
    await store.save_memory("k", f"value {CANARY}")
    assert await store.delete_memory("k")
    assert db.with_name("data.db-wal").stat().st_size == 0  # checkpointed and truncated
    await store.close()
    assert CANARY.encode() not in _raw(db)


async def test_secure_delete_is_what_removes_the_text(tmp_path):
    for flag, found in ((True, False), (False, True)):
        db = tmp_path / f"{flag}.db"
        store = MemoryStore(db, secure_delete=flag)
        await store.init()
        for i in range(30):
            await store.save_memory(f"k{i}", f"value {CANARY} {i}")
        await store.delete_memory("k5")
        await store.db.execute("DELETE FROM memories WHERE key = 'k6'")
        await store.db.commit()
        await store.close()
        assert (f"{CANARY} 5".encode() in _raw(db)) is found


# --- retention ---

async def test_retention_removes_old_rows_and_keeps_new_ones_and_memories(tmp_path):
    db = tmp_path / "data.db"
    store = await _fill(db, Vec4())
    await store.save_messages("dm:owner", [Message(role="user", content="fresh message")])
    old = "datetime('now', '-40 days')"
    await store.db.execute(f"UPDATE messages SET created_at = {old} WHERE content LIKE '%{CANARY}%'")
    await store.db.execute(f"UPDATE document_chunks SET created_at = {old}")
    await store.db.execute(f"UPDATE conversations SET updated_at = {old}")
    await store.db.execute("CREATE TABLE IF NOT EXISTS email_seen (job_id INT, msg_id TEXT, ts REAL)")
    await store.db.execute("CREATE TABLE IF NOT EXISTS email_runs (job_id INT, ts REAL)")
    import time
    await store.db.execute("INSERT INTO email_seen VALUES (1, 'old', ?)", (time.time() - 40 * 86400,))
    await store.db.execute("INSERT INTO email_seen VALUES (1, 'new', ?)", (time.time(),))
    await store.db.execute("INSERT INTO email_runs VALUES (1, ?)", (time.time() - 40 * 86400,))
    await store.db.commit()
    counts = await store.purge_older_than(30)
    assert counts["messages"] == 1 and counts["chunks"] > 0 and counts["summaries"] == 1
    assert counts["email_seen"] == 1 and counts["email_runs"] == 1
    assert [m.content for m in await store.get_messages("dm:owner", 10)] == ["fresh message"]
    assert await store.get_summary("dm:owner") is None
    assert [m["key"] for m in await store.get_all_memories()] == ["secret"]  # memories stay until /forget
    assert len(await store.search_messages(CANARY)) == 0  # and the full-text rows went with the messages
    if store._has_vec:
        async with store.db.execute("SELECT COUNT(*) FROM vec_chunks") as cur:
            assert (await cur.fetchone())[0] == 0
    async with store.db.execute("SELECT COUNT(*) FROM email_seen") as cur:
        assert (await cur.fetchone())[0] == 1
    await store.close()
    assert db.with_name("data.db-wal").stat().st_size == 0 if db.with_name("data.db-wal").exists() else True


async def test_vacuum_runs_only_when_a_tenth_of_the_file_is_free(tmp_path):
    store = MemoryStore(tmp_path / "data.db")
    await store.init()
    await asyncio.gather(*store._tasks)  # the first-start index of old messages must be done
    real, ran = store._db, []

    class Spy:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, sql, *args):
            if sql.strip().upper() == "VACUUM":
                ran.append(1)
            return real.execute(sql, *args)

    store._db = Spy()
    try:
        await store.compact_storage()
        assert not ran
        for i in range(300):
            await store.save_memory(f"k{i}", "x" * 2000)
        await store.db.execute("DELETE FROM memories")
        await store.db.commit()
        await store.compact_storage()
        assert ran
    finally:
        store._db = real
        await store.close()


async def test_the_gateway_sweeps_now_and_daily(monkeypatch):
    gw = Gateway(Config(), MagicMock(), MagicMock(), [])
    gw.store = MagicMock()
    calls = []

    async def purge(days):
        calls.append(days)
        return {"messages": 1}

    gw.store.purge_older_than = purge
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr("micropaw.gateway.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await gw._retention_loop(30)
    assert calls == [30] and sleeps == [86400]


def test_retention_is_off_by_default():
    assert Config().storage.retention_days == 0


# --- files ---

@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
async def test_files_are_owner_only_after_hardening(tmp_path):
    old_umask = os.umask(0o022)
    try:
        (tmp_path / "logs").mkdir()
        for name in ("data.db", "data.db-wal", "audit.jsonl", ".env", "google_tokens.json", "logs/gateway.log"):
            (tmp_path / name).write_text("x")
            (tmp_path / name).chmod(0o644)
        (tmp_path / "logs").chmod(0o755)
        storage.harden_home(tmp_path)
        for name in ("data.db", "data.db-wal", "audit.jsonl", ".env", "google_tokens.json", "logs/gateway.log"):
            assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600, name
        assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        # a file made after this is private from the start: db, wal and shm included
        store = MemoryStore(tmp_path / "fresh.db")
        await store.init()
        await store.save_memory("k", "v")
        for p in tmp_path.glob("fresh.db*"):
            assert stat.S_IMODE(p.stat().st_mode) == 0o600, p.name
        await store.close()
    finally:
        os.umask(old_umask)


def test_old_config_backups_are_deleted_after_a_start(tmp_path):
    (tmp_path / "config.json.v0.bak").write_text("{}")
    (tmp_path / "config.json").write_text("{}")
    storage.drop_config_backups(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


# --- the commands ---

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_FILE", tmp_path / "data.db")
    monkeypatch.setattr(config, "DB_FILE", tmp_path / "data.db")
    return tmp_path


def test_forget_all_refuses_while_the_gateway_runs(home, monkeypatch):
    (home / "data.db").write_text("keep")
    monkeypatch.setattr(svc, "running_gateway", lambda: 4242)
    result = CliRunner().invoke(cli.main, ["forget", "--all", "--yes"])
    assert result.exit_code != 0 and "running" in result.output and (home / "data.db").read_text() == "keep"


def test_forget_all_asks_first_and_then_clears(home, monkeypatch):
    monkeypatch.setattr(svc, "running_gateway", lambda: None)

    async def make():
        s = await _fill(home / "data.db")
        await s.close()
    asyncio.run(make())
    declined = CliRunner().invoke(cli.main, ["forget", "--all"], input="n\n")
    assert declined.exit_code != 0 and CANARY.encode() in _raw(home / "data.db")
    done = CliRunner().invoke(cli.main, ["forget", "--all", "--yes"])
    assert done.exit_code == 0 and "Done" in done.output
    assert CANARY.encode() not in _raw(home / "data.db")


def test_forget_older_than_runs_one_sweep(home):
    async def make():
        s = await _fill(home / "data.db")
        await s.db.execute("UPDATE messages SET created_at = datetime('now', '-90 days')")
        await s.db.commit()
        await s.close()
    asyncio.run(make())
    result = CliRunner().invoke(cli.main, ["forget", "--older-than", "30d"])
    assert result.exit_code == 0 and "1 messages" in result.output


def test_forget_needs_exactly_one_choice(home):
    assert CliRunner().invoke(cli.main, ["forget"]).exit_code != 0
    assert CliRunner().invoke(cli.main, ["forget", "--all", "--older-than", "3d"]).exit_code != 0
    assert CliRunner().invoke(cli.main, ["forget", "--older-than", "soon"]).exit_code != 0


def test_ages_parse():
    assert storage.parse_age("30") == 30 and storage.parse_age("30d") == 30 and storage.parse_age("2w") == 14
    for bad in ("", "0d", "-1d", "1y", "d"):
        with pytest.raises(ValueError):
            storage.parse_age(bad)


def test_reset_says_it_is_not_a_secure_wipe():
    assert "not a secure wipe" in CliRunner().invoke(cli.main, ["reset", "--help"]).output


# --- sharing local history with a cloud model, on purpose ---

async def test_share_history_clears_every_local_flag(tmp_path):
    db = tmp_path / "data.db"
    s = MemoryStore(db)
    await s.init()
    privacy._reset_for_tests()
    privacy.freeze("local")
    await s.save_memory("m", "made locally")
    await s.save_messages("c", [Message(role="user", content="said locally")])
    await s.ingest_document("/tmp/a.txt", "local document text", "u")
    await s.get_or_create_conversation("c", "dm", "u")
    await s.save_messages("c", [Message(role="user", content="more")])
    await s.compact_messages("c", "a summary", keep_recent=1)
    privacy._reset_for_tests()
    privacy.freeze("normal")
    assert await s.get_all_memories(include_local=False) == []
    await s.close()
    counts = storage.share_local_history(db)
    assert counts["memories"] == 1 and counts["document_chunks"] == 1 and counts["summaries"] == 1
    s = MemoryStore(db)
    await s.init()
    assert len(await s.get_all_memories(include_local=False)) == 1
    assert await s.get_summary("c", include_local=False) == "a summary"
    assert await s.search_documents("local", 5, "u", include_local=False)
    await s.close()


def test_models_fetch_is_refused_in_airgapped_mode(home):
    privacy._reset_for_tests()
    privacy.freeze("airgapped")
    result = CliRunner().invoke(cli.main, ["models", "fetch", "embeddings"])
    assert result.exit_code != 0 and "Airgapped mode never downloads" in result.output


def test_models_fetch_runs_in_a_fresh_process_without_the_offline_variable(home, monkeypatch):
    import subprocess
    seen = {}

    def run(cmd, env=None, **kw):
        seen.update(cmd=cmd, env=env)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(subprocess, "run", run)
    result = CliRunner().invoke(cli.main, ["models", "fetch", "embeddings"])
    assert result.exit_code == 0, result.output
    assert "HF_HUB_OFFLINE" not in seen["env"] and seen["cmd"][-1] == "BAAI/bge-small-en-v1.5"
    assert "fetch_local_model" in seen["cmd"][2] and "Copy that folder" in result.output


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_a_database_made_by_forget_is_owner_only(home):
    old_umask = os.umask(0o022)
    try:
        assert CliRunner().invoke(cli.main, ["forget", "--older-than", "30d"]).exit_code == 0
        assert stat.S_IMODE((home / "data.db").stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)
