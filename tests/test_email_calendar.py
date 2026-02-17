"""Tests for email and calendar tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from micropaw.tools.base import ToolContext


@pytest.fixture
def context():
    return ToolContext(
        conversation_id="test-conv",
        user_id="test-user",
        platform="terminal",
        allow_shell=False,
        allow_file_write=False,
        allow_web_browse=True,
        confirm_dangerous=False,
    )


# ---------------------------------------------------------------------------
# Email tool tests
# ---------------------------------------------------------------------------

class TestEmailTool:

    @pytest.fixture(autouse=True)
    def setup(self):
        import micropaw.tools.email_tool as mod
        self.mod = mod
        self.orig_config = mod._config
        cfg = MagicMock()
        cfg.email_address = "test@gmail.com"
        cfg.password = "testpass"
        cfg.imap_host = "imap.gmail.com"
        cfg.imap_port = 993
        cfg.smtp_host = "smtp.gmail.com"
        cfg.smtp_port = 465
        mod._config = cfg
        yield
        mod._config = self.orig_config

    async def test_not_configured(self, context):
        self.mod._config = None
        tool = self.mod.EmailTool()
        result = await tool.execute({"action": "inbox"}, context)
        assert "not configured" in result.lower()

    async def test_inbox_calls_imap(self, context):
        with patch.object(self.mod, "_imap_inbox", return_value="Recent emails (2):") as mock:
            tool = self.mod.EmailTool()
            result = await tool.execute({"action": "inbox", "count": 5}, context)
            mock.assert_called_once_with(5, "all")
            assert "Recent emails" in result

    async def test_inbox_count_clamped(self, context):
        with patch.object(self.mod, "_imap_inbox", return_value="ok") as mock:
            tool = self.mod.EmailTool()
            await tool.execute({"action": "inbox", "count": 100}, context)
            mock.assert_called_once_with(50, "all")

    async def test_read_requires_message_id(self, context):
        tool = self.mod.EmailTool()
        result = await tool.execute({"action": "read"}, context)
        assert "requires" in result.lower()

    async def test_read_calls_imap(self, context):
        with patch.object(self.mod, "_imap_read", return_value="From: a\nBody") as mock:
            tool = self.mod.EmailTool()
            result = await tool.execute({"action": "read", "message_id": "123"}, context)
            mock.assert_called_once_with("123")
            assert "From:" in result

    async def test_send_requires_to_and_body(self, context):
        tool = self.mod.EmailTool()
        result = await tool.execute({"action": "send", "to": "a@b.com"}, context)
        assert "requires" in result.lower()

    async def test_send_calls_smtp(self, context):
        with patch.object(self.mod, "_smtp_send", return_value="Email sent to a@b.com") as mock:
            tool = self.mod.EmailTool()
            result = await tool.execute(
                {"action": "send", "to": "a@b.com", "subject": "Hi", "body": "Hello"},
                context,
            )
            mock.assert_called_once_with("a@b.com", "Hi", "Hello")
            assert "sent" in result.lower()

    async def test_search_requires_query(self, context):
        tool = self.mod.EmailTool()
        result = await tool.execute({"action": "search"}, context)
        assert "requires" in result.lower()

    async def test_search_calls_imap(self, context):
        with patch.object(self.mod, "_imap_search", return_value="Search results (1 messages):") as mock:
            tool = self.mod.EmailTool()
            result = await tool.execute({"action": "search", "query": "FROM john"}, context)
            mock.assert_called_once_with("FROM john")

    async def test_unknown_action(self, context):
        tool = self.mod.EmailTool()
        result = await tool.execute({"action": "forward"}, context)
        assert "unknown" in result.lower()

    def test_strip_html(self):
        assert "hello" in self.mod._strip_html("<p>hello</p>")
        assert "<" not in self.mod._strip_html("<b>bold</b>")

    def test_decode_header_value(self):
        assert self.mod._decode_header_value("plain text") == "plain text"


# ---------------------------------------------------------------------------
# Calendar tool tests
# ---------------------------------------------------------------------------

class TestCalendarTool:

    @pytest.fixture(autouse=True)
    def setup(self):
        import micropaw.tools.calendar_tool as mod
        self.mod = mod
        self.orig_config = mod._config
        cfg = MagicMock()
        cfg.client_id = "test-client-id"
        cfg.client_secret = "test-client-secret"
        mod._config = cfg
        yield
        mod._config = self.orig_config

    async def test_authorize_not_configured(self, context):
        self.mod._config = None
        tool = self.mod.CalendarTool()
        result = await tool.execute({"action": "authorize"}, context)
        assert "not configured" in result.lower()

    async def test_list_requires_token(self, context):
        with patch.object(self.mod, "_get_access_token", return_value=None):
            tool = self.mod.CalendarTool()
            result = await tool.execute({"action": "list"}, context)
            assert "not authorized" in result.lower()

    async def test_list_events(self, context):
        mock_data = {
            "items": [{
                "summary": "Team Sync",
                "start": {"dateTime": "2026-02-20T10:00:00Z"},
                "end": {"dateTime": "2026-02-20T11:00:00Z"},
                "id": "ev123",
            }]
        }
        with patch.object(self.mod, "_get_access_token", return_value="tok"):
            with patch.object(self.mod, "_api_get", return_value=mock_data):
                tool = self.mod.CalendarTool()
                result = await tool.execute(
                    {"action": "list", "start_date": "2026-02-20"},
                    context,
                )
                assert "Team Sync" in result

    async def test_create_requires_title(self, context):
        with patch.object(self.mod, "_get_access_token", return_value="tok"):
            tool = self.mod.CalendarTool()
            result = await tool.execute({"action": "create"}, context)
            assert "requires" in result.lower()

    async def test_create_event(self, context):
        mock_resp = {"summary": "Meeting", "id": "ev456", "htmlLink": "https://cal.google.com/ev456"}
        with patch.object(self.mod, "_get_access_token", return_value="tok"):
            with patch.object(self.mod, "_api_post", return_value=mock_resp):
                tool = self.mod.CalendarTool()
                result = await tool.execute(
                    {
                        "action": "create",
                        "title": "Meeting",
                        "start_time": "2026-02-20T10:00:00",
                        "end_time": "2026-02-20T11:00:00",
                    },
                    context,
                )
                assert "created" in result.lower()
                assert "ev456" in result

    async def test_create_allday(self, context):
        mock_resp = {"summary": "Holiday", "id": "ev789", "htmlLink": "https://cal.google.com/ev789"}
        with patch.object(self.mod, "_get_access_token", return_value="tok"):
            with patch.object(self.mod, "_api_post", return_value=mock_resp):
                tool = self.mod.CalendarTool()
                result = await tool.execute(
                    {"action": "create", "title": "Holiday", "all_day": True, "start_date": "2026-03-01"},
                    context,
                )
                assert "created" in result.lower()

    async def test_delete_requires_event_id(self, context):
        with patch.object(self.mod, "_get_access_token", return_value="tok"):
            tool = self.mod.CalendarTool()
            result = await tool.execute({"action": "delete"}, context)
            assert "requires" in result.lower()

    async def test_delete_event(self, context):
        with patch.object(self.mod, "_get_access_token", return_value="tok"):
            with patch.object(self.mod, "_api_delete", return_value=204):
                tool = self.mod.CalendarTool()
                result = await tool.execute({"action": "delete", "event_id": "ev123"}, context)
                assert "deleted" in result.lower()

    async def test_search_requires_query(self, context):
        with patch.object(self.mod, "_get_access_token", return_value="tok"):
            tool = self.mod.CalendarTool()
            result = await tool.execute({"action": "search"}, context)
            assert "requires" in result.lower()

    async def test_search_events(self, context):
        mock_data = {
            "items": [{
                "summary": "Budget Review",
                "start": {"dateTime": "2026-02-25T14:00:00Z"},
                "end": {"dateTime": "2026-02-25T15:00:00Z"},
                "id": "ev999",
            }]
        }
        with patch.object(self.mod, "_get_access_token", return_value="tok"):
            with patch.object(self.mod, "_api_get", return_value=mock_data):
                tool = self.mod.CalendarTool()
                result = await tool.execute({"action": "search", "query": "budget"}, context)
                assert "Budget Review" in result

    async def test_unknown_action(self, context):
        with patch.object(self.mod, "_get_access_token", return_value="tok"):
            tool = self.mod.CalendarTool()
            result = await tool.execute({"action": "reschedule"}, context)
            assert "unknown" in result.lower()

    def test_format_event(self):
        ev = {
            "summary": "Test",
            "start": {"dateTime": "2026-02-20T10:00:00Z"},
            "end": {"dateTime": "2026-02-20T11:00:00Z"},
            "id": "ev1",
            "location": "Room 1",
        }
        result = self.mod._format_event(ev)
        assert "Test" in result
        assert "Room 1" in result
        assert "ev1" in result

    def test_token_save_load(self, tmp_path):
        original_file = self.mod._TOKEN_FILE
        self.mod._TOKEN_FILE = tmp_path / "tokens.json"
        try:
            tokens = {"access_token": "abc", "refresh_token": "def", "expires_at": 9999999999}
            self.mod._save_tokens(tokens)
            loaded = self.mod._load_tokens()
            assert loaded["access_token"] == "abc"
            assert loaded["refresh_token"] == "def"
        finally:
            self.mod._TOKEN_FILE = original_file

    def test_load_tokens_missing_file(self, tmp_path):
        original_file = self.mod._TOKEN_FILE
        self.mod._TOKEN_FILE = tmp_path / "nonexistent.json"
        try:
            assert self.mod._load_tokens() is None
        finally:
            self.mod._TOKEN_FILE = original_file


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestEmailCalendarConfig:

    def test_email_defaults(self):
        from micropaw.config import EmailConfig
        cfg = EmailConfig()
        assert cfg.imap_host == "imap.gmail.com"
        assert cfg.smtp_port == 465
        assert not cfg.enabled

    def test_calendar_defaults(self):
        from micropaw.config import CalendarConfig
        cfg = CalendarConfig()
        assert not cfg.enabled
        assert cfg.client_id == ""

    def test_configs_in_root(self):
        from micropaw.config import Config
        cfg = Config()
        assert hasattr(cfg, "email")
        assert hasattr(cfg, "calendar")

    def test_email_password_redacted(self):
        from micropaw.config import Config, display_config
        cfg = Config()
        cfg.email.password = "abcdefghijklmnop"
        data = display_config(cfg)
        assert data["email"]["password"] != "abcdefghijklmnop"
        assert "..." in data["email"]["password"]

    def test_calendar_secret_redacted(self):
        from micropaw.config import Config, display_config
        cfg = Config()
        cfg.calendar.client_secret = "super-secret-value-here"
        data = display_config(cfg)
        assert data["calendar"]["client_secret"] != "super-secret-value-here"
        assert "..." in data["calendar"]["client_secret"]
