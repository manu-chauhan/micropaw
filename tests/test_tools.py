"""Tests for the tool system — registry, path security, chunk_text, web search, sub-agent."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from microclaw.channels.base import chunk_text
from microclaw.tools.base import Tool, ToolContext, ToolRegistry
from microclaw.tools.path_security import validate_file_path, validate_write_path


# ---------------------------------------------------------------------------
# ToolRegistry tests
# ---------------------------------------------------------------------------

class FakeTool(Tool):
    name = "test_tool"
    description = "A test tool"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        return f"result: {args.get('text', '')}"


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        tool = FakeTool()
        reg.register(tool)
        assert reg.get("test_tool") is tool
        assert reg.get("nonexistent") is None

    def test_names(self):
        reg = ToolRegistry()
        reg.register(FakeTool())
        assert "test_tool" in reg.names

    def test_all(self):
        reg = ToolRegistry()
        reg.register(FakeTool())
        assert len(reg.all()) == 1

    def test_to_tool_defs(self):
        reg = ToolRegistry()
        reg.register(FakeTool())
        defs = reg.to_tool_defs()
        assert len(defs) == 1
        assert defs[0].name == "test_tool"


class TestToolExecution:
    async def test_execute(self):
        tool = FakeTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
        result = await tool.execute({"text": "hello"}, ctx)
        assert result == "result: hello"


# ---------------------------------------------------------------------------
# Path security tests
# ---------------------------------------------------------------------------

class TestPathSecurity:
    def test_normal_path_allowed(self):
        assert validate_file_path("/tmp/test.txt") is None

    def test_proc_blocked(self):
        err = validate_file_path("/proc/self/environ")
        assert err is not None
        assert "blocked" in err.lower()

    def test_sys_blocked(self):
        err = validate_file_path("/sys/kernel/version")
        assert err is not None

    def test_etc_shadow_blocked(self):
        err = validate_file_path("/etc/shadow")
        assert err is not None

    def test_ssh_dir_blocked(self):
        err = validate_file_path("/home/user/.ssh/id_rsa")
        assert err is not None
        assert "sensitive" in err.lower()

    def test_env_file_blocked(self):
        err = validate_file_path("/app/.env")
        assert err is not None

    def test_netrc_blocked(self):
        err = validate_file_path("/home/user/.netrc")
        assert err is not None


    def test_etc_passwd_blocked(self):
        err = validate_file_path("/etc/passwd")
        assert err is not None
        assert "protected" in err.lower()

    def test_etc_ssl_private_blocked(self):
        err = validate_file_path("/etc/ssl/private/server.key")
        assert err is not None
        assert "sensitive" in err.lower()

    def test_etc_ssh_blocked(self):
        err = validate_file_path("/etc/ssh/sshd_config")
        assert err is not None


class TestWritePathSecurity:
    def test_bashrc_blocked(self):
        err = validate_write_path("/home/user/.bashrc")
        assert err is not None
        assert "login script" in err.lower()

    def test_zshrc_blocked(self):
        err = validate_write_path("/home/user/.zshrc")
        assert err is not None

    def test_authorized_keys_blocked(self):
        err = validate_write_path("/home/user/.ssh/authorized_keys")
        assert err is not None

    def test_cron_blocked(self):
        err = validate_write_path("/etc/cron.d/malicious")
        assert err is not None

    def test_normal_write_allowed(self):
        assert validate_write_path("/tmp/output.txt") is None


# ---------------------------------------------------------------------------
# chunk_text tests
# ---------------------------------------------------------------------------

class TestChunkText:
    def test_short_text_single_chunk(self):
        assert chunk_text("hello", 100) == ["hello"]

    def test_exact_limit(self):
        text = "x" * 100
        assert chunk_text(text, 100) == [text]

    def test_splits_on_newline(self):
        text = "line1\nline2\nline3"
        chunks = chunk_text(text, 12)
        assert len(chunks) >= 2
        assert "line1" in chunks[0]

    def test_splits_without_newline(self):
        text = "a" * 10
        chunks = chunk_text(text, 3)
        assert len(chunks) >= 3
        for chunk in chunks:
            assert len(chunk) <= 3

    def test_empty_text(self):
        assert chunk_text("", 100) == [""]


# ---------------------------------------------------------------------------
# Web search tests
# ---------------------------------------------------------------------------

class TestWebSearch:
    """Tests for the multi-backend web search system."""

    @pytest.mark.asyncio
    async def test_searxng_returns_formatted_results(self):
        from microclaw.tools.web_search import _search_searxng

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"title": "Python Docs", "url": "https://python.org", "content": "Official docs"},
                {"title": "PyPI", "url": "https://pypi.org", "content": "Package index"},
            ]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("microclaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_searxng("python", 5, base_url="http://localhost:8888")

        assert result is not None
        assert "Python Docs" in result
        assert "python.org" in result
        assert "PyPI" in result

    @pytest.mark.asyncio
    async def test_searxng_returns_none_on_failure(self):
        from microclaw.tools.web_search import _search_searxng

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("microclaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_searxng("test", 5, base_url="http://localhost:8888")

        assert result is None

    @pytest.mark.asyncio
    async def test_brave_returns_formatted_results(self):
        from microclaw.tools.web_search import _search_brave

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "web": {
                "results": [
                    {"title": "Brave Result", "url": "https://brave.com", "description": "A result"},
                ]
            }
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("microclaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_brave("test", 5, "BSA-fake-key")

        assert result is not None
        assert "Brave Result" in result
        assert "via Brave" in result

    @pytest.mark.asyncio
    async def test_brave_returns_none_on_rate_limit(self):
        import httpx
        from microclaw.tools.web_search import _search_brave

        mock_response = MagicMock()
        mock_response.status_code = 429
        error = httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=mock_response)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=error)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("microclaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_brave("test", 5, "BSA-fake-key")

        assert result is None  # None allows fallthrough to next backend

    @pytest.mark.asyncio
    async def test_duckduckgo_always_returns_string(self):
        from microclaw.tools.web_search import _search_duckduckgo

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "AbstractText": "Python is a programming language.",
            "Heading": "Python",
            "AbstractURL": "https://en.wikipedia.org/wiki/Python",
            "RelatedTopics": [],
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("microclaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_duckduckgo("python", 5)

        assert isinstance(result, str)
        assert "Python" in result

    @pytest.mark.asyncio
    async def test_duckduckgo_returns_no_results_message(self):
        from microclaw.tools.web_search import _search_duckduckgo

        mock_response = MagicMock()
        mock_response.json.return_value = {"RelatedTopics": []}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("microclaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_duckduckgo("xyznonexistent", 5)

        assert isinstance(result, str)
        assert "No results" in result

    @pytest.mark.asyncio
    async def test_tool_execute_fallback_chain(self):
        """Test that the tool falls back from SearXNG to DuckDuckGo."""
        from microclaw.tools.web_search import WebSearchTool, set_search_config

        # Set up config with auto provider (no brave key)
        mock_config = MagicMock()
        mock_config.provider = "auto"
        mock_config.brave_api_key = ""
        mock_config.searxng_url = ""
        set_search_config(mock_config)

        tool = WebSearchTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")

        # Mock SearXNG to fail, DDG to succeed
        with patch("microclaw.tools.web_search._search_searxng", AsyncMock(return_value=None)), \
             patch("microclaw.tools.web_search._search_duckduckgo", AsyncMock(return_value="DDG results")) as mock_ddg:
            result = await tool.execute({"query": "test"}, ctx)

        assert result == "DDG results"
        mock_ddg.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_execute_brave_with_key(self):
        """Test that Brave is used when API key is configured."""
        from microclaw.tools.web_search import WebSearchTool, set_search_config

        mock_config = MagicMock()
        mock_config.provider = "auto"
        mock_config.brave_api_key = "BSA-test-key"
        mock_config.searxng_url = ""
        set_search_config(mock_config)

        tool = WebSearchTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")

        with patch("microclaw.tools.web_search._search_brave", AsyncMock(return_value="Brave results")) as mock_brave:
            result = await tool.execute({"query": "test"}, ctx)

        assert result == "Brave results"
        mock_brave.assert_called_once_with("test", 5, "BSA-test-key")


# ---------------------------------------------------------------------------
# Sub-agent tests
# ---------------------------------------------------------------------------

class TestSubAgent:
    @pytest.mark.asyncio
    async def test_inline_returns_text(self):
        from microclaw.core.subagent import SubAgent
        from microclaw.providers.base import AIResponse

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(return_value=AIResponse(
            content="The answer is 42.", tool_calls=[], usage={"input": 10, "output": 5},
        ))

        agent = SubAgent(mock_provider)
        result = await agent.run("What is the meaning of life?")

        assert result == "The answer is 42."
        mock_provider.chat.assert_called_once()
        # Verify tools=None (security: no tool access)
        _, kwargs = mock_provider.chat.call_args
        assert kwargs.get("tools") is None

    @pytest.mark.asyncio
    async def test_max_iterations_bail(self):
        from microclaw.core.subagent import SubAgent
        from microclaw.providers.base import AIResponse

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(return_value=AIResponse(
            content="", tool_calls=[], usage={"input": 5, "output": 0},
        ))

        agent = SubAgent(mock_provider)
        result = await agent.run("Do something")

        assert "maximum iterations" in result.lower()

    @pytest.mark.asyncio
    async def test_provider_error_handled(self):
        from microclaw.core.subagent import SubAgent

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(side_effect=RuntimeError("API down"))

        agent = SubAgent(mock_provider)
        result = await agent.run("Test task")

        assert "error" in result.lower()
        assert "API down" in result


class TestDelegateTool:
    @pytest.mark.asyncio
    async def test_inline_mode(self):
        from microclaw.providers.base import AIResponse
        from microclaw.tools.delegate import DelegateTool, set_delegate_deps

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(return_value=AIResponse(
            content="Delegated result", tool_calls=[], usage={"input": 10, "output": 5},
        ))
        set_delegate_deps(mock_provider, None)

        tool = DelegateTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
        result = await tool.execute({"task": "Summarize this"}, ctx)

        assert result == "Delegated result"

    @pytest.mark.asyncio
    async def test_background_without_bus_errors(self):
        from microclaw.providers.base import AIResponse
        from microclaw.tools.delegate import DelegateTool, set_delegate_deps

        mock_provider = AsyncMock()
        set_delegate_deps(mock_provider, None)

        tool = DelegateTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
        result = await tool.execute({"task": "Background task", "mode": "background"}, ctx)

        assert "error" in result.lower()
        assert "bus" in result.lower()
