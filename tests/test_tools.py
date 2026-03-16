"""Tests for the tool system — registry, path security, chunk_text, web search, sub-agent."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from micropaw.channels.base import chunk_text
from micropaw.tools.base import Tool, ToolContext, ToolRegistry
from micropaw.tools.path_security import validate_file_path, validate_write_path

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
        from micropaw.tools.web_search import _search_searxng

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

        with patch("micropaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_searxng("python", 5, base_url="http://localhost:8888")

        assert result is not None
        assert "Python Docs" in result
        assert "python.org" in result
        assert "PyPI" in result

    @pytest.mark.asyncio
    async def test_searxng_returns_none_on_failure(self):
        from micropaw.tools.web_search import _search_searxng

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("micropaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_searxng("test", 5, base_url="http://localhost:8888")

        assert result is None

    @pytest.mark.asyncio
    async def test_brave_returns_formatted_results(self):
        from micropaw.tools.web_search import _search_brave

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

        with patch("micropaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_brave("test", 5, "BSA-fake-key")

        assert result is not None
        assert "Brave Result" in result
        assert "via Brave" in result

    @pytest.mark.asyncio
    async def test_brave_returns_none_on_rate_limit(self):
        import httpx

        from micropaw.tools.web_search import _search_brave

        mock_response = MagicMock()
        mock_response.status_code = 429
        error = httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=mock_response)

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=error)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("micropaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_brave("test", 5, "BSA-fake-key")

        assert result is None  # None allows fallthrough to next backend

    @pytest.mark.asyncio
    async def test_duckduckgo_always_returns_string(self):
        from micropaw.tools.web_search import _search_duckduckgo

        # DDG Lite returns HTML with result-link and result-snippet classes
        html = (
            '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FPython" '
            "class='result-link'>Python (programming language)</a>"
            "<td class='result-snippet'>Python is a high-level programming language.</td>"
        )
        mock_response = MagicMock()
        mock_response.text = html
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("micropaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_duckduckgo("python", 5)

        assert isinstance(result, str)
        assert "Python" in result

    @pytest.mark.asyncio
    async def test_duckduckgo_returns_no_results_message(self):
        from micropaw.tools.web_search import _search_duckduckgo

        # Empty HTML page — no result-link elements
        mock_response = MagicMock()
        mock_response.text = "<html><body>No results</body></html>"
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("micropaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_duckduckgo("xyznonexistent", 5)

        assert isinstance(result, str)
        assert "No results" in result

    @pytest.mark.asyncio
    async def test_tool_execute_fallback_chain(self):
        """Test that the tool falls back from SearXNG to DuckDuckGo."""
        from micropaw.tools.web_search import WebSearchTool, set_search_config

        # Set up config with auto provider (no brave key)
        mock_config = MagicMock()
        mock_config.provider = "auto"
        mock_config.brave_api_key = ""
        mock_config.searxng_url = ""
        set_search_config(mock_config)

        tool = WebSearchTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")

        # Mock SearXNG to fail, DDG to succeed
        with patch("micropaw.tools.web_search._search_searxng", AsyncMock(return_value=None)), \
             patch("micropaw.tools.web_search._search_duckduckgo", AsyncMock(return_value="DDG results")) as mock_ddg:
            result = await tool.execute({"query": "test"}, ctx)

        assert result == "DDG results"
        mock_ddg.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_execute_brave_with_key(self):
        """Test that Brave is used when API key is configured."""
        from micropaw.tools.web_search import WebSearchTool, set_search_config

        mock_config = MagicMock()
        mock_config.provider = "auto"
        mock_config.brave_api_key = "BSA-test-key"
        mock_config.searxng_url = ""
        set_search_config(mock_config)

        tool = WebSearchTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")

        with patch("micropaw.tools.web_search._search_brave", AsyncMock(return_value="Brave results")) as mock_brave:
            result = await tool.execute({"query": "test"}, ctx)

        assert result == "Brave results"
        mock_brave.assert_called_once_with("test", 5, "BSA-test-key")

    @pytest.mark.asyncio
    async def test_searxng_source_uses_safe_parsing(self):
        """Source name should be extracted safely even with unusual URLs."""
        from micropaw.tools.web_search import _search_searxng

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [{"title": "Test", "url": "https://example.com", "content": "result"}]
        }
        mock_response.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("micropaw.tools.web_search.httpx.AsyncClient", return_value=mock_client):
            result = await _search_searxng("test", 5, base_url="https://search.example.com")

        assert result is not None
        assert "search.example.com" in result

    @pytest.mark.asyncio
    async def test_max_results_clamped_to_20(self):
        """max_results should be capped at 20 even if LLM requests more."""
        from micropaw.tools.web_search import WebSearchTool, set_search_config

        mock_config = MagicMock()
        mock_config.provider = "duckduckgo"
        mock_config.brave_api_key = ""
        mock_config.searxng_url = ""
        set_search_config(mock_config)

        tool = WebSearchTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")

        with patch("micropaw.tools.web_search._search_duckduckgo", AsyncMock(return_value="results")) as mock_ddg:
            await tool.execute({"query": "test", "max_results": 1000}, ctx)

        # Verify max_results was clamped to 20
        mock_ddg.assert_called_once_with("test", 20)


# ---------------------------------------------------------------------------
# Sub-agent tests
# ---------------------------------------------------------------------------

class TestSubAgent:
    @pytest.mark.asyncio
    async def test_inline_returns_text(self):
        from micropaw.core.subagent import SubAgent
        from micropaw.providers.base import AIResponse

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(return_value=AIResponse(
            content="The answer is 42.", tool_calls=[], usage={"input": 10, "output": 5},
        ))

        agent = SubAgent(mock_provider)
        result = await agent.run("What is the meaning of life?")

        assert result == "The answer is 42."
        mock_provider.chat.assert_called_once()
        # No registry → tools=None
        _, kwargs = mock_provider.chat.call_args
        assert kwargs.get("tools") is None

    @pytest.mark.asyncio
    async def test_with_tools_excludes_delegate(self):
        """Sub-agent with a registry should get all tools except delegate."""
        from micropaw.core.subagent import SubAgent
        from micropaw.providers.base import AIResponse

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(return_value=AIResponse(
            content="Done.", tool_calls=[], usage={"input": 10, "output": 5},
        ))

        # Build a registry with a regular tool and a delegate tool
        from micropaw.tools.delegate import DelegateTool
        reg = ToolRegistry()
        reg.register(FakeTool())
        reg.register(DelegateTool())

        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
        agent = SubAgent(mock_provider, registry=reg, tool_context=ctx)
        result = await agent.run("Use tools")

        assert result == "Done."
        _, kwargs = mock_provider.chat.call_args
        tool_defs = kwargs.get("tools")
        assert tool_defs is not None
        tool_names = [t.name for t in tool_defs]
        assert "test_tool" in tool_names
        assert "delegate" not in tool_names  # excluded!

    @pytest.mark.asyncio
    async def test_tool_call_executed(self):
        """Sub-agent should execute tool calls and continue."""
        from micropaw.core.subagent import SubAgent
        from micropaw.providers.base import AIResponse, ToolCall

        call_count = 0

        async def mock_chat(messages, tools=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return AIResponse(
                    content="",
                    tool_calls=[ToolCall(id="tc1", name="test_tool", arguments={"text": "hello"})],
                    usage={"input": 5, "output": 3},
                )
            return AIResponse(content="Got: result: hello", tool_calls=[], usage={"input": 10, "output": 5})

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(side_effect=mock_chat)

        reg = ToolRegistry()
        reg.register(FakeTool())
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")

        agent = SubAgent(mock_provider, registry=reg, tool_context=ctx)
        result = await agent.run("Do something with test_tool")

        assert "result: hello" in result
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_max_iterations_bail(self):
        from micropaw.core.subagent import SubAgent
        from micropaw.providers.base import AIResponse

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(return_value=AIResponse(
            content="", tool_calls=[], usage={"input": 5, "output": 0},
        ))

        agent = SubAgent(mock_provider, max_iterations=3)
        result = await agent.run("Do something")

        assert "maximum iterations" in result.lower()

    @pytest.mark.asyncio
    async def test_custom_max_iterations(self):
        from micropaw.core.subagent import SubAgent
        from micropaw.providers.base import AIResponse

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(return_value=AIResponse(
            content="", tool_calls=[], usage={"input": 5, "output": 0},
        ))

        agent = SubAgent(mock_provider, max_iterations=2)
        await agent.run("Do something")
        # With nudging, each empty response adds 2 messages then retries → 2 calls
        assert mock_provider.chat.call_count == 2

    @pytest.mark.asyncio
    async def test_provider_error_handled(self):
        from micropaw.core.subagent import SubAgent

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(side_effect=RuntimeError("API down"))

        agent = SubAgent(mock_provider)
        result = await agent.run("Test task")

        assert "error" in result.lower()
        assert "API down" in result


class TestDelegateTool:
    @pytest.mark.asyncio
    async def test_inline_mode(self):
        from micropaw.providers.base import AIResponse
        from micropaw.tools.delegate import DelegateTool, set_delegate_deps

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
        from micropaw.tools.delegate import DelegateTool, set_delegate_deps

        mock_provider = AsyncMock()
        set_delegate_deps(mock_provider, None)

        tool = DelegateTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
        result = await tool.execute({"task": "Background task", "mode": "background"}, ctx)

        assert "error" in result.lower()
        assert "bus" in result.lower()

    @pytest.mark.asyncio
    async def test_background_passes_channel_directly(self):
        """Background delegation should pass platform as channel name, not parse from chat_id."""
        from micropaw.bus import MessageBus
        from micropaw.providers.base import AIResponse
        from micropaw.tools.delegate import _run_background, set_delegate_deps

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(return_value=AIResponse(
            content="Done", tool_calls=[], usage={"input": 5, "output": 5},
        ))

        bus = MessageBus()
        set_delegate_deps(mock_provider, bus)

        from micropaw.core.subagent import SubAgent
        agent = SubAgent(mock_provider)
        await _run_background(agent, "test task", "discord", "discord:channel:123")

        msg = await bus.consume_outbound()
        assert msg is not None
        assert msg.channel == "discord"
        assert msg.chat_id == "discord:channel:123"
        bus.close()

    @pytest.mark.asyncio
    async def test_subagent_gets_registry(self):
        """Delegate should pass registry to sub-agent so it can use tools."""
        from micropaw.providers.base import AIResponse
        from micropaw.tools.delegate import DelegateTool, set_delegate_deps

        mock_provider = AsyncMock()
        mock_provider.chat = AsyncMock(return_value=AIResponse(
            content="Used tools", tool_calls=[], usage={"input": 10, "output": 5},
        ))

        reg = ToolRegistry()
        reg.register(FakeTool())
        set_delegate_deps(mock_provider, None, registry=reg, max_iterations=5)

        tool = DelegateTool()
        ctx = ToolContext(conversation_id="conv1", user_id="u1", platform="test")
        result = await tool.execute({"task": "Do something"}, ctx)

        assert result == "Used tools"
        # Verify tools were passed (not None)
        _, kwargs = mock_provider.chat.call_args
        assert kwargs.get("tools") is not None


# ---------------------------------------------------------------------------
# RAG: chunker tests
# ---------------------------------------------------------------------------

class TestChunker:
    def test_short_text_single_chunk(self):
        from micropaw.memory.chunker import chunk_text
        assert chunk_text("hello world") == ["hello world"]

    def test_empty_returns_empty(self):
        from micropaw.memory.chunker import chunk_text
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_splits_long_text(self):
        from micropaw.memory.chunker import chunk_text
        text = "word " * 1000  # ~5000 chars
        chunks = chunk_text(text, max_chars=500, overlap=50)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 500

    def test_overlap_preserves_context(self):
        from micropaw.memory.chunker import chunk_text
        text = "A" * 200 + " " + "B" * 200 + " " + "C" * 200
        chunks = chunk_text(text, max_chars=250, overlap=50)
        assert len(chunks) >= 2
        # Overlap means end of chunk N appears in start of chunk N+1
        if len(chunks) >= 2:
            end_of_first = chunks[0][-30:]
            assert any(end_of_first[i:i+10] in chunks[1] for i in range(len(end_of_first) - 10))

    def test_prefers_paragraph_breaks(self):
        from micropaw.memory.chunker import chunk_text
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        chunks = chunk_text(text, max_chars=30, overlap=5)
        assert any("First paragraph." in c for c in chunks)


# ---------------------------------------------------------------------------
# RAG: BLOB serialization tests
# ---------------------------------------------------------------------------

class TestBlobSerialization:
    def test_roundtrip(self):
        from micropaw.memory.embeddings import deserialize_embedding_blob, serialize_embedding_blob
        original = [0.1, 0.2, 0.3, 0.4, 0.5]
        blob = serialize_embedding_blob(original)
        assert isinstance(blob, bytes)
        result = deserialize_embedding_blob(blob)
        assert len(result) == len(original)
        for a, b in zip(original, result):
            assert abs(a - b) < 1e-6

    def test_empty_embedding(self):
        from micropaw.memory.embeddings import deserialize_embedding_blob, serialize_embedding_blob
        assert serialize_embedding_blob([]) is None
        assert deserialize_embedding_blob(None) == []
        assert deserialize_embedding_blob(b"") == []


# ---------------------------------------------------------------------------
# RAG: ingest + search roundtrip tests
# ---------------------------------------------------------------------------

class TestDocumentIngest:
    @pytest.mark.asyncio
    async def test_ingest_and_search(self, tmp_path):
        from micropaw.memory.embeddings import NullEmbedding
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "test.db"), NullEmbedding())
        await store.init()

        content = "Revenue grew 23% in Q4.\n\nExpenses were reduced by 10%.\n\nNet profit margin reached 18%."
        count = await store.ingest_document("/tmp/report.pdf", content, "user1")
        assert count >= 1

        # Text fallback search (NullEmbedding)
        results = await store.search_documents("revenue", limit=5, user_id="user1")
        assert len(results) >= 1
        assert any("Revenue" in r["content"] for r in results)
        assert results[0]["source"] == "/tmp/report.pdf"

        await store.close()

    @pytest.mark.asyncio
    async def test_reingest_replaces_old_chunks(self, tmp_path):
        from micropaw.memory.embeddings import NullEmbedding
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "test.db"), NullEmbedding())
        await store.init()

        await store.ingest_document("/tmp/file.txt", "Old content here", "user1")
        await store.ingest_document("/tmp/file.txt", "New content replaced", "user1")

        results = await store.search_documents("content", limit=10, user_id="user1")
        contents = " ".join(r["content"] for r in results)
        assert "New content replaced" in contents
        assert "Old content" not in contents

        await store.close()

    @pytest.mark.asyncio
    async def test_empty_content_not_ingested(self, tmp_path):
        from micropaw.memory.embeddings import NullEmbedding
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "test.db"), NullEmbedding())
        await store.init()

        count = await store.ingest_document("/tmp/empty.txt", "", "user1")
        assert count == 0

        await store.close()

    @pytest.mark.asyncio
    async def test_cross_user_isolation(self, tmp_path):
        from micropaw.memory.embeddings import NullEmbedding
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "test.db"), NullEmbedding())
        await store.init()

        await store.ingest_document("/tmp/secret.pdf", "Classified information here", "alice")
        results = await store.search_documents("classified", limit=5, user_id="bob")
        assert len(results) == 0

        await store.close()


# ---------------------------------------------------------------------------
# YouTube transcript tests
# ---------------------------------------------------------------------------

class TestYouTubePattern:
    def test_watch_url(self):
        from micropaw.tools.web_browse import _YT_PATTERN
        m = _YT_PATTERN.search("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert m and m.group(1) == "dQw4w9WgXcQ"

    def test_short_url(self):
        from micropaw.tools.web_browse import _YT_PATTERN
        m = _YT_PATTERN.search("https://youtu.be/dQw4w9WgXcQ")
        assert m and m.group(1) == "dQw4w9WgXcQ"

    def test_shorts_url(self):
        from micropaw.tools.web_browse import _YT_PATTERN
        m = _YT_PATTERN.search("https://youtube.com/shorts/dQw4w9WgXcQ")
        assert m and m.group(1) == "dQw4w9WgXcQ"

    def test_non_youtube_url(self):
        from micropaw.tools.web_browse import _YT_PATTERN
        m = _YT_PATTERN.search("https://example.com/page")
        assert m is None

    def test_url_with_extra_params(self):
        from micropaw.tools.web_browse import _YT_PATTERN
        m = _YT_PATTERN.search("https://www.youtube.com/watch?v=Tn6-PIqc4UM&t=120")
        assert m and m.group(1) == "Tn6-PIqc4UM"


class TestYouTubeTranscriptParsing:
    """Test XML parsing for both manual (<p>) and auto-generated (<text>) captions."""

    def test_parse_manual_captions(self):
        """Manual captions use <p> tags."""
        from html import unescape
        from xml.etree.ElementTree import fromstring

        xml = (
            '<?xml version="1.0" encoding="utf-8" ?><timedtext format="3"><body>'
            '<p t="1000" d="2000">Hello world</p>'
            '<p t="3000" d="2000">This is a test</p>'
            '</body></timedtext>'
        )
        root = fromstring(xml)
        segments = []
        for tag in ("p", "text"):
            for elem in root.iter(tag):
                text = unescape(elem.text or "").replace("\n", " ").strip()
                if text:
                    segments.append(text)
            if segments:
                break
        assert segments == ["Hello world", "This is a test"]

    def test_parse_autogen_captions(self):
        """Auto-generated captions use <text> tags."""
        from html import unescape
        from xml.etree.ElementTree import fromstring

        xml = (
            '<?xml version="1.0" encoding="utf-8" ?><transcript>'
            '<text start="0" dur="1.5">hello everyone</text>'
            '<text start="1.5" dur="2.0">welcome to the video</text>'
            '</transcript>'
        )
        root = fromstring(xml)
        segments = []
        for tag in ("p", "text"):
            for elem in root.iter(tag):
                text = unescape(elem.text or "").replace("\n", " ").strip()
                if text:
                    segments.append(text)
            if segments:
                break
        assert segments == ["hello everyone", "welcome to the video"]

    def test_html_entity_decoding(self):
        """Captions often contain HTML entities."""
        from html import unescape
        from xml.etree.ElementTree import fromstring

        xml = (
            '<?xml version="1.0" encoding="utf-8" ?><timedtext format="3"><body>'
            '<p t="0" d="1000">We&#39;re no strangers to love</p>'
            '</body></timedtext>'
        )
        root = fromstring(xml)
        text = unescape(list(root.iter("p"))[0].text)
        assert text == "We're no strangers to love"


class TestYouTubeTranscriptFetch:
    @pytest.mark.asyncio
    async def test_fetch_returns_structured_output(self):
        """Mock the full InnerTube flow and verify output structure."""
        from micropaw.tools.web_browse import fetch_youtube_transcript

        page_html = '<script>var x = {"INNERTUBE_API_KEY": "test_key_123"};</script>'
        player_json = {
            "videoDetails": {"title": "Test Video", "author": "Tester", "lengthSeconds": "125"},
            "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{
                "languageCode": "en",
                "baseUrl": "https://www.youtube.com/api/timedtext?v=test",
            }]}},
        }
        caption_xml = (
            '<?xml version="1.0" encoding="utf-8" ?><transcript>'
            '<text start="0" dur="5">Hello world this is a test transcript</text>'
            '</transcript>'
        )

        mock_responses = [
            MagicMock(text=page_html, status_code=200),   # page GET
            MagicMock(status_code=200),                     # player POST
            MagicMock(text=caption_xml, status_code=200),  # caption GET
        ]
        mock_responses[1].json.return_value = player_json

        with patch("micropaw.tools.web_browse.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=[mock_responses[0], mock_responses[2]])
            mock_client.post = AsyncMock(return_value=mock_responses[1])
            mock_client_cls.return_value = mock_client

            result = await fetch_youtube_transcript("test12345ab")

        assert "Title: Test Video" in result
        assert "Author: Tester" in result
        assert "Duration: 2:05" in result
        assert "Hello world this is a test transcript" in result

    @pytest.mark.asyncio
    async def test_no_captions_returns_message(self):
        """Videos without captions return a helpful message."""
        from micropaw.tools.web_browse import fetch_youtube_transcript

        page_html = '<script>var x = {"INNERTUBE_API_KEY": "key123"};</script>'
        player_json = {
            "videoDetails": {"title": "No Caps", "author": "Test", "lengthSeconds": "60"},
            "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": []}},
        }

        with patch("micropaw.tools.web_browse.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=MagicMock(text=page_html, status_code=200))
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200, **{"json.return_value": player_json}))
            mock_client_cls.return_value = mock_client

            result = await fetch_youtube_transcript("nocaps12345")

        assert "No captions available" in result


class TestWebBrowseYouTube:
    @pytest.mark.asyncio
    async def test_youtube_url_triggers_transcript(self):
        """web_browse with a YouTube URL should call fetch_youtube_transcript."""
        from micropaw.tools.web_browse import WebBrowseTool

        tool = WebBrowseTool()
        context = MagicMock()
        context.allow_web_browse = True

        with patch("micropaw.tools.web_browse.validate_url", return_value=None), \
             patch("micropaw.tools.web_browse.fetch_youtube_transcript", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = "Title: Test\n\nTranscript:\nHello world"
            result = await tool.execute({"url": "https://youtube.com/watch?v=abc12345678"}, context)

        assert "Hello world" in result
        mock_fetch.assert_called_once_with("abc12345678")

    @pytest.mark.asyncio
    async def test_non_youtube_skips_transcript(self):
        """Non-YouTube URLs should NOT trigger transcript fetch."""
        from micropaw.tools.web_browse import WebBrowseTool

        tool = WebBrowseTool()
        context = MagicMock()
        context.allow_web_browse = True

        with patch("micropaw.tools.web_browse.validate_url", return_value=None), \
             patch("micropaw.tools.web_browse.fetch_youtube_transcript", new_callable=AsyncMock) as mock_fetch, \
             patch("micropaw.tools.web_browse._get_browser") as mock_browser:
            # Make Playwright fail so we can verify the path taken
            mock_browser.side_effect = RuntimeError("test: should use playwright path")
            result = await tool.execute({"url": "https://example.com/article"}, context)

        mock_fetch.assert_not_called()
        assert "test: should use playwright path" in result


# ---------------------------------------------------------------------------
# sqlite-vec integration tests
# ---------------------------------------------------------------------------

class _FakeEmbedder:
    """Deterministic embedder for testing — returns 4-dim vectors."""
    name = "fake"
    dimensions = 4

    async def embed(self, text: str) -> list[float]:
        h = hash(text) & 0xFFFFFFFF
        return [(h >> (i * 8) & 0xFF) / 255.0 for i in range(4)]


class TestSqliteVec:
    @pytest.mark.asyncio
    async def test_vec_search_path(self, tmp_path):
        """Ingest + search uses sqlite-vec when available."""
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "vec.db"), _FakeEmbedder())
        await store.init()

        content = "Revenue grew 23% in Q4. " * 50  # Enough to chunk
        count = await store.ingest_document("/tmp/report.pdf", content, "user1")
        assert count >= 1

        results = await store.search_documents("revenue growth", limit=3, user_id="user1")
        assert len(results) >= 1
        assert results[0]["source"] == "/tmp/report.pdf"

        # Verify vec was used (if available)
        if store._has_vec:
            assert store._vec_dims == 4

        await store.close()

    @pytest.mark.asyncio
    async def test_vec_reingest_cascade_delete(self, tmp_path):
        """Re-ingesting a document removes old chunks from vec index too."""
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "vec.db"), _FakeEmbedder())
        await store.init()

        await store.ingest_document("/tmp/f.txt", "Old data here. " * 50, "u1")
        await store.ingest_document("/tmp/f.txt", "New data replaced. " * 50, "u1")

        results = await store.search_documents("data", limit=10, user_id="u1")
        contents = " ".join(r["content"] for r in results)
        assert "New data" in contents
        assert "Old data" not in contents

        await store.close()

    @pytest.mark.asyncio
    async def test_vec_cross_user_isolation(self, tmp_path):
        """Vec search respects user_id filtering."""
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "vec.db"), _FakeEmbedder())
        await store.init()

        await store.ingest_document("/tmp/secret.pdf", "Top secret info. " * 50, "alice")
        results = await store.search_documents("secret", limit=5, user_id="bob")
        assert len(results) == 0

        await store.close()

    @pytest.mark.asyncio
    async def test_kv_meta_tracks_dimensions(self, tmp_path):
        """kv_meta stores embedding dimensions for change detection."""
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "vec.db"), _FakeEmbedder())
        await store.init()

        stored = await store._get_meta("vec_dims")
        if store._has_vec:
            assert stored == "4"

        await store.close()

    @pytest.mark.asyncio
    async def test_brute_force_fallback(self, tmp_path):
        """When vec is disabled, brute-force cosine still works."""
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "bf.db"), _FakeEmbedder())
        await store.init()
        store._has_vec = False  # Force brute-force path

        content = "Machine learning is transforming healthcare. " * 50
        await store.ingest_document("/tmp/ml.pdf", content, "user1")

        results = await store.search_documents("machine learning", limit=3, user_id="user1")
        assert len(results) >= 1
        assert "Machine learning" in results[0]["content"]

        await store.close()

    @pytest.mark.asyncio
    async def test_null_embedding_text_fallback(self, tmp_path):
        """NullEmbedding falls through to text LIKE search."""
        from micropaw.memory.embeddings import NullEmbedding
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "null.db"), NullEmbedding())
        await store.init()
        assert not store._has_vec or store._vec_dims == 0

        content = "Quantum computing breakthroughs in 2025."
        await store.ingest_document("/tmp/quantum.txt", content, "user1")

        results = await store.search_documents("quantum", limit=3, user_id="user1")
        assert len(results) >= 1

        await store.close()

    @pytest.mark.asyncio
    async def test_search_memories_no_cap(self, tmp_path):
        """search_memories scans all memories without artificial row cap."""
        from micropaw.memory.embeddings import NullEmbedding
        from micropaw.memory.store import MemoryStore

        store = MemoryStore(str(tmp_path / "mem.db"), NullEmbedding())
        await store.init()

        # Save enough memories to exceed old 500 cap
        for i in range(10):
            await store.save_memory(f"fact_{i}", f"Value number {i}", user_id="u1")

        results = await store.search_memories("Value", limit=20, user_id="u1")
        assert len(results) >= 10

        await store.close()


# ---------------------------------------------------------------------------
# Shell blocking pattern tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Embedding auto-detect tests
# ---------------------------------------------------------------------------

class TestProbeOllama:
    @pytest.mark.asyncio
    async def test_probe_ollama_success(self):
        from micropaw.memory.embeddings import probe_ollama

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "models": [
                {"name": "nomic-embed-text:latest", "size": 274000000},
                {"name": "llama3.1:latest", "size": 4700000000},
            ]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("micropaw.memory.embeddings.httpx.AsyncClient", return_value=mock_client):
            assert await probe_ollama("http://localhost:11434") is True

    @pytest.mark.asyncio
    async def test_probe_ollama_model_missing(self):
        from micropaw.memory.embeddings import probe_ollama

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "models": [{"name": "llama3.1:latest"}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("micropaw.memory.embeddings.httpx.AsyncClient", return_value=mock_client):
            assert await probe_ollama("http://localhost:11434") is False

    @pytest.mark.asyncio
    async def test_probe_ollama_connection_refused(self):
        from micropaw.memory.embeddings import probe_ollama

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("micropaw.memory.embeddings.httpx.AsyncClient", return_value=mock_client):
            assert await probe_ollama("http://localhost:11434") is False


class TestEmbeddingConfig:
    def test_bool_true_coerced(self):
        from micropaw.config import EmbeddingConfig
        cfg = EmbeddingConfig(enabled=True)
        assert cfg.enabled == "true"

    def test_bool_false_coerced(self):
        from micropaw.config import EmbeddingConfig
        cfg = EmbeddingConfig(enabled=False)
        assert cfg.enabled == "false"

    def test_auto_default(self):
        from micropaw.config import EmbeddingConfig
        cfg = EmbeddingConfig()
        assert cfg.enabled == "auto"
        assert cfg.provider == "auto"
        assert cfg.model == ""

    def test_explicit_provider(self):
        from micropaw.config import EmbeddingConfig
        cfg = EmbeddingConfig(provider="ollama", model="snowflake-arctic-embed")
        assert cfg.provider == "ollama"
        assert cfg.model == "snowflake-arctic-embed"


class TestShellBlocking:
    def test_base64_pipe_to_shell_blocked(self):
        from micropaw.tools.shell import _BLOCKED_RE
        assert any(p.search("base64 -d payload.txt|bash") for p in _BLOCKED_RE)
        assert any(p.search("base64 -d payload.txt | sh") for p in _BLOCKED_RE)

    def test_xxd_pipe_to_shell_blocked(self):
        from micropaw.tools.shell import _BLOCKED_RE
        assert any(p.search("xxd -r -p payload.hex|bash") for p in _BLOCKED_RE)

    def test_process_substitution_blocked(self):
        from micropaw.tools.shell import _BLOCKED_RE
        assert any(p.search("diff <(cat /etc/shadow) <(echo)") for p in _BLOCKED_RE)

    def test_safe_commands_allowed(self):
        from micropaw.tools.shell import _BLOCKED_RE
        safe = ["ls -la", "cat README.md", "python3 main.py", "git status", "pip list"]
        for cmd in safe:
            assert not any(p.search(cmd) for p in _BLOCKED_RE), f"False positive: {cmd}"


# ---------------------------------------------------------------------------
# PDF out-of-bounds tests
# ---------------------------------------------------------------------------

class TestPdfParsing:
    def test_parse_pages_empty_on_out_of_bounds(self):
        from micropaw.tools.pdf_read import _parse_pages
        # PDF has 5 pages; request page 100
        pages = _parse_pages("100", 5)
        assert pages == []

    def test_parse_pages_valid(self):
        from micropaw.tools.pdf_read import _parse_pages
        pages = _parse_pages("1-3", 10)
        assert pages == [0, 1, 2]

    def test_parse_pages_mixed(self):
        from micropaw.tools.pdf_read import _parse_pages
        pages = _parse_pages("1,3,5", 10)
        assert pages == [0, 2, 4]


# ---------------------------------------------------------------------------
# Message bus tests
# ---------------------------------------------------------------------------

class TestMessageBus:
    @pytest.mark.asyncio
    async def test_closed_bus_consume_returns_none(self):
        from micropaw.bus import MessageBus
        bus = MessageBus()
        bus.close()
        assert await bus.consume_outbound() is None
        assert await bus.consume_inbound() is None

    @pytest.mark.asyncio
    async def test_publish_on_closed_bus_raises(self):
        from micropaw.bus import InboundMessage, MessageBus
        bus = MessageBus()
        bus.close()
        with pytest.raises(RuntimeError, match="closed"):
            await bus.publish_inbound(InboundMessage(
                channel="test", sender_id="u1", chat_id="c1",
                content="hi", session_key="test:c1",
            ))

    @pytest.mark.asyncio
    async def test_publish_and_consume_roundtrip(self):
        from micropaw.bus import MessageBus, OutboundMessage
        bus = MessageBus()
        await bus.publish_outbound(OutboundMessage(channel="discord", chat_id="ch1", content="hello"))
        msg = await bus.consume_outbound()
        assert msg is not None
        assert msg.channel == "discord"
        assert msg.content == "hello"
        bus.close()


# ---------------------------------------------------------------------------
# Store corrupt JSON tests
# ---------------------------------------------------------------------------

class TestStoreCorruptJson:
    @pytest.mark.asyncio
    async def test_corrupt_tool_calls_returns_empty_tools(self, tmp_path):
        from micropaw.memory.store import MemoryStore

        db_path = tmp_path / "test.db"
        store = MemoryStore(str(db_path))
        await store.init()

        # Insert a message with corrupt tool_calls JSON directly
        await store.db.execute(
            "INSERT INTO conversations (id, platform, user_id) VALUES (?, ?, ?)",
            ("conv1", "test", "user1"),
        )
        await store.db.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_calls) VALUES (?, ?, ?, ?)",
            ("conv1", "assistant", "test", "{corrupt json!!!}"),
        )
        await store.db.commit()

        messages = await store.get_messages("conv1")
        assert len(messages) == 1
        assert messages[0].content == "test"
        assert messages[0].tool_calls == []  # Corrupt JSON → empty list, no crash

        await store.close()

    @pytest.mark.asyncio
    async def test_valid_tool_calls_loaded(self, tmp_path):
        import json

        from micropaw.memory.store import MemoryStore

        db_path = tmp_path / "test.db"
        store = MemoryStore(str(db_path))
        await store.init()

        await store.db.execute(
            "INSERT INTO conversations (id, platform, user_id) VALUES (?, ?, ?)",
            ("conv1", "test", "user1"),
        )
        tool_calls_json = json.dumps([{"id": "tc1", "name": "web_search", "arguments": {"query": "test"}}])
        await store.db.execute(
            "INSERT INTO messages (conversation_id, role, content, tool_calls) VALUES (?, ?, ?, ?)",
            ("conv1", "assistant", "searching", tool_calls_json),
        )
        await store.db.commit()

        messages = await store.get_messages("conv1")
        assert len(messages) == 1
        assert len(messages[0].tool_calls) == 1
        assert messages[0].tool_calls[0].name == "web_search"

        await store.close()


# ---------------------------------------------------------------------------
# ChannelSendTool tests
# ---------------------------------------------------------------------------

class TestChannelSendTool:
    def _make_context(self) -> ToolContext:
        return ToolContext(conversation_id="dm:owner", user_id="user1", platform="terminal")

    @pytest.mark.asyncio
    async def test_send_to_known_channel(self):
        import micropaw.tools.channel_send as cs
        from micropaw.bus import MessageBus
        from micropaw.tools.channel_send import ChannelSendTool

        bus = MessageBus()
        cs._bus = bus
        cs._channel_registry = {"telegram": "telegram:12345"}

        tool = ChannelSendTool()
        result = await tool.execute({"channel": "telegram", "message": "Hello!"}, self._make_context())
        assert "sent to telegram" in result.lower()

        msg = await bus.consume_outbound()
        assert msg is not None
        assert msg.channel == "telegram"
        assert msg.chat_id == "telegram:12345"
        assert msg.content == "Hello!"
        bus.close()

    @pytest.mark.asyncio
    async def test_send_to_terminal(self):
        import micropaw.tools.channel_send as cs
        from micropaw.bus import MessageBus
        from micropaw.tools.channel_send import ChannelSendTool

        bus = MessageBus()
        cs._bus = bus
        cs._channel_registry = {"terminal": "terminal"}

        tool = ChannelSendTool()
        result = await tool.execute({"channel": "terminal", "message": "Hi terminal"}, self._make_context())
        assert "sent to terminal" in result.lower()

        msg = await bus.consume_outbound()
        assert msg is not None
        assert msg.channel == "terminal"
        assert msg.chat_id == ""
        bus.close()

    @pytest.mark.asyncio
    async def test_send_to_unknown_channel(self):
        import micropaw.tools.channel_send as cs
        from micropaw.bus import MessageBus
        from micropaw.tools.channel_send import ChannelSendTool

        bus = MessageBus()
        cs._bus = bus
        cs._channel_registry = {"terminal": "terminal"}

        tool = ChannelSendTool()
        result = await tool.execute({"channel": "slack", "message": "Hello"}, self._make_context())
        assert "error" in result.lower()
        assert "slack" in result.lower()
        bus.close()

    @pytest.mark.asyncio
    async def test_send_empty_message(self):
        import micropaw.tools.channel_send as cs
        from micropaw.bus import MessageBus
        from micropaw.tools.channel_send import ChannelSendTool

        bus = MessageBus()
        cs._bus = bus
        cs._channel_registry = {"telegram": "telegram:123"}

        tool = ChannelSendTool()
        result = await tool.execute({"channel": "telegram", "message": ""}, self._make_context())
        assert "error" in result.lower()
        bus.close()

    @pytest.mark.asyncio
    async def test_send_without_bus(self):
        import micropaw.tools.channel_send as cs
        from micropaw.tools.channel_send import ChannelSendTool

        cs._bus = None

        tool = ChannelSendTool()
        result = await tool.execute({"channel": "telegram", "message": "Hello"}, self._make_context())
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_get_active_channels(self):
        import micropaw.tools.channel_send as cs
        from micropaw.tools.channel_send import get_active_channels

        cs._channel_registry = {"telegram": "telegram:123", "terminal": "terminal"}
        active = get_active_channels()
        assert "telegram" in active
        assert "terminal" in active

    @pytest.mark.asyncio
    async def test_update_channel_chat_id(self):
        import micropaw.tools.channel_send as cs
        from micropaw.tools.channel_send import update_channel_chat_id

        cs._channel_registry = {}
        update_channel_chat_id("discord", "discord:789")
        assert cs._channel_registry["discord"] == "discord:789"
