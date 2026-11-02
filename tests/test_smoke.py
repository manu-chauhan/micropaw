"""Integration smoke tests for Web Capabilities Phase 1.

Tests real code paths (not mocks) for:
1. Location context in system prompt
2. ToolResult + MediaItem types
3. Web browse actions with Playwright (skipped if not installed)
4. Browser session management
5. Agent handling of ToolResult with media

Run: python -m pytest tests/test_smoke.py -v
"""

from __future__ import annotations

import pytest

from micropaw.config import Config
from micropaw.providers.base import AIResponse, MediaItem, Message, ToolCall
from micropaw.tools.base import ToolContext, ToolResult

# ---------------------------------------------------------------------------
# 1. Location context
# ---------------------------------------------------------------------------


class TestLocationContext:
    def _make_agent(self, location: str = ""):
        """Create an Agent with optional location set."""
        from unittest.mock import AsyncMock, MagicMock

        from micropaw.core.agent import Agent
        from micropaw.tools.base import ToolRegistry

        config = Config()
        config.persona.location = location

        provider = MagicMock()
        provider.chat = AsyncMock(return_value=AIResponse(
            content="ok", tool_calls=[], usage={"input": 5, "output": 2},
        ))
        registry = ToolRegistry()
        store = MagicMock()
        store.get_or_create_conversation = AsyncMock(return_value="conv1")
        store.get_messages = AsyncMock(return_value=[])
        store.save_message = AsyncMock()
        store.get_recent_memories = AsyncMock(return_value=[])
        store.search_documents = AsyncMock(return_value=[])
        store.is_marked = AsyncMock(return_value=False)
        store.set_marked = AsyncMock()

        agent = Agent(config, provider, registry, store)
        return agent

    def test_location_in_system_prompt(self):
        agent = self._make_agent("San Francisco, CA")
        prompt = agent._build_system_prompt("terminal")
        assert "San Francisco, CA" in prompt
        assert "near me" in prompt.lower()

    def test_no_location_no_mention(self):
        agent = self._make_agent("")
        prompt = agent._build_system_prompt("terminal")
        assert "User's location" not in prompt

    def test_location_from_env(self, monkeypatch):
        """Env override applies via load_config(), not bare Config()."""
        from micropaw.config import _apply_env_overrides
        monkeypatch.setenv("MICROPAW_LOCATION", "Tokyo, Japan")
        raw = _apply_env_overrides({})
        assert raw["persona"]["location"] == "Tokyo, Japan"


# ---------------------------------------------------------------------------
# 2. ToolResult + MediaItem
# ---------------------------------------------------------------------------


