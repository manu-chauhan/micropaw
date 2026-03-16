"""Tests for the scheduler, cron tool, heartbeat, and persona files."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from micropaw.tools.base import ToolContext

# ---------------------------------------------------------------------------
# Interval parsing (CLI)
# ---------------------------------------------------------------------------

class TestIntervalParsing:
    def test_hours(self):
        from micropaw.cli import _parse_interval
        assert _parse_interval("6h") == 21600

    def test_minutes(self):
        from micropaw.cli import _parse_interval
        assert _parse_interval("45m") == 2700

    def test_seconds(self):
        from micropaw.cli import _parse_interval
        assert _parse_interval("30s") == 30

    def test_days(self):
        from micropaw.cli import _parse_interval
        assert _parse_interval("1d") == 86400

    def test_bare_number(self):
        from micropaw.cli import _parse_interval
        assert _parse_interval("3600") == 3600

    def test_whitespace(self):
        from micropaw.cli import _parse_interval
        assert _parse_interval("  6h  ") == 21600

    def test_invalid(self):
        import click

        from micropaw.cli import _parse_interval
        with pytest.raises(click.BadParameter):
            _parse_interval("abc")


# ---------------------------------------------------------------------------
# Interval formatting (cron tool)
# ---------------------------------------------------------------------------

class TestIntervalFormatting:
    def test_days(self):
        from micropaw.tools.cron_tool import _fmt_interval
        assert _fmt_interval(86400) == "1d"

    def test_hours(self):
        from micropaw.tools.cron_tool import _fmt_interval
        assert _fmt_interval(3600) == "1h"
        assert _fmt_interval(7200) == "2h"

    def test_minutes(self):
        from micropaw.tools.cron_tool import _fmt_interval
        assert _fmt_interval(60) == "1m"
        assert _fmt_interval(2700) == "45m"

    def test_seconds(self):
        from micropaw.tools.cron_tool import _fmt_interval
        assert _fmt_interval(30) == "30s"

    def test_mixed(self):
        from micropaw.tools.cron_tool import _fmt_interval
        # 90 seconds doesn't divide evenly into minutes
        assert _fmt_interval(90) == "90s"


# ---------------------------------------------------------------------------
# Scheduler CRUD
# ---------------------------------------------------------------------------

class TestSchedulerCRUD:
    async def test_add_and_list(self, tmp_path):
        from micropaw.scheduler import Scheduler
        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[])
        await sched.init()
        try:
            job_id = await sched.add_job("test task", 3600)
            assert job_id is not None
            assert job_id > 0

            jobs = await sched.list_jobs()
            assert len(jobs) == 1
            assert jobs[0]["description"] == "test task"
            assert jobs[0]["interval_seconds"] == 3600
            assert jobs[0]["one_shot"] is False
        finally:
            await sched.close()

    async def test_add_one_shot(self, tmp_path):
        from micropaw.scheduler import Scheduler
        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[])
        await sched.init()
        try:
            await sched.add_job("reminder", 7200, one_shot=True)
            jobs = await sched.list_jobs()
            assert len(jobs) == 1
            assert jobs[0]["one_shot"] is True
        finally:
            await sched.close()

    async def test_remove(self, tmp_path):
        from micropaw.scheduler import Scheduler
        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[])
        await sched.init()
        try:
            job_id = await sched.add_job("to remove", 3600)
            removed = await sched.remove_job(job_id)
            assert removed is True
            jobs = await sched.list_jobs()
            assert len(jobs) == 0
        finally:
            await sched.close()

    async def test_remove_nonexistent(self, tmp_path):
        from micropaw.scheduler import Scheduler
        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[])
        await sched.init()
        try:
            removed = await sched.remove_job(999)
            assert removed is False
        finally:
            await sched.close()

    async def test_max_jobs(self, tmp_path):
        from micropaw.scheduler import Scheduler
        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[], max_jobs=3)
        await sched.init()
        try:
            await sched.add_job("job 1", 60)
            await sched.add_job("job 2", 60)
            await sched.add_job("job 3", 60)
            with pytest.raises(ValueError, match="Maximum of 3"):
                await sched.add_job("job 4", 60)
        finally:
            await sched.close()

    async def test_add_stores_context(self, tmp_path):
        from micropaw.scheduler import Scheduler
        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[])
        await sched.init()
        try:
            await sched.add_job("test", 60, user_id="u1", platform="telegram", chat_id="chat123")
            jobs = await sched.list_jobs()
            # Verify the job was created (context stored in DB but not returned in list)
            assert len(jobs) == 1
        finally:
            await sched.close()


# ---------------------------------------------------------------------------
# Cron Tool
# ---------------------------------------------------------------------------

class TestCronTool:
    async def test_add_action(self, tmp_path):
        from micropaw.scheduler import Scheduler
        from micropaw.tools.cron_tool import CronTool, set_scheduler

        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[])
        await sched.init()
        set_scheduler(sched)
        try:
            tool = CronTool()
            ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
            result = await tool.execute(
                {"action": "add", "description": "water plants", "interval_seconds": 86400},
                ctx,
            )
            assert "Scheduled recurring job" in result
            assert "water plants" in result
        finally:
            set_scheduler(None)  # type: ignore[arg-type]
            await sched.close()

    async def test_add_one_shot(self, tmp_path):
        from micropaw.scheduler import Scheduler
        from micropaw.tools.cron_tool import CronTool, set_scheduler

        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[])
        await sched.init()
        set_scheduler(sched)
        try:
            tool = CronTool()
            ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
            result = await tool.execute(
                {"action": "add", "description": "remind me", "interval_seconds": 7200, "one_shot": True},
                ctx,
            )
            assert "one-shot" in result
        finally:
            set_scheduler(None)  # type: ignore[arg-type]
            await sched.close()

    async def test_list_empty(self, tmp_path):
        from micropaw.scheduler import Scheduler
        from micropaw.tools.cron_tool import CronTool, set_scheduler

        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[])
        await sched.init()
        set_scheduler(sched)
        try:
            tool = CronTool()
            ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
            result = await tool.execute({"action": "list"}, ctx)
            assert "No scheduled jobs" in result
        finally:
            set_scheduler(None)  # type: ignore[arg-type]
            await sched.close()

    async def test_list_with_jobs(self, tmp_path):
        from micropaw.scheduler import Scheduler
        from micropaw.tools.cron_tool import CronTool, set_scheduler

        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[])
        await sched.init()
        set_scheduler(sched)
        try:
            await sched.add_job("daily check", 86400)
            tool = CronTool()
            ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
            result = await tool.execute({"action": "list"}, ctx)
            assert "daily check" in result
            assert "1d" in result
        finally:
            set_scheduler(None)  # type: ignore[arg-type]
            await sched.close()

    async def test_remove_action(self, tmp_path):
        from micropaw.scheduler import Scheduler
        from micropaw.tools.cron_tool import CronTool, set_scheduler

        db = str(tmp_path / "test.db")
        sched = Scheduler(db, handler=None, bus=None, channels=[])
        await sched.init()
        set_scheduler(sched)
        try:
            job_id = await sched.add_job("to remove", 60)
            tool = CronTool()
            ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
            result = await tool.execute({"action": "remove", "job_id": job_id}, ctx)
            assert "removed" in result
        finally:
            set_scheduler(None)  # type: ignore[arg-type]
            await sched.close()

    async def test_add_missing_fields(self):
        from micropaw.tools.cron_tool import CronTool, set_scheduler
        set_scheduler(MagicMock())
        try:
            tool = CronTool()
            ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
            result = await tool.execute({"action": "add"}, ctx)
            assert "Error" in result
        finally:
            set_scheduler(None)  # type: ignore[arg-type]

    async def test_no_scheduler(self):
        from micropaw.tools.cron_tool import CronTool, set_scheduler
        set_scheduler(None)  # type: ignore[arg-type]
        tool = CronTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
        result = await tool.execute({"action": "list"}, ctx)
        assert "not initialized" in result


# ---------------------------------------------------------------------------
# Heartbeat parsing
# ---------------------------------------------------------------------------

class TestHeartbeat:
    async def test_process_heartbeat(self, tmp_path):
        from micropaw.scheduler import Scheduler

        heartbeat = tmp_path / "HEARTBEAT.md"
        heartbeat.write_text(
            "# Tasks\n"
            "- [ ] Check weather\n"
            "- [x] Already done\n"
            "- [ ] Send report\n"
            "- Just a note\n"
        )

        handler = AsyncMock(return_value="done")
        bus = MagicMock()
        bus.closed = False
        bus.publish_outbound = AsyncMock()

        sched = Scheduler(str(tmp_path / "test.db"), handler=handler, bus=bus, channels=[])
        await sched.init()

        with patch("micropaw.scheduler.CONFIG_DIR", tmp_path):
            await sched._process_heartbeat()

        # Should have called handler for 2 unchecked items
        assert handler.call_count == 2
        calls = [c.args[0] for c in handler.call_args_list]
        assert any("Check weather" in c for c in calls)
        assert any("Send report" in c for c in calls)
        await sched.close()

    async def test_heartbeat_no_file(self, tmp_path):
        from micropaw.scheduler import Scheduler

        handler = AsyncMock()
        sched = Scheduler(str(tmp_path / "test.db"), handler=handler, bus=None, channels=[])
        await sched.init()

        with patch("micropaw.scheduler.CONFIG_DIR", tmp_path):
            await sched._process_heartbeat()

        # No file, no calls
        handler.assert_not_called()
        await sched.close()

    async def test_heartbeat_skips_checked(self, tmp_path):
        from micropaw.scheduler import Scheduler

        heartbeat = tmp_path / "HEARTBEAT.md"
        heartbeat.write_text("- [x] Done task\n- [x] Also done\n")

        handler = AsyncMock()
        sched = Scheduler(str(tmp_path / "test.db"), handler=handler, bus=None, channels=[])
        await sched.init()

        with patch("micropaw.scheduler.CONFIG_DIR", tmp_path):
            await sched._process_heartbeat()

        handler.assert_not_called()
        await sched.close()


# ---------------------------------------------------------------------------
# Persona files
# ---------------------------------------------------------------------------

class TestPersonaFiles:
    def test_soul_file_overrides_personality(self, tmp_path):
        from micropaw.config import Config
        from micropaw.core.agent import Agent

        soul_file = tmp_path / "SOUL.md"
        soul_file.write_text("You are a pirate who loves coding. Always say 'Arrr!'")

        config = Config()
        agent = Agent.__new__(Agent)
        agent.config = config
        agent.registry = MagicMock()
        agent.registry.all.return_value = []
        agent._file_cache = {}

        with patch("micropaw.config.CONFIG_DIR", tmp_path):
            prompt = agent._build_system_prompt("terminal")

        assert "pirate" in prompt
        assert "Arrr" in prompt
        # Default personality should NOT be present
        assert "friendly, helpful" not in prompt

    def test_user_file_injected(self, tmp_path):
        from micropaw.config import Config
        from micropaw.core.agent import Agent

        user_file = tmp_path / "USER.md"
        user_file.write_text("Timezone: IST\nPrefers dark mode")

        config = Config()
        agent = Agent.__new__(Agent)
        agent.config = config
        agent.registry = MagicMock()
        agent.registry.all.return_value = []
        agent._file_cache = {}

        with patch("micropaw.config.CONFIG_DIR", tmp_path):
            prompt = agent._build_system_prompt("terminal")

        assert "About the user:" in prompt
        assert "IST" in prompt

    def test_no_persona_files_uses_defaults(self, tmp_path):
        from micropaw.config import Config
        from micropaw.core.agent import Agent

        config = Config()
        agent = Agent.__new__(Agent)
        agent.config = config
        agent.registry = MagicMock()
        agent.registry.all.return_value = []
        agent._file_cache = {}

        with patch("micropaw.config.CONFIG_DIR", tmp_path):
            prompt = agent._build_system_prompt("terminal")

        assert "friendly, helpful" in prompt
        assert "About the user:" not in prompt

    def test_soul_file_truncated(self, tmp_path):
        from micropaw.config import Config
        from micropaw.core.agent import Agent

        soul_file = tmp_path / "SOUL.md"
        soul_file.write_text("x" * 5000)

        config = Config()
        agent = Agent.__new__(Agent)
        agent.config = config
        agent.registry = MagicMock()
        agent.registry.all.return_value = []
        agent._file_cache = {}

        with patch("micropaw.config.CONFIG_DIR", tmp_path):
            prompt = agent._build_system_prompt("terminal")

        # 2048 chars max for soul + other parts + bundled SKILLS.md (~1900)
        assert len(prompt) < 5000

    def test_empty_soul_file_uses_default(self, tmp_path):
        from micropaw.config import Config
        from micropaw.core.agent import Agent

        soul_file = tmp_path / "SOUL.md"
        soul_file.write_text("")

        config = Config()
        agent = Agent.__new__(Agent)
        agent.config = config
        agent.registry = MagicMock()
        agent.registry.all.return_value = []
        agent._file_cache = {}

        with patch("micropaw.config.CONFIG_DIR", tmp_path):
            prompt = agent._build_system_prompt("terminal")

        assert "friendly, helpful" in prompt


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestSchedulerConfig:
    def test_defaults(self):
        from micropaw.config import Config
        c = Config()
        assert c.scheduler.heartbeat_interval == 1800
        assert c.scheduler.max_jobs == 50

    def test_custom(self):
        from micropaw.config import Config
        c = Config(scheduler={"heartbeat_interval": 900, "max_jobs": 20})
        assert c.scheduler.heartbeat_interval == 900
        assert c.scheduler.max_jobs == 20
