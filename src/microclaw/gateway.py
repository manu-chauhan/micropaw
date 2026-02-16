"""Gateway — central coordinator that wires channels, agent, and tools."""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from microclaw.channels.base import Channel
from microclaw.config import Config, DB_FILE
from microclaw.core.agent import Agent
from microclaw.memory.store import MemoryStore
from microclaw.providers.base import AIProvider
from microclaw.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


class _RateLimiter:
    """Simple per-user rate limiter using a sliding window."""

    def __init__(self, messages_per_minute: int, burst_limit: int) -> None:
        self.messages_per_minute = messages_per_minute
        self.burst_limit = burst_limit
        self._windows: dict[str, list[float]] = {}

    def check(self, user_id: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.monotonic()
        window = self._windows.setdefault(user_id, [])
        # Prune entries older than 60 seconds
        cutoff = now - 60.0
        self._windows[user_id] = window = [t for t in window if t > cutoff]

        if len(window) >= self.messages_per_minute:
            return False

        # Burst check: no more than burst_limit in 5 seconds
        recent = sum(1 for t in window if t > now - 5.0)
        if recent >= self.burst_limit:
            return False

        window.append(now)
        return True


class Gateway:
    """Starts all channels, initializes the database, and routes messages to the agent."""

    def __init__(
        self,
        config: Config,
        provider: AIProvider,
        registry: ToolRegistry,
        channels: list[Channel],
    ) -> None:
        self.config = config
        self.provider = provider
        self.registry = registry
        self.channels = channels
        self.store: MemoryStore | None = None
        self.agent: Agent | None = None
        self._rate_limiter = _RateLimiter(config.rate_limit.messages_per_minute, config.rate_limit.burst_limit)

    async def start(self) -> None:
        """Initialize everything and start all channels."""
        # Create embedding provider
        embedding_provider = None
        if self.config.embedding.enabled:
            embedding_provider = self._create_embedding_provider()

        # Init database
        self.store = MemoryStore(str(DB_FILE), embedding_provider=embedding_provider)
        await self.store.init()

        # Wire tools that need runtime dependencies
        from microclaw.tools.memory_tool import set_store as set_memory_store
        set_memory_store(self.store)

        from microclaw.tools.sessions import set_store as set_sessions_store
        set_sessions_store(self.store)

        from microclaw.tools.vision import set_provider
        set_provider(self.provider)

        # Init agent
        self.agent = Agent(self.config, self.provider, self.registry, self.store)

        # Start channels
        channel_tasks = []
        for channel in self.channels:
            try:
                # Wire handler callbacks
                channel.set_handler(self._handle_message)
                channel.set_stream_handler(self._handle_message_stream)
                # Give terminal channel extra context for slash commands
                if channel.name == "terminal":
                    channel._registry = self.registry
                    channel._config = self.config
                    channel._on_new_conversation = self._reset_terminal_conversation
                channel_tasks.append(channel.start())
                logger.info("Channel [%s] starting", channel.name)
            except Exception as e:
                logger.error("Failed to start channel [%s]: %s", channel.name, e)

        # Start channels concurrently for non-blocking channels
        # Terminal channel blocks, so it should be started last or separately
        terminal = None
        others = []
        for i, ch in enumerate(self.channels):
            if ch.name == "terminal":
                terminal = (ch, channel_tasks[i])
            else:
                others.append(channel_tasks[i])

        # Start non-blocking channels first
        if others:
            await asyncio.gather(*others, return_exceptions=True)

        logger.info(
            "Gateway running with %d channel(s) using %s provider",
            len(self.channels), self.provider.name,
        )

        if terminal:
            # Terminal blocks — run it as the main loop
            await terminal[1]
            await self.stop()
        else:
            # No terminal — wait for shutdown signal
            stop_event = asyncio.Event()

            def _signal_handler() -> None:
                logger.info("Shutting down...")
                stop_event.set()

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _signal_handler)
                except NotImplementedError:
                    pass

            await stop_event.wait()
            await self.stop()

    async def _handle_message(self, text: str, user_id: str, platform: str, channel_id: str | None) -> str:
        """Route a message to the agent (non-streaming)."""
        if not self.agent:
            return "Agent not initialized."
        if not self._rate_limiter.check(user_id):
            return "You're sending messages too fast. Please wait a moment."
        max_size = self.config.context.max_message_size
        if len(text) > max_size:
            return f"Message too long ({len(text):,} chars). Maximum is {max_size:,} characters."
        logger.info("[%s] %s: %s", platform, user_id, text[:80])
        response = await self.agent.handle_message(text, user_id, platform, channel_id)
        logger.info("[%s] MicroClaw: %s", platform, response[:80])
        return response

    async def _handle_message_stream(
        self, text: str, user_id: str, platform: str, channel_id: str | None,
        on_chunk=None,
    ) -> str:
        """Route a message to the agent (streaming)."""
        if not self.agent:
            return "Agent not initialized."
        if not self._rate_limiter.check(user_id):
            return "You're sending messages too fast. Please wait a moment."
        max_size = self.config.context.max_message_size
        if len(text) > max_size:
            return f"Message too long ({len(text):,} chars). Maximum is {max_size:,} characters."
        logger.info("[%s] %s: %s", platform, user_id, text[:80])
        response = await self.agent.handle_message_stream(text, user_id, platform, channel_id, on_chunk=on_chunk)
        logger.info("[%s] MicroClaw: %s", platform, response[:80])
        return response

    async def _reset_terminal_conversation(self) -> None:
        """Reset the terminal conversation ID so /new starts fresh."""
        if self.agent:
            # Generate a new unique conversation ID for terminal
            import uuid
            new_id = f"terminal:terminal-user:{uuid.uuid4().hex[:8]}"
            self.agent.conversations._terminal_override = new_id

    def _create_embedding_provider(self):
        from microclaw.memory.embeddings import LocalEmbedding, NullEmbedding, OllamaEmbedding, OpenAIEmbedding
        cfg = self.config.embedding
        if cfg.provider == "local":
            return LocalEmbedding(cfg.model)
        elif cfg.provider == "openai":
            api_key = self.config.provider_config.openai.api_key
            if not api_key:
                logger.warning("Embeddings enabled but no OpenAI API key; disabling vector search")
                return NullEmbedding()
            return OpenAIEmbedding(api_key, cfg.model)
        elif cfg.provider == "ollama":
            return OllamaEmbedding(self.config.provider_config.ollama.host, cfg.model)
        return NullEmbedding()

    async def stop(self) -> None:
        for channel in self.channels:
            try:
                await channel.stop()
            except Exception as e:
                logger.error("Error stopping [%s]: %s", channel.name, e)

        try:
            from microclaw.tools.web_browse import cleanup_browser
            await cleanup_browser()
        except Exception:
            pass

        # Close AI provider HTTP clients
        try:
            await self.provider.close()
        except Exception as e:
            logger.error("Error closing provider: %s", e)

        # Close embedding provider HTTP client if applicable
        if self.store and hasattr(self.store.embedder, "close"):
            try:
                await self.store.embedder.close()
            except Exception as e:
                logger.error("Error closing embedding provider: %s", e)

        if self.store:
            await self.store.close()

        logger.info("Gateway stopped.")