class TestToolResultType:
    def test_toolresult_with_media(self):
        media = MediaItem(type="image", data=b"\x89PNG fake", mime_type="image/png")
        result = ToolResult(text="Screenshot taken", media=[media])
        assert result.text == "Screenshot taken"
        assert len(result.media) == 1
        assert result.media[0].mime_type == "image/png"
        assert result.media[0].data == b"\x89PNG fake"

    def test_toolresult_without_media(self):
        result = ToolResult(text="Just text")
        assert result.media == []

    def test_toolresult_multiple_media(self):
        items = [
            MediaItem(type="image", data=b"img1", mime_type="image/png"),
            MediaItem(type="image", data=b"img2", mime_type="image/jpeg"),
        ]
        result = ToolResult(text="Two images", media=items)
        assert len(result.media) == 2

    @pytest.mark.asyncio
    async def test_agent_handles_toolresult_with_media(self):
        """Agent correctly extracts text + media from ToolResult."""
        from unittest.mock import AsyncMock, MagicMock

        from micropaw.core.agent import Agent
        from micropaw.tools.base import Tool, ToolRegistry

        class ScreenshotTool(Tool):
            name = "screenshot"
            description = "Take a screenshot"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, args, context):
                return ToolResult(
                    text="Screenshot of example.com",
                    media=[MediaItem(type="image", data=b"\x89PNG", mime_type="image/png")],
                )

        config = Config()
        registry = ToolRegistry()
        registry.register(ScreenshotTool())

        # Provider returns tool call on first chat, then text on second
        call_count = 0

        async def mock_chat(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AIResponse(
                    content="",
                    tool_calls=[ToolCall(id="tc1", name="screenshot", arguments={})],
                    usage={"input": 10, "output": 5},
                )
            return AIResponse(
                content="I see a webpage", tool_calls=[],
                usage={"input": 10, "output": 5},
            )

        provider = MagicMock()
        provider.chat = AsyncMock(side_effect=mock_chat)

        store = MagicMock()
        store.get_or_create_conversation = AsyncMock(return_value="conv1")
        store.get_messages = AsyncMock(return_value=[])
        store.save_message = AsyncMock()
        store.get_recent_memories = AsyncMock(return_value=[])
        store.search_documents = AsyncMock(return_value=[])
        store.is_marked = AsyncMock(return_value=False)
        store.set_marked = AsyncMock()
        store.ingest_document = AsyncMock(return_value=0)
        store.get_summary = AsyncMock(return_value=None)
        store.compact_messages = AsyncMock()

        agent = Agent(config, provider, registry, store)
        result = await agent.handle_message("take screenshot", "u1", "terminal")
        assert result == "I see a webpage"
        assert call_count == 2  # tool call + follow-up


# ---------------------------------------------------------------------------
# 3. Claude provider image encoding
# ---------------------------------------------------------------------------


class TestClaudeImageBlock:
    def test_image_block_encoding(self):
        import base64

        from micropaw.providers.claude import ClaudeProvider

        media = MediaItem(type="image", data=b"\x89PNG test", mime_type="image/png")
        block = ClaudeProvider._image_block(media)
        assert block["type"] == "image"
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "image/png"
        assert block["source"]["data"] == base64.b64encode(b"\x89PNG test").decode()

    def test_build_body_with_tool_result_images(self):
        from micropaw.providers.claude import ClaudeProvider

        provider = ClaudeProvider(api_key="test", model="test")
        messages = [
            Message(role="system", content="You are helpful."),
            Message(role="user", content="take screenshot"),
            Message(
                role="assistant", content="",
                tool_calls=[ToolCall(id="tc1", name="screenshot", arguments={})],
            ),
            Message(
                role="tool", content="Screenshot taken",
                tool_call_id="tc1",
                media=[MediaItem(type="image", data=b"\x89PNG", mime_type="image/png")],
            ),
        ]
        body = provider._build_body(messages, tools=None)
        # Find the tool_result message
        tool_results = [m for m in body["messages"]
                        if m["role"] == "user" and isinstance(m["content"], list)
                        and any(c.get("type") == "tool_result" for c in m["content"])]
        assert len(tool_results) == 1
        tr_content = tool_results[0]["content"][0]["content"]
        # Should have image block + text block
        types = [c["type"] for c in tr_content]
        assert "image" in types
        assert "text" in types

    def test_build_body_user_message_with_images(self):
        from micropaw.providers.claude import ClaudeProvider

        provider = ClaudeProvider(api_key="test", model="test")
        messages = [
            Message(
                role="user", content="What's in this image?",
                media=[MediaItem(type="image", data=b"\x89PNG", mime_type="image/png")],
            ),
        ]
        body = provider._build_body(messages, tools=None)
        user_msg = body["messages"][0]
        assert isinstance(user_msg["content"], list)
        types = [c["type"] for c in user_msg["content"]]
        assert "image" in types
        assert "text" in types


# ---------------------------------------------------------------------------
# 4. Web browse (requires Playwright + installed browsers)
# ---------------------------------------------------------------------------

def _has_playwright_browsers() -> bool:
    """Check if Playwright is installed AND browsers are available."""
    try:
        import subprocess
        check_script = (
            "from playwright.sync_api import sync_playwright;"
            "b=sync_playwright().start();b.chromium.launch(headless=True).close();b.stop()"
        )
        result = subprocess.run(
            ["python", "-c", check_script],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False

HAS_BROWSERS = _has_playwright_browsers()

skip_no_browsers = pytest.mark.skipif(
    not HAS_BROWSERS, reason="Playwright browsers not installed (run: playwright install chromium)",
)


@skip_no_browsers
class TestWebBrowseLive:
    """Live browser tests — require playwright + chromium installed."""

    @pytest.fixture
    def ctx(self):
        return ToolContext(
            conversation_id="smoke_test",
            user_id="tester",
            platform="terminal",
        )

    @pytest.mark.asyncio
    async def test_browse_extracts_text(self, ctx):
        from micropaw.tools.web_browse import WebBrowseTool, close_session

        tool = WebBrowseTool()
        try:
            result = await tool.execute(
                {"action": "browse", "url": "https://example.com", "extract": "text"},
                ctx,
            )
            assert isinstance(result, str)
            assert "Example Domain" in result
        finally:
            await close_session(ctx.conversation_id)

    @pytest.mark.asyncio
    async def test_browse_title(self, ctx):
        from micropaw.tools.web_browse import WebBrowseTool, close_session

        tool = WebBrowseTool()
        try:
            result = await tool.execute(
                {"action": "browse", "url": "https://example.com", "extract": "title"},
                ctx,
            )
            assert "Example Domain" in result
        finally:
            await close_session(ctx.conversation_id)

    @pytest.mark.asyncio
    async def test_screenshot_returns_toolresult(self, ctx):
        from micropaw.tools.web_browse import WebBrowseTool, close_session

        tool = WebBrowseTool()
        try:
            await tool.execute(
                {"action": "browse", "url": "https://example.com", "extract": "text"},
                ctx,
            )
            result = await tool.execute({"action": "screenshot"}, ctx)
            assert isinstance(result, ToolResult)
            assert len(result.media) == 1
            assert result.media[0].mime_type == "image/png"
            assert len(result.media[0].data) > 100  # real PNG bytes
        finally:
            await close_session(ctx.conversation_id)

    @pytest.mark.asyncio
    async def test_scroll_action(self, ctx):
        from micropaw.tools.web_browse import WebBrowseTool, close_session

        tool = WebBrowseTool()
        try:
            await tool.execute(
                {"action": "browse", "url": "https://example.com", "extract": "text"},
                ctx,
            )
            result = await tool.execute(
                {"action": "scroll", "direction": "down"}, ctx,
            )
            assert isinstance(result, str)
            assert "example" in result.lower()
        finally:
            await close_session(ctx.conversation_id)

    @pytest.mark.asyncio
    async def test_session_persists(self, ctx):
        """Session reuses the same page across actions."""
        from micropaw.tools.web_browse import WebBrowseTool, _sessions, close_session

        tool = WebBrowseTool()
        try:
            await tool.execute(
                {"action": "browse", "url": "https://example.com", "extract": "text"},
                ctx,
            )
            assert ctx.conversation_id in _sessions
            page1 = _sessions[ctx.conversation_id][1]

            # Second action reuses same page
            await tool.execute({"action": "scroll", "direction": "down"}, ctx)
            page2 = _sessions[ctx.conversation_id][1]
            assert page1 is page2
        finally:
            await close_session(ctx.conversation_id)

    @pytest.mark.asyncio
    async def test_close_session_cleans_up(self, ctx):
        from micropaw.tools.web_browse import WebBrowseTool, _sessions, close_session

        tool = WebBrowseTool()
        await tool.execute(
            {"action": "browse", "url": "https://example.com", "extract": "text"},
            ctx,
        )
        assert ctx.conversation_id in _sessions
        await close_session(ctx.conversation_id)
        assert ctx.conversation_id not in _sessions

    @pytest.mark.asyncio
    async def test_browse_disabled(self):
        from micropaw.tools.web_browse import WebBrowseTool

        tool = WebBrowseTool()
        ctx = ToolContext(
            conversation_id="no_browse",
            user_id="tester",
            platform="terminal",
            allow_web_browse=False,
        )
        result = await tool.execute(
            {"action": "browse", "url": "https://example.com"}, ctx,
        )
        assert "not enabled" in result.lower()


class TestWebBrowseNoPlaywright:
    """Tests that work without Playwright installed."""

    @pytest.mark.asyncio
    async def test_browse_disabled_returns_error(self):
        from micropaw.tools.web_browse import WebBrowseTool

        tool = WebBrowseTool()
        ctx = ToolContext(
            conversation_id="disabled",
            user_id="tester",
            platform="terminal",
            allow_web_browse=False,
        )
        result = await tool.execute(
            {"action": "browse", "url": "https://example.com"}, ctx,
        )
        assert "not enabled" in result.lower()

    def test_youtube_pattern_matches(self):
        from micropaw.tools.web_browse import _YT_PATTERN

        assert _YT_PATTERN.search("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert _YT_PATTERN.search("https://youtu.be/dQw4w9WgXcQ")
        assert not _YT_PATTERN.search("https://example.com")

    def test_tool_parameters_schema(self):
        from micropaw.tools.web_browse import WebBrowseTool

        tool = WebBrowseTool()
        params = tool.parameters
        assert "action" in params["properties"]
        actions = params["properties"]["action"]["enum"]
        assert set(actions) == {"browse", "click", "type", "scroll", "screenshot"}
