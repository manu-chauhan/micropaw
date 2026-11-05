"""Scheduled jobs run on the agent that created them, and are skipped when that agent is gone."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from micropaw import cli
from micropaw.config import Config
from micropaw.gateway import Gateway
from micropaw.scheduler import AgentGoneError, Scheduler, email_rule
from micropaw.tools.base import ToolContext
from micropaw.tools.cron_tool import CronTool, set_scheduler

OWNER = "boss@example.com"


@pytest.fixture
async def sched(tmp_path):
    calls: list[tuple] = []

    async def handler(*args, **kw):
        calls.append((args, kw))
        return "done"

    bus = SimpleNamespace(closed=False, publish_outbound=AsyncMock())
    s = Scheduler(str(tmp_path / "s.db"), handler, bus, [SimpleNamespace(name="telegram")], store=MagicMock())
    await s.init()
    s.record_activity("telegram", "telegram:1")
    s.calls, s.sent = calls, bus.publish_outbound
    yield s
    await s.close()


async def _due(s, job_id):
    await s.db.execute("UPDATE cron_jobs SET next_run_at = 0 WHERE id = ?", (job_id,))
    await s.db.commit()


async def test_a_job_runs_on_its_own_agent_and_older_jobs_on_the_default(sched):
    pinned = await sched.add_job("check", 3600, platform="telegram", chat_id="telegram:1", agent="family")
    plain = await sched.add_job("old style", 3600, platform="telegram", chat_id="telegram:1")
    for job in (pinned, plain):
        await _due(sched, job)
    await sched._tick()
    by_job = {args[3]: kw for args, kw in sched.calls}
    assert by_job[f"cron-{pinned}"] == {"agent": "family"}
    assert by_job[f"cron-{plain}"] == {}  # no agent: the gateway routes it to main as before
    assert {j["id"]: j["agent"] for j in await sched.list_jobs()} == {pinned: "family", plain: "main"}


async def test_a_job_whose_agent_is_gone_is_skipped_and_the_owner_is_told(sched):
    async def gone(*args, **kw):
        raise AgentGoneError("agent 'family' is not configured any more, so the job did not run.")

    sched._handler = gone
    job = await sched.add_job("check", 3600, platform="telegram", chat_id="telegram:1", agent="family")
    await _due(sched, job)
    await sched._tick()
    msg = sched.sent.call_args.args[0]
    assert "Skipped" in msg.content and "family" in msg.content
    async with sched.db.execute("SELECT next_run_at FROM cron_jobs WHERE id = ?", (job,)) as cur:
        assert (await cur.fetchone())[0] > time.time()  # it waits for its next turn, no tight loop


async def test_an_email_job_runs_on_its_own_agent(sched):
    import base64

    headers = [{"name": "From", "value": f"B <{OWNER}>"}, {"name": "Subject", "value": "Hi"},
               {"name": "Authentication-Results",
                "value": "mx.google.com; dmarc=pass (p=NONE) header.from=example.com"}]
    data = base64.urlsafe_b64encode(b"body").decode()
    message = {"id": "m1", "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": data}}}

    class Gmail:
        async def search(self, q, limit):
            return ["m1"]

        async def get(self, mid):
            return message

    sched.gmail = Gmail()
    sched._store.get_or_create_conversation = AsyncMock()
    sched._store.set_marked = AsyncMock()
    job = await sched.add_job("watch", 0, platform="telegram", chat_id="telegram:1", enabled=False,
                              match=email_rule(OWNER), agent="family")
    await sched.set_enabled(job, True)
    await sched._poll_email(time.time())
    (args, kw), = sched.calls
    assert args[3] == f"email-{job}" and kw == {"agent": "family"}


async def test_the_cron_tool_stores_the_agent_of_the_chat(sched):
    set_scheduler(sched)
    try:
        ctx = ToolContext("c", "u", "telegram", chat_id="telegram:1", agent="family")
        await CronTool().execute({"action": "add", "description": "d", "interval_seconds": 3600}, ctx)
        await CronTool().execute({"action": "add", "trigger": "email", "description": "e",
                                  "from_address": OWNER}, ctx)
        assert {j["agent"] for j in await sched.list_jobs()} == {"family"}
    finally:
        set_scheduler(None)  # type: ignore[arg-type]


def test_agent_puts_its_name_in_the_tool_context():
    from micropaw.core.agent import Agent
    from micropaw.core.profile import AgentProfile

    from .test_agent import MockProvider, _make_agent

    base = _make_agent(MockProvider())
    agent = Agent(base.config, base.provider, base.registry, base.store, AgentProfile(name="family", main="ollama:x"))
    assert agent._build_tool_context("c", "u", "telegram").agent == "family"


async def test_gateway_runs_a_pinned_job_on_that_agent_only():
    gw = Gateway(Config(), MagicMock(), MagicMock(), [])
    main, private = MagicMock(), MagicMock()
    main.handle_message = AsyncMock(return_value="from main")
    private.handle_message = AsyncMock(return_value="from private")
    gw.agent, gw.agents = main, {"main": main, "private": private}
    assert await gw._handle_message("task", "cron", "system", "cron-1", agent="private") == "from private"
    main.handle_message.assert_not_called()
    assert await gw._handle_message("task", "cron", "system", "cron-2") == "from main"
    with pytest.raises(AgentGoneError):
        await gw._handle_message("task", "cron", "system", "cron-3", agent="removed")
    assert main.handle_message.await_count == 1  # the missing agent's job did not fall back to main


def test_cli_cron_add_takes_an_agent(tmp_path, monkeypatch):
    from micropaw import config

    monkeypatch.setattr(cli, "DB_FILE", tmp_path / "d.db")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: Config(agents={"family": {"model": "ollama:qwen3.5:9b"}}))
    runner = CliRunner()
    bad = runner.invoke(cli.main, ["cron", "add", "x", "--every", "1h", "--agent", "nobody"])
    assert "No agent called 'nobody'" in bad.output
    good = runner.invoke(cli.main, ["cron", "add", "x", "--every", "1h", "--agent", "family"])
    assert "Created recurring job #1" in good.output
    assert "family" in runner.invoke(cli.main, ["cron", "list"]).output


async def test_a_job_made_in_local_mode_says_so_to_the_handler(sched, monkeypatch):
    from micropaw import privacy

    monkeypatch.setattr(privacy, "local_only", lambda: True)
    local = await sched.add_job("check", 3600, platform="telegram", chat_id="telegram:1")
    monkeypatch.setattr(privacy, "local_only", lambda: False)
    plain = await sched.add_job("other", 3600, platform="telegram", chat_id="telegram:1")
    for job in (local, plain):
        await _due(sched, job)
    await sched._tick()
    by_job = {args[3]: kw for args, kw in sched.calls}
    assert by_job[f"cron-{local}"] == {"local_only": True}
    assert by_job[f"cron-{plain}"] == {}


async def test_gateway_never_runs_a_local_job_on_a_cloud_model(monkeypatch):
    from micropaw import privacy
    from micropaw.scheduler import LocalJobError

    gw = Gateway(Config(), MagicMock(), MagicMock(), [])
    main = MagicMock()
    main.handle_message = AsyncMock(return_value="from main")
    gw.agent, gw.agents = main, {"main": main}
    monkeypatch.setattr(privacy, "is_local_provider", lambda p: False)
    with pytest.raises(LocalJobError):
        await gw._handle_message("task", "cron", "system", "cron-1", local_only=True)
    main.handle_message.assert_not_called()  # the job's text never reached the model
    assert await gw._handle_message("task", "cron", "system", "cron-2") == "from main"
    monkeypatch.setattr(privacy, "is_local_provider", lambda p: True)
    assert await gw._handle_message("task", "cron", "system", "cron-3", local_only=True) == "from main"
