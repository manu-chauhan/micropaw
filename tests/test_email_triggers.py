"""Tests for email-triggered jobs, with a fake Gmail client."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from micropaw.scheduler import Scheduler, _authenticated, email_rule
from micropaw.tools.base import ToolContext

OWNER = "boss@example.com"
GOOD = "mx.google.com; dkim=pass header.i=@example.com; spf=pass smtp.mailfrom=example.com; dmarc=pass (p=NONE)"


def _msg(mid, sender=OWNER, subject="Invoice 7", body="Please pay 10 euros", auth=GOOD, extra_headers=()):
    headers = [{"name": "From", "value": f"Boss <{sender}>"}, {"name": "Subject", "value": subject}]
    if auth is not None:
        headers.append({"name": "Authentication-Results", "value": auth})
    headers += [{"name": n, "value": v} for n, v in extra_headers]
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {"id": mid, "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": data}}}


class FakeGmail:
    def __init__(self, *messages):
        self.messages = {m["id"]: m for m in messages}  # newest first
        self.queries: list[str] = []
        self.gets: list[str] = []

    async def search(self, q, limit):
        self.queries.append(q)
        return list(self.messages)[:limit]

    async def get(self, message_id):
        self.gets.append(message_id)
        return self.messages[message_id]


class FakeStore:
    def __init__(self, log):
        self.log = log
        self.marked: set[str] = set()

    async def get_or_create_conversation(self, conv_id, platform, user_id):
        self.log.append(("create", conv_id))

    async def set_marked(self, conv_id):
        self.marked.add(conv_id)
        self.log.append(("mark", conv_id))


@pytest.fixture
async def env(tmp_path):
    log: list = []

    async def handler(text, user_id, platform, channel_id):
        log.append(("run", platform, user_id, channel_id, text))
        return "summary done"

    bus = SimpleNamespace(closed=False, publish_outbound=AsyncMock())
    sched = Scheduler(str(tmp_path / "s.db"), handler, bus, [SimpleNamespace(name="telegram")],
                      email_poll_seconds=300, store=FakeStore(log))
    await sched.init()
    sched.record_activity("telegram", "telegram:1")
    yield SimpleNamespace(sched=sched, log=log, sent=bus.publish_outbound)
    await sched.close()


async def _job(sched, **rule):
    job_id = await sched.add_job("summarize invoices", 0, platform="telegram", chat_id="telegram:1",
                                 enabled=False, match=email_rule(rule.pop("from_", OWNER), **rule))
    return job_id


def test_rule_needs_one_exact_address():
    assert email_rule(" Boss@Example.com ")["from"] == "Boss@Example.com"
    for bad in ("", "example.com", "*@example.com", "a@b", "a b@example.com", "x@example.com, y@example.com"):
        with pytest.raises(ValueError):
            email_rule(bad)
    with pytest.raises(ValueError):
        email_rule(OWNER, label="inbox; drop")
    assert '"' not in email_rule(OWNER, subject_contains='a"b')["subject_contains"]


def test_authentication_needs_gmails_own_dmarc_or_spf_and_dkim():
    def h(value):
        return [{"name": "Authentication-Results", "value": value}]
    assert _authenticated(h(GOOD))
    assert not _authenticated(h("mx.google.com; dmarc=fail; spf=pass; dkim=pass"))
    assert _authenticated(h("mx.google.com; spf=pass; dkim=pass"))
    assert not _authenticated(h("mx.google.com; spf=pass; dkim=fail"))
    assert not _authenticated(h("evil.example.org; dmarc=pass"))  # a header the sender wrote
    assert not _authenticated([])
    # the first mx.google.com result decides; a forged later one cannot rescue a failure
    assert not _authenticated(h("mx.google.com; dmarc=fail") + h("mx.google.com; dmarc=pass"))


@pytest.mark.asyncio
async def test_matching_mail_runs_marked_unattended_and_reports(env):
    s = env.sched
    job_id = await _job(s)
    await s.set_enabled(job_id, True)
    s.gmail = FakeGmail(_msg("m1"))
    await s._poll_email(time.time())
    assert [e[0] for e in env.log] == ["create", "mark", "run"]  # marked before the run starts
    kind, platform, user, channel, text = env.log[2]
    assert (platform, user, channel) == ("system", "system", f"email-{job_id}")  # no approver exists for "system"
    assert env.log[1] == ("mark", f"system:email-{job_id}")
    assert "outside content" in text and "Please pay 10 euros" in text and "summarize invoices" in text
    msg = env.sent.call_args.args[0]
    assert (msg.channel, msg.chat_id) == ("telegram", "telegram:1") and "summary done" in msg.content
    q = s.gmail.queries[0]
    assert f"from:{OWNER}" in q and "in:inbox" in q and "after:" in q


@pytest.mark.asyncio
async def test_last_seen_id_is_stored_and_each_message_runs_once(env):
    s = env.sched
    job_id = await _job(s)
    await s.set_enabled(job_id, True)
    s.gmail = FakeGmail(_msg("m2"), _msg("m1"))
    await s._poll_email(time.time())
    assert [e[3] for e in env.log if e[0] == "run"] == [f"email-{job_id}"] * 2
    assert "Please" in env.log[2][4] and s.gmail.gets == ["m1", "m2"]  # oldest first
    async with s.db.execute("SELECT last_msg_id FROM cron_jobs WHERE id = ?", (job_id,)) as cur:
        assert (await cur.fetchone())[0] == "m2"
    s._last_email_poll = 0
    s.gmail.messages = {"m3": _msg("m3"), **s.gmail.messages}
    s.gmail.gets.clear()
    await s._poll_email(time.time())
    assert s.gmail.gets == ["m3"]  # only the new one
    s._last_email_poll = 0
    s.gmail.gets.clear()
    await s._poll_email(time.time())
    assert s.gmail.gets == []


@pytest.mark.asyncio
async def test_unauthenticated_wrong_sender_and_subject_are_skipped(env, caplog):
    s = env.sched
    job_id = await _job(s, subject_contains="invoice")
    await s.set_enabled(job_id, True)
    s.gmail = FakeGmail(
        _msg("spoof", auth="mx.google.com; dmarc=fail"), _msg("noauth", auth=None),
        _msg("other", sender="eve@example.com"), _msg("subject", subject="Lunch"), _msg("fine", subject="INVOICE 9"),
    )
    with caplog.at_level("INFO"):
        await s._poll_email(time.time())
    runs = [e for e in env.log if e[0] == "run"]
    assert len(runs) == 1 and "INVOICE 9" in runs[0][4]
    assert "DMARC" in caplog.text and "From address" in caplog.text and "subject does not match" in caplog.text
    async with s.db.execute("SELECT last_msg_id FROM cron_jobs") as cur:
        assert (await cur.fetchone())[0] == "spoof"  # skipped mail is consumed, not retried forever


@pytest.mark.asyncio
async def test_the_owners_own_address_still_needs_a_pass(env):
    s = env.sched
    job_id = await _job(s, from_=OWNER)
    await s.set_enabled(job_id, True)
    s.gmail = FakeGmail(_msg("self", sender=OWNER, auth=None))
    await s._poll_email(time.time())
    assert not [e for e in env.log if e[0] == "run"]


@pytest.mark.asyncio
async def test_body_is_capped(env):
    s = env.sched
    job_id = await _job(s)
    await s.set_enabled(job_id, True)
    s.gmail = FakeGmail(_msg("big", body="x" * 50_000))
    await s._poll_email(time.time())
    run = next(e for e in env.log if e[0] == "run")
    assert 20_000 <= len(run[4]) < 21_000


@pytest.mark.asyncio
async def test_run_limits_per_hour_and_per_day(env):
    s = env.sched
    job_id = await _job(s)
    await s.set_enabled(job_id, True)
    s.gmail = FakeGmail(*[_msg(f"m{i}") for i in range(10, 0, -1)])  # ten new messages
    await s._poll_email(time.time())
    assert len([e for e in env.log if e[0] == "run"]) == 6
    # six in the last hour: still waiting, even at the next poll
    s._last_email_poll = 0
    await s._poll_email(time.time())
    assert len([e for e in env.log if e[0] == "run"]) == 6
    # an hour later the rest may run, up to 30 in a day
    await s.db.execute("UPDATE email_runs SET ts = ts - 3700")
    s._last_email_poll = 0
    await s._poll_email(time.time())
    assert len([e for e in env.log if e[0] == "run"]) == 10
    await s.db.execute("DELETE FROM email_runs")
    await s.db.executemany("INSERT INTO email_runs VALUES (?, ?)", [(job_id, time.time() - 4000)] * 30)
    assert await s._room_for_run(job_id) is False


@pytest.mark.asyncio
async def test_no_gmail_call_without_an_enabled_email_job(env):
    s = env.sched
    s.gmail = FakeGmail(_msg("m1"))
    await s._poll_email(time.time())
    job_id = await _job(s)  # paused: the model-made default
    await s._poll_email(time.time())
    assert s.gmail.queries == []
    await s.set_enabled(job_id, True)
    await s._poll_email(time.time())
    assert len(s.gmail.queries) == 1
    await s._poll_email(time.time())  # inside the poll interval
    assert len(s.gmail.queries) == 1


@pytest.mark.asyncio
async def test_mail_from_before_switch_on_does_not_count_and_old_jobs_ignore_email(env):
    s = env.sched
    job_id = await _job(s)
    await s.set_enabled(job_id, True)
    async with s.db.execute("SELECT since FROM cron_jobs WHERE id = ?", (job_id,)) as cur:
        assert abs((await cur.fetchone())[0] - time.time()) < 5
    await s.add_job("every hour", 3600)
    await s._tick()  # the timer loop does not fire the email job
    assert not [e for e in env.log if e[0] == "run"]


@pytest.mark.asyncio
async def test_fails_closed_without_a_store(tmp_path):
    sched = Scheduler(str(tmp_path / "s.db"), AsyncMock(), None, [])
    await sched.init()
    try:
        job_id = await _job(sched)
        await sched.set_enabled(job_id, True)
        sched.gmail = FakeGmail(_msg("m1"))
        await sched._poll_email(time.time())
        assert sched.gmail.queries == []
    finally:
        await sched.close()


@pytest.mark.asyncio
async def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE cron_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, description TEXT NOT NULL, "
               "interval_seconds INTEGER NOT NULL, next_run_at REAL NOT NULL, one_shot INTEGER DEFAULT 0, "
               "user_id TEXT DEFAULT 'terminal-user', platform TEXT DEFAULT 'terminal', chat_id TEXT DEFAULT '', "
               "enabled INTEGER DEFAULT 1, created_at REAL NOT NULL)")
    db.execute("INSERT INTO cron_jobs (description, interval_seconds, next_run_at, created_at) "
               "VALUES ('old', 60, 0, 0)")
    db.commit()
    db.close()
    sched = Scheduler(str(path), AsyncMock(return_value="ok"), None, [])
    await sched.init()
    try:
        jobs = await sched.list_jobs()
        assert jobs[0]["description"] == "old" and jobs[0]["trigger"] == "time" and jobs[0]["match"] is None
    finally:
        await sched.close()


@pytest.mark.asyncio
async def test_cron_tool_creates_email_jobs_paused(env):
    from micropaw.tools.cron_tool import CronTool, set_scheduler
    set_scheduler(env.sched)
    try:
        ctx = ToolContext(conversation_id="c", user_id="u", platform="telegram", chat_id="telegram:1")
        tool = CronTool()
        assert "read, summarize" in tool.description.lower() or "summarize" in tool.description
        out = await tool.execute({"action": "add", "trigger": "email", "description": "watch invoices",
                                  "from_address": OWNER, "subject_contains": "invoice"}, ctx)
        assert "paused" in out and "/cron enable" in out
        job = (await env.sched.list_jobs())[0]
        assert job["trigger"] == "email" and not job["enabled"] and job["match"]["from"] == OWNER
        assert "on email from boss@example.com" in await tool.execute({"action": "list"}, ctx)
        for bad in ({"from_address": ""}, {"from_address": "*@example.com"}, {"subject_contains": "x"}):
            assert (await tool.execute({"action": "add", "trigger": "email", "description": "d", **bad}, ctx)
                    ).startswith("Error")
        assert len(await env.sched.list_jobs()) == 1
    finally:
        set_scheduler(None)  # type: ignore[arg-type]


def test_cli_cron_add_on_email(tmp_path, monkeypatch):
    import micropaw.cli as cli
    monkeypatch.setattr(cli, "DB_FILE", tmp_path / "cli.db")
    from micropaw.cli import main
    runner = CliRunner()
    out = runner.invoke(main, ["cron", "add", "summarize invoices", "--on-email", "--from", OWNER])
    assert "Created email job #1" in out.output and "unattended" in out.output
    assert "Exact sender" not in out.output
    for args in (["--on-email"], ["--on-email", "--from", "*@x.com"], ["--on-email", "--from", OWNER, "--every", "1h"]):
        out = runner.invoke(main, ["cron", "add", "d", *args])
        assert "Created" not in out.output
    listed = runner.invoke(main, ["cron", "list"]).output
    assert "on email from" in listed and "boss@example.com" in listed and "on" in listed
    job = json.loads(sqlite3.connect(tmp_path / "cli.db").execute("SELECT match FROM cron_jobs").fetchone()[0])
    assert job["from"] == OWNER
