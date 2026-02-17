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

        from microclaw.tools.web_search import set_search_config
        set_search_config(self.config.search)

        from microclaw.bus import MessageBus
        from microclaw.tools.delegate import set_delegate_deps
        self._bus = MessageBus()
        self._bus_task: asyncio.Task | None = None
        set_delegate_deps(self.provider, self._bus)

        # Init agent
        self.agent = Agent(self.config, self.provider, self.registry, self.store)

        # Wire handler callbacks to all channels
        terminal_coro = None
        other_coros: list[tuple[Channel, Any]] = []

        for channel in self.channels:
            channel.set_handler(self._handle_message)
            channel.set_stream_handler(self._handle_message_stream)
            if channel.name == "terminal":
                channel._registry = self.registry
                channel._config = self.config
                channel._on_new_conversation = self._reset_terminal_conversation
                terminal_coro = (channel, channel.start())
            else:
                other_coros.append((channel, channel.start()))

        # Start non-blocking channels first and report failures
        started_channels: list[str] = []
        failed_channels: list[str] = []

        if other_coros:
            results = await asyncio.gather(
                *[coro for _, coro in other_coros], return_exceptions=True,
            )
            for (ch, _), result in zip(other_coros, results):
                if isinstance(result, BaseException):
                    logger.error("Channel [%s] failed to start: %s", ch.name, result)
                    failed_channels.append(f"{ch.name} ({result})")
                else:
                    started_channels.append(ch.name)

        if terminal_coro:
            started_channels.append("terminal")

        if failed_channels and not started_channels:
            logger.error("All channels failed to start: %s", ", ".join(failed_channels))
            await self.stop()
            raise RuntimeError(f"All channels failed to start: {', '.join(failed_channels)}")

        if failed_channels:
            logger.warning("Some channels failed: %s", ", ".join(failed_channels))

        logger.info(
            "Gateway running with %d channel(s) using %s provider",
            len(started_channels), self.provider.name,
        )

        # Start bus consumer for background sub-agent results
        self._bus_task = asyncio.create_task(self._bus_consumer())

        if terminal_coro:
            # Terminal blocks — run it as the main loop
            await terminal_coro[1]
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

    def _get_terminal_channel(self):
        """Find the terminal channel for usage tracking."""
        for ch in self.channels:
            if ch.name == "terminal":
                return ch
        return None

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
        self._track_usage()
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
        self._track_usage()
        logger.info("[%s] MicroClaw: %s", platform, response[:80])
        return response

    def _track_usage(self) -> None:
        """Pass accumulated usage from agent to terminal channel."""
        if self.agent:
            terminal = self._get_terminal_channel()
            if terminal and hasattr(terminal, "track_usage"):
                terminal.track_usage(self.agent.last_usage)

    async def _reset_terminal_conversation(self) -> None:
        """Reset the terminal conversation ID so /new starts fresh."""
        if self.agent:
            # Generate a new unique conversation ID for terminal
            import uuid
            new_id = f"terminal:terminal-user:{uuid.uuid4().hex[:8]}"
            self.agent.conversations._terminal_override = new_id

    async def _bus_consumer(self) -> None:
        """Consume outbound bus messages and route to channels."""
        while True:
            try:
                msg = await self._bus.consume_outbound()
                if msg is None:
                    if self._bus.closed:
                        return
                    continue
                # Find the right channel and deliver
                for ch in self.channels:
                    if ch.name == msg.channel:
                        try:
                            await ch.send_message(msg.chat_id, msg.content)
                        except Exception as e:
                            logger.error("Bus delivery to [%s] failed: %s", ch.name, e)
                        break
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Bus consumer error: %s", e)

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
        # Close bus and consumer
        if hasattr(self, "_bus"):
            self._bus.close()
        if hasattr(self, "_bus_task") and self._bus_task and not self._bus_task.done():
            self._bus_task.cancel()
            try:
                await self._bus_task
            except asyncio.CancelledError:
                pass

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
