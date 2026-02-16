"""Gateway — central coordinator that wires channels, agent, and tools."""

from __future__ import annotations

import asyncio
import logging
import signal

from microclaw.channels.base import Channel
from microclaw.config import Config, DB_FILE
from microclaw.core.agent import Agent
from microclaw.memory.store import MemoryStore
from microclaw.providers.base import AIProvider
from microclaw.tools.base import ToolRegistry

logger = logging.getLogger(__name__)


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
        logger.info("[%s] %s: %s", platform, user_id, text[:80])
        response = await self.agent.handle_message_stream(text, user_id, platform, channel_id, on_chunk=on_chunk)
        logger.info("[%s] MicroClaw: %s", platform, response[:80])
        return response

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

        if self.store:
            await self.store.close()

        logger.info("Gateway stopped.")
