"""Tests for unified Google OAuth2, email (Gmail API), and calendar tools."""

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


class TestGoogleAuth:

    @pytest.fixture(autouse=True)
    def setup(self):
        import micropaw.tools.google_auth as mod
        self.mod = mod
        self.orig_config = mod._config
        cfg = MagicMock()
        cfg.client_id = "test-client-id"
        cfg.client_secret = "test-client-secret"
        cfg.gmail_enabled = True
        cfg.calendar_enabled = True
        mod._config = cfg
        yield
        mod._config = self.orig_config

    def test_token_save_load(self, tmp_path):
        orig = self.mod._TOKEN_FILE
        self.mod._TOKEN_FILE = tmp_path / "tokens.json"
        try:
            tokens = {"access_token": "abc", "refresh_token": "def", "expires_at": 9999999999}
            self.mod._save_tokens(tokens)
            loaded = self.mod._load_tokens()
            assert loaded["access_token"] == "abc"
            assert loaded["refresh_token"] == "def"
        finally:
            self.mod._TOKEN_FILE = orig

    def test_load_tokens_missing_file(self, tmp_path):
        orig = self.mod._TOKEN_FILE
        self.mod._TOKEN_FILE = tmp_path / "nonexistent.json"
        try:
            assert self.mod._load_tokens() is None
        finally:
            self.mod._TOKEN_FILE = orig

    def test_required_scopes_both(self):
        scopes = self.mod._required_scopes()
        assert "calendar" in scopes
        assert "gmail.modify" in scopes

    def test_required_scopes_gmail_only(self):
        self.mod._config.calendar_enabled = False
        scopes = self.mod._required_scopes()
        assert "gmail.modify" in scopes
        assert "calendar" not in scopes

    def test_required_scopes_calendar_only(self):
        self.mod._config.gmail_enabled = False
        scopes = self.mod._required_scopes()
        assert "calendar" in scopes
        assert "gmail" not in scopes

    def test_needs_reauth_no_tokens(self, tmp_path):
        orig = self.mod._TOKEN_FILE
        self.mod._TOKEN_FILE = tmp_path / "nonexistent.json"
        try:
            assert self.mod.needs_reauth() is True
        finally:
            self.mod._TOKEN_FILE = orig

    def test_needs_reauth_missing_scope(self, tmp_path):
        orig = self.mod._TOKEN_FILE
        self.mod._TOKEN_FILE = tmp_path / "tokens.json"
        try:
            tokens = {"access_token": "x", "scopes": self.mod.SCOPE_CALENDAR}
            self.mod._save_tokens(tokens)
            # Config wants both, but only calendar authorized
            assert self.mod.needs_reauth() is True
        finally:
            self.mod._TOKEN_FILE = orig

    def test_needs_reauth_all_scopes_present(self, tmp_path):
        orig = self.mod._TOKEN_FILE
        self.mod._TOKEN_FILE = tmp_path / "tokens.json"
        try:
            tokens = {"access_token": "x", "scopes": f"{self.mod.SCOPE_CALENDAR} {self.mod.SCOPE_GMAIL}"}
            self.mod._save_tokens(tokens)
            assert self.mod.needs_reauth() is False
        finally:
            self.mod._TOKEN_FILE = orig

    async def test_get_access_token_valid(self, tmp_path):
        import time
        orig = self.mod._TOKEN_FILE
        self.mod._TOKEN_FILE = tmp_path / "tokens.json"
        try:
            tokens = {"access_token": "valid-tok", "expires_at": time.time() + 3600}
            self.mod._save_tokens(tokens)
            result = await self.mod.get_access_token()
            assert result == "valid-tok"
        finally:
            self.mod._TOKEN_FILE = orig

    async def test_get_access_token_none_when_missing(self, tmp_path):
        orig = self.mod._TOKEN_FILE
        self.mod._TOKEN_FILE = tmp_path / "nonexistent.json"
        try:
            result = await self.mod.get_access_token()
            assert result is None
        finally:
            self.mod._TOKEN_FILE = orig

    async def test_authorize_not_configured(self):
        self.mod._config = None
        result = await self.mod.authorize()
        assert "not configured" in result.lower()

    def test_set_google_config(self):
        cfg = MagicMock()
        self.mod.set_google_config(cfg)
        assert self.mod._config is cfg


