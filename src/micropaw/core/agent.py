"""Main agent loop — the brain. Receives messages, thinks, uses tools, replies."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx

from micropaw.config import Config
from micropaw.core.context import ContextManager
from micropaw.core.conversation import ConversationManager
from micropaw.memory.store import MemoryStore
from micropaw.providers.base import AIProvider, AIResponse, Message
from micropaw.tools.base import ToolContext, ToolRegistry

logger = logging.getLogger(__name__)

# Tools that are safe to cache (read-only, no side effects)
_CACHEABLE_TOOLS = frozenset({"web_search", "web_browse", "file_read", "http_request", "pdf_read", "vision"})
# Tools whose output should be auto-ingested for RAG
_INGEST_TOOLS = frozenset({"file_read", "pdf_read", "web_browse"})
_MIN_INGEST_SIZE = 500  # only ingest results larger than this


def _cache_key(tool_name: str, args: dict) -> tuple:
    """Build a hashable cache key from tool name and arguments."""
    def _freeze(obj):
        if isinstance(obj, dict):
            return tuple(sorted((k, _freeze(v)) for k, v in obj.items()))
        if isinstance(obj, (list, tuple)):
            return tuple(_freeze(v) for v in obj)
        return obj
    return (tool_name, _freeze(args))


class Agent:
    """Core agent loop: chat with the LLM, execute tools, repeat until done."""

    def __init__(
        self,
        config: Config,
        provider: AIProvider,
        registry: ToolRegistry,
        store: MemoryStore,
    ) -> None:
        self.config = config
        self.provider = provider
        self.registry = registry
        self.store = store
        self.conversations = ConversationManager(store)
        self.context_manager = ContextManager(
            max_history=config.context.max_history_messages,
            compaction_threshold=config.context.compaction_threshold,
        )
        self.max_tool_iterations = config.context.max_tool_iterations
        self.last_usage: dict[str, int] = {"input": 0, "output": 0}
        self._tool_cache: dict[tuple, str] = {}

    def _reset_usage(self) -> None:
        """Reset per-message usage counter and tool cache."""
        self.last_usage = {"input": 0, "output": 0}
        self._tool_cache.clear()

    def _accumulate_usage(self, usage: dict) -> None:
        """Accumulate token usage from the latest response."""
        self.last_usage["input"] += usage.get("input", 0)
        self.last_usage["output"] += usage.get("output", 0)

    def _build_tool_context(self, conv_id: str, user_id: str, platform: str) -> ToolContext:
        return ToolContext(
            conversation_id=conv_id,
            user_id=user_id,
            platform=platform,
            allow_shell=self.config.permissions.allow_shell,
            allow_file_write=self.config.permissions.allow_file_write,
            allow_web_browse=self.config.permissions.allow_web_browse,
            confirm_dangerous=self.config.permissions.confirm_dangerous,
        )

    def _build_system_prompt(
        self,
        platform: str,
        recent_memories: list[dict] | None = None,
        relevant_docs: list[dict] | None = None,
    ) -> str:
        """Build the system prompt from config + tools + memories + RAG context."""
        parts: list[str] = []

        # Identity
        parts.append(f"You are {self.config.persona.name}, an AI assistant.")

        # Persona: SOUL.md overrides default personality if present
        from micropaw.config import CONFIG_DIR
        soul_file = CONFIG_DIR / "SOUL.md"
        if soul_file.exists():
            soul = soul_file.read_text()[:2048].strip()
            if soul:
                parts.append(soul)
            else:
                parts.append(f"You are {self.config.persona.personality}.")
        else:
            parts.append(f"You are {self.config.persona.personality}.")

        parts.append("Help users by chatting naturally and using tools when needed.")
        parts.append("Keep responses concise and conversational.")

        # User context: USER.md provides user preferences if present
        user_file = CONFIG_DIR / "USER.md"
        if user_file.exists():
            user_context = user_file.read_text()[:2048].strip()
            if user_context:
                parts.append(f"\nAbout the user:\n{user_context}")

        # Platform hints
        hints = {
            "terminal": "Chatting in a local terminal. The user is a developer.",
            "whatsapp": "In WhatsApp. Keep messages short and mobile-friendly.",
            "telegram": "In Telegram. You can use basic markdown.",
            "discord": "In Discord. You can use Discord markdown.",
            "slack": "In Slack. You can use Slack mrkdwn.",
            "webchat": "In a web chat interface. You can use markdown.",
        }
        if hint := hints.get(platform):
            parts.append(hint)

        # Tools
        tools = self.registry.all()
        if tools:
            parts.append("\nAvailable tools:")
            for tool in tools:
                parts.append(f"- {tool.name}: {tool.description}")
            parts.append("\nUse tools for real-world actions. Don't use tools when a text response suffices.")

        # Memories
        if recent_memories:
            parts.append("\nThings you remember:")
            for m in recent_memories:
                parts.append(f"- {m['key']}: {m['value']}")

        # RAG — relevant document chunks
        if relevant_docs:
            parts.append("\nRelevant knowledge from your documents:")
            for doc in relevant_docs:
                source = Path(doc["source"]).name if "/" in doc["source"] else doc["source"]
                content = doc["content"][:500]
                parts.append(f"- [{source}]: {content}")

        # Extra instructions
        if self.config.persona.system_prompt_extra:
            parts.append(f"\n{self.config.persona.system_prompt_extra}")

        return "\n".join(parts)

    async def _prepare(
        self,
        text: str,
        user_id: str,
        platform: str,
        channel_id: str | None = None,
        send_typing: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[str, list[Message]]:
        """Load history, build system prompt, prepare message list."""
        if send_typing:
            try:
                await send_typing()
            except Exception:
                pass

        conv_id = self.conversations.get_conversation_id(platform, user_id, channel_id)
        history = await self.conversations.load_history(conv_id, platform, user_id)
        # Memories and RAG are global — shared across all channels
        recent_memories = await self.store.get_recent_memories(10, user_id="_global")

        # RAG: search ingested documents for relevant chunks
        relevant_docs = await self.store.search_documents(text, limit=5, user_id="_global")

        system_prompt = self._build_system_prompt(platform, recent_memories, relevant_docs)

        messages: list[Message] = [Message(role="system", content=system_prompt)]
        messages.extend(history)
        messages.append(Message(role="user", content=text))

        # Compact context if needed
        messages = await self.context_manager.compact(messages, self.provider)

        await self.conversations.save_message(conv_id, Message(role="user", content=text))
        return conv_id, messages

    async def handle_message(
        self,
        text: str,
        user_id: str,
        platform: str,
        channel_id: str | None = None,
        send_typing: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message and return the response text (non-streaming)."""
        self._reset_usage()
        conv_id, messages = await self._prepare(text, user_id, platform, channel_id, send_typing)

        tool_defs = self.registry.to_tool_defs()
        tool_context = self._build_tool_context(conv_id, user_id, platform)

        response: AIResponse | None = None
        for _ in range(self.max_tool_iterations):
            try:
                response = await self.provider.chat(messages, tool_defs)
            except Exception as e:
                logger.error("AI provider error: %s", e)
                error_msg = _classify_error(e)
                await self.conversations.save_message(conv_id, Message(role="assistant", content=error_msg))
                return error_msg

            self._accumulate_usage(response.usage)

            if not response.tool_calls:
                reply = response.content or "I'm not sure how to respond to that."
                await self.conversations.save_message(conv_id, Message(role="assistant", content=reply))
                return reply

            messages = await self._execute_tools(messages, response, tool_context, conv_id)

            if send_typing:
                try:
                    await send_typing()
                except Exception:
                    pass

        fallback = response.content if response and response.content else "I've been working on this for a while. Here's what I have so far."
        await self.conversations.save_message(conv_id, Message(role="assistant", content=fallback))
        return fallback

    async def handle_message_stream(
        self,
        text: str,
        user_id: str,
        platform: str,
        channel_id: str | None = None,
        send_typing: Callable[[], Awaitable[None]] | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message with streaming. Streams text via on_chunk callback."""
        self._reset_usage()
        conv_id, messages = await self._prepare(text, user_id, platform, channel_id, send_typing)

        tool_defs = self.registry.to_tool_defs()
        tool_context = self._build_tool_context(conv_id, user_id, platform)

        response: AIResponse | None = None
        for _ in range(self.max_tool_iterations):
            try:
                final_response: AIResponse | None = None
                async for item in self.provider.chat_stream(messages, tool_defs):
                    if isinstance(item, str):
                        if on_chunk:
                            await on_chunk(item)
                    elif isinstance(item, AIResponse):
                        final_response = item

                if not final_response:
                    final_response = AIResponse(content="", tool_calls=[], usage={})
                response = final_response

            except Exception as e:
                logger.error("AI provider error: %s", e)
                error_msg = _classify_error(e)
                await self.conversations.save_message(conv_id, Message(role="assistant", content=error_msg))
                if on_chunk:
                    await on_chunk(error_msg)
                return error_msg

            self._accumulate_usage(response.usage)

            if not response.tool_calls:
                reply = response.content or "I'm not sure how to respond to that."
                await self.conversations.save_message(conv_id, Message(role="assistant", content=reply))
                return reply

            # Tool calls: switch to batch mode
            messages = await self._execute_tools(messages, response, tool_context, conv_id)

            if send_typing:
                try:
                    await send_typing()
                except Exception:
                    pass

        fallback = response.content if response and response.content else "I've been working on this for a while."
        await self.conversations.save_message(conv_id, Message(role="assistant", content=fallback))
        return fallback

    async def _execute_tools(
        self,
        messages: list[Message],
        response: AIResponse,
        tool_context: ToolContext,
        conv_id: str,
    ) -> list[Message]:
        """Execute tool calls from a response, append results to messages."""
        assistant_msg = Message(role="assistant", content=response.content, tool_calls=response.tool_calls)
        messages.append(assistant_msg)
        await self.conversations.save_message(conv_id, assistant_msg)

        for tc in response.tool_calls:
            tool = self.registry.get(tc.name)
            if tool:
                logger.info("[%s] %s", tc.name, _summarize_args(tc.arguments))
                # Check cache for read-only tools
                ck = _cache_key(tc.name, tc.arguments) if tc.name in _CACHEABLE_TOOLS else None
                if ck and ck in self._tool_cache:
                    result = self._tool_cache[ck]
                    logger.debug("[%s] cache hit", tc.name)
                else:
                    try:
                        result = await tool.execute(tc.arguments, tool_context)
                    except Exception as e:
                        result = f"Tool error: {e}"
                        logger.error("[%s] %s", tc.name, e)
                    if ck and not result.startswith("Tool error:"):
                        self._tool_cache[ck] = result
            else:
                result = f"Unknown tool: {tc.name}"
                logger.warning("Unknown tool called: %s", tc.name)

            # RAG: auto-ingest large file/PDF reads in background
            if (
                tc.name in _INGEST_TOOLS
                and len(result) > _MIN_INGEST_SIZE
                and not result.startswith("Error")
            ):
                source = tc.arguments.get("path") or tc.arguments.get("url") or "unknown"
                asyncio.create_task(self._auto_ingest(source, result, "_global"))

            tool_msg = Message(role="tool", content=result, tool_call_id=tc.id)
            messages.append(tool_msg)
            await self.conversations.save_message(conv_id, tool_msg)

        return messages

    async def _auto_ingest(self, source: str, content: str, user_id: str) -> None:
        """Background document ingestion for RAG."""
        try:
            count = await self.store.ingest_document(source, content, user_id)
            logger.info("RAG: auto-ingested %d chunks from %s", count, source)
        except Exception as e:
            logger.error("RAG: auto-ingest failed for %s: %s", source, e)


def _classify_error(e: Exception) -> str:
    """Turn a provider exception into a user-friendly message."""
    if isinstance(e, httpx.ConnectError):
        return "Cannot connect to the AI provider. Check your network or if Ollama is running."
    if isinstance(e, httpx.TimeoutException):
        return "The AI provider timed out. Try again, or switch to a faster model."
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 401:
            return "Invalid API key. Run 'micropaw setup' to fix it."
        if status == 403:
            return "Access denied by the AI provider. Check your API key permissions."
        if status == 429:
            return "Rate limited by the AI provider. Wait a moment and try again."
        if status == 500:
            return "The AI provider had an internal error. Try again."
        if status == 503:
            return "The AI provider is temporarily unavailable. Try again shortly."
        return f"AI provider error (HTTP {status}). Try again or check your config."
    return "Something went wrong talking to the AI provider. Please try again."


def _summarize_args(args: dict[str, Any]) -> str:
    """Create a short summary of tool arguments for logging."""
    parts = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 50:
            s = s[:47] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts) if parts else "(no args)"