class TestEmailTool:

    @pytest.fixture(autouse=True)
    def setup(self):
        import micropaw.tools.email_tool as mod
        self.mod = mod
        yield

    async def test_authorize_action(self, context):
        with patch("micropaw.tools.email_tool.authorize", return_value="Google authorized successfully"):
            tool = self.mod.EmailTool()
            result = await tool.execute({"action": "authorize"}, context)
            assert "authorized" in result.lower()

    async def test_not_authorized(self, context):
        with patch("micropaw.tools.email_tool.ensure_token", return_value=(None, "Error: Not authorized.")):
            tool = self.mod.EmailTool()
            result = await tool.execute({"action": "inbox"}, context)
            assert "not authorized" in result.lower()

    async def test_inbox(self, context):
        with patch("micropaw.tools.email_tool.ensure_token", return_value=("tok", None)):
            with patch.object(self.mod, "_inbox", return_value="Recent emails (2):") as mock:
                tool = self.mod.EmailTool()
                result = await tool.execute({"action": "inbox", "count": 5}, context)
                mock.assert_called_once_with("tok", 5, "all")
                assert "Recent emails" in result

    async def test_inbox_count_clamped(self, context):
        with patch("micropaw.tools.email_tool.ensure_token", return_value=("tok", None)):
            with patch.object(self.mod, "_inbox", return_value="ok") as mock:
                tool = self.mod.EmailTool()
                await tool.execute({"action": "inbox", "count": 100}, context)
                mock.assert_called_once_with("tok", 50, "all")

    async def test_read_requires_message_id(self, context):
        with patch("micropaw.tools.email_tool.ensure_token", return_value=("tok", None)):
            tool = self.mod.EmailTool()
            result = await tool.execute({"action": "read"}, context)
            assert "requires" in result.lower()

    async def test_read(self, context):
        with patch("micropaw.tools.email_tool.ensure_token", return_value=("tok", None)):
            with patch.object(self.mod, "_read", return_value="From: a\nBody") as mock:
                tool = self.mod.EmailTool()
                result = await tool.execute({"action": "read", "message_id": "msg123"}, context)
                mock.assert_called_once_with("tok", "msg123")
                assert "From:" in result

    async def test_send_requires_to_and_body(self, context):
        with patch("micropaw.tools.email_tool.ensure_token", return_value=("tok", None)):
            tool = self.mod.EmailTool()
            result = await tool.execute({"action": "send", "to": "a@b.com"}, context)
            assert "requires" in result.lower()

    async def test_send(self, context):
        with patch("micropaw.tools.email_tool.ensure_token", return_value=("tok", None)):
            with patch.object(self.mod, "_send", return_value="Email sent to a@b.com") as mock:
                tool = self.mod.EmailTool()
                result = await tool.execute(
                    {"action": "send", "to": "a@b.com", "subject": "Hi", "body": "Hello"},
                    context,
                )
                mock.assert_called_once_with("tok", "a@b.com", "Hi", "Hello")
                assert "sent" in result.lower()

    async def test_search_requires_query(self, context):
        with patch("micropaw.tools.email_tool.ensure_token", return_value=("tok", None)):
            tool = self.mod.EmailTool()
            result = await tool.execute({"action": "search"}, context)
            assert "requires" in result.lower()

    async def test_search(self, context):
        with patch("micropaw.tools.email_tool.ensure_token", return_value=("tok", None)):
            with patch.object(self.mod, "_search", return_value="Search results (1 messages):") as mock:
                tool = self.mod.EmailTool()
                result = await tool.execute({"action": "search", "query": "from:alice"}, context)
                mock.assert_called_once_with("tok", "from:alice")

    async def test_unknown_action(self, context):
        with patch("micropaw.tools.email_tool.ensure_token", return_value=("tok", None)):
            tool = self.mod.EmailTool()
            result = await tool.execute({"action": "forward"}, context)
            assert "unknown" in result.lower()

    def test_strip_html(self):
        assert "hello" in self.mod._strip_html("<p>hello</p>")
        assert "<" not in self.mod._strip_html("<b>bold</b>")

    def test_b64_decode(self):
        import base64
        encoded = base64.urlsafe_b64encode(b"test message").decode().rstrip("=")
        assert self.mod._b64_decode(encoded) == "test message"

    def test_extract_body_plain(self):
        import base64
        data = base64.urlsafe_b64encode(b"Hello world").decode().rstrip("=")
        payload = {"mimeType": "text/plain", "body": {"data": data}}
        assert self.mod._extract_body(payload) == "Hello world"

    def test_extract_body_multipart(self):
        import base64
        data = base64.urlsafe_b64encode(b"Body text").decode().rstrip("=")
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": data}},
                {"mimeType": "text/html", "body": {"data": ""}},
            ],
        }
        assert self.mod._extract_body(payload) == "Body text"

    def test_get_header(self):
        headers = [
            {"name": "From", "value": "alice@example.com"},
            {"name": "Subject", "value": "Test"},
        ]
        assert self.mod._get_header(headers, "From") == "alice@example.com"
        assert self.mod._get_header(headers, "Missing") == ""


class TestCalendarTool:

    @pytest.fixture(autouse=True)
    def setup(self):
        import micropaw.tools.calendar_tool as mod
        self.mod = mod
        yield

    async def test_authorize_action(self, context):
        with patch("micropaw.tools.calendar_tool.authorize", return_value="Google authorized successfully"):
            tool = self.mod.CalendarTool()
            result = await tool.execute({"action": "authorize"}, context)
            assert "authorized" in result.lower()

    async def test_list_requires_token(self, context):
        with patch("micropaw.tools.calendar_tool.ensure_token", return_value=(None, "Error: Not authorized.")):
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
        with patch("micropaw.tools.calendar_tool.ensure_token", return_value=("tok", None)):
            with patch("micropaw.tools.calendar_tool.api_get", return_value=mock_data):
                tool = self.mod.CalendarTool()
                result = await tool.execute({"action": "list", "start_date": "2026-02-20"}, context)
                assert "Team Sync" in result

    async def test_create_requires_title(self, context):
        with patch("micropaw.tools.calendar_tool.ensure_token", return_value=("tok", None)):
            tool = self.mod.CalendarTool()
            result = await tool.execute({"action": "create"}, context)
            assert "requires" in result.lower()

    async def test_create_event(self, context):
        mock_resp = {"summary": "Meeting", "id": "ev456", "htmlLink": "https://cal.google.com/ev456"}
        with patch("micropaw.tools.calendar_tool.ensure_token", return_value=("tok", None)):
            with patch("micropaw.tools.calendar_tool.api_post", return_value=mock_resp):
                tool = self.mod.CalendarTool()
                result = await tool.execute(
                    {"action": "create", "title": "Meeting", "start_time": "2026-02-20T10:00:00", "end_time": "2026-02-20T11:00:00"},
                    context,
                )
                assert "created" in result.lower()
                assert "ev456" in result

    async def test_create_allday(self, context):
        mock_resp = {"summary": "Holiday", "id": "ev789", "htmlLink": "https://cal.google.com/ev789"}
        with patch("micropaw.tools.calendar_tool.ensure_token", return_value=("tok", None)):
            with patch("micropaw.tools.calendar_tool.api_post", return_value=mock_resp):
                tool = self.mod.CalendarTool()
                result = await tool.execute(
                    {"action": "create", "title": "Holiday", "all_day": True, "start_date": "2026-03-01"},
                    context,
                )
                assert "created" in result.lower()

    async def test_delete_requires_event_id(self, context):
        with patch("micropaw.tools.calendar_tool.ensure_token", return_value=("tok", None)):
            tool = self.mod.CalendarTool()
            result = await tool.execute({"action": "delete"}, context)
            assert "requires" in result.lower()

    async def test_delete_event(self, context):
        with patch("micropaw.tools.calendar_tool.ensure_token", return_value=("tok", None)):
            with patch("micropaw.tools.calendar_tool.api_delete", return_value=204):
                tool = self.mod.CalendarTool()
                result = await tool.execute({"action": "delete", "event_id": "ev123"}, context)
                assert "deleted" in result.lower()

    async def test_search_requires_query(self, context):
        with patch("micropaw.tools.calendar_tool.ensure_token", return_value=("tok", None)):
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
        with patch("micropaw.tools.calendar_tool.ensure_token", return_value=("tok", None)):
            with patch("micropaw.tools.calendar_tool.api_get", return_value=mock_data):
                tool = self.mod.CalendarTool()
                result = await tool.execute({"action": "search", "query": "budget"}, context)
                assert "Budget Review" in result

    async def test_unknown_action(self, context):
        with patch("micropaw.tools.calendar_tool.ensure_token", return_value=("tok", None)):
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


class TestGoogleConfig:

    def test_google_defaults(self):
        from micropaw.config import GoogleConfig
        cfg = GoogleConfig()
        assert not cfg.enabled
        assert cfg.client_id == ""
        assert cfg.gmail_enabled is True
        assert cfg.calendar_enabled is True

    def test_config_has_google(self):
        from micropaw.config import Config
        cfg = Config()
        assert hasattr(cfg, "google")
        assert not hasattr(cfg, "email")
        assert not hasattr(cfg, "calendar")

    def test_google_secret_redacted(self):
        from micropaw.config import Config, display_config
        cfg = Config()
        cfg.google.client_secret = "super-secret-value-here"
        data = display_config(cfg)
        assert data["google"]["client_secret"] != "super-secret-value-here"
        assert "..." in data["google"]["client_secret"]

    def test_config_migration(self, tmp_path):
        """Old email+calendar config migrates to google config."""
        from micropaw.config import Config
        raw = {
            "calendar": {"enabled": True, "client_id": "cid", "client_secret": "csecret"},
            "email": {"enabled": True},
        }
        # Simulate migration logic
        if ("calendar" in raw or "email" in raw) and "google" not in raw:
            cal = raw.pop("calendar", {})
            email_cfg = raw.pop("email", {})
            raw["google"] = {
                "enabled": cal.get("enabled", False) or email_cfg.get("enabled", False),
                "client_id": cal.get("client_id", ""),
                "client_secret": cal.get("client_secret", ""),
                "gmail_enabled": email_cfg.get("enabled", False),
                "calendar_enabled": cal.get("enabled", False),
            }
        cfg = Config(**raw)
        assert cfg.google.enabled is True
        assert cfg.google.client_id == "cid"
        assert cfg.google.gmail_enabled is True
        assert cfg.google.calendar_enabled is True
