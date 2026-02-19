"""Gateway — central coordinator that wires channels, agent, and tools."""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from micropaw.channels.base import Channel
from micropaw.config import Config, DB_FILE
from micropaw.core.agent import Agent
from micropaw.memory.store import MemoryStore
from micropaw.providers.base import AIProvider
from micropaw.tools.base import ToolRegistry

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
        # Create embedding provider (auto-detect or explicit)
        embedding_provider = await self._create_embedding_provider()

        # Init database
        self.store = MemoryStore(str(DB_FILE), embedding_provider=embedding_provider)
        await self.store.init()

        # Wire tools that need runtime dependencies
        from micropaw.tools.memory_tool import set_store as set_memory_store
        set_memory_store(self.store)

        from micropaw.tools.sessions import set_store as set_sessions_store
        set_sessions_store(self.store)

        from micropaw.tools.vision import set_provider
        set_provider(self.provider)

        from micropaw.tools.web_search import set_search_config
        set_search_config(self.config.search)

        from micropaw.tools.google_auth import set_google_config
        if self.config.google.enabled and self.config.google.client_id:
            set_google_config(self.config.google)

        from micropaw.bus import MessageBus
        from micropaw.tools.delegate import set_delegate_deps
        self._bus = MessageBus()
        self._bus_task: asyncio.Task | None = None
        set_delegate_deps(
            self.provider, self._bus,
            registry=self.registry,
            max_iterations=self.config.context.max_subagent_iterations,
        )

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

        # Start scheduler for cron jobs + heartbeat
        from micropaw.scheduler import Scheduler
        self._scheduler = Scheduler(
            str(DB_FILE), self._handle_message, self._bus, self.channels,
            heartbeat_interval=self.config.scheduler.heartbeat_interval,
            max_jobs=self.config.scheduler.max_jobs,
        )
        await self._scheduler.init()
        self._scheduler_task: asyncio.Task | None = asyncio.create_task(self._scheduler.run())

        from micropaw.tools.cron_tool import set_scheduler
        set_scheduler(self._scheduler)

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

    def _validate(self, text: str, user_id: str) -> str | None:
        if not self.agent:
            return "Agent not initialized."
        if not self._rate_limiter.check(user_id):
            return "You're sending messages too fast. Please wait a moment."
        max_size = self.config.context.max_message_size
        if len(text) > max_size:
            return f"Message too long ({len(text):,} chars). Maximum is {max_size:,} characters."
        return None

    async def _handle_message(self, text: str, user_id: str, platform: str, channel_id: str | None) -> str:
        err = self._validate(text, user_id)
        if err:
            return err
        self._record_channel_activity(platform, user_id, channel_id)
        logger.info("[%s] %s: %s", platform, user_id, text[:80])
        response = await self.agent.handle_message(text, user_id, platform, channel_id)
        self._track_usage()
        logger.info("[%s] MicroPaw: %s", platform, response[:80])
        return response

    async def _handle_message_stream(
        self, text: str, user_id: str, platform: str, channel_id: str | None,
        on_chunk=None,
    ) -> str:
        err = self._validate(text, user_id)
        if err:
            return err
        self._record_channel_activity(platform, user_id, channel_id)
        logger.info("[%s] %s: %s", platform, user_id, text[:80])
        response = await self.agent.handle_message_stream(text, user_id, platform, channel_id, on_chunk=on_chunk)
        self._track_usage()
        logger.info("[%s] MicroPaw: %s", platform, response[:80])
        return response

    def _record_channel_activity(self, platform: str, user_id: str, channel_id: str | None) -> None:
        """Record last-known chat_id per channel so the scheduler can broadcast."""
        if hasattr(self, "_scheduler"):
            effective_chat_id = f"{platform}:{channel_id or user_id}"
            self._scheduler.record_activity(platform, effective_chat_id)

    def _track_usage(self) -> None:
        """Pass accumulated usage from agent to terminal channel."""
        if self.agent:
            terminal = self._get_terminal_channel()
            if terminal and hasattr(terminal, "track_usage"):
                terminal.track_usage(self.agent.last_usage)

    async def _reset_terminal_conversation(self) -> None:
        """Reset the terminal conversation ID so /new starts fresh."""
        if self.agent:
            import uuid
            new_id = f"terminal:terminal-user:{uuid.uuid4().hex[:8]}"
            self.agent.conversations.reset_terminal_session(new_id)

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

    async def _create_embedding_provider(self):
        from micropaw.memory.embeddings import (
            LocalEmbedding, NullEmbedding, OllamaEmbedding, OpenAIEmbedding,
            probe_ollama, _DEFAULT_LOCAL_MODEL, _DEFAULT_OLLAMA_MODEL,
        )
        cfg = self.config.embedding

        if cfg.enabled == "false":
            return NullEmbedding()

        # Explicit provider (user set provider != auto, or enabled == "true")
        if cfg.provider != "auto":
            return self._create_explicit_provider(cfg)

        # Auto-detect: probe available providers
        ollama_host = self.config.provider_config.ollama.host
        ollama_model = cfg.model or _DEFAULT_OLLAMA_MODEL

        # 1. Ollama — zero extra deps, local, fast
        if await probe_ollama(ollama_host, ollama_model):
            logger.info("Embeddings: auto-detected Ollama (%s)", ollama_model)
            return OllamaEmbedding(ollama_host, ollama_model)
        else:
            logger.debug(
                "Ollama not available or missing %s; "
                "run 'ollama pull %s' for local vector search",
                ollama_model, ollama_model,
            )

        # 2. OpenAI — zero extra deps, uses existing key
        openai_key = self.config.provider_config.openai.api_key
        if openai_key:
            model = cfg.model or "text-embedding-3-small"
            logger.info("Embeddings: auto-detected OpenAI (%s)", model)
            return OpenAIEmbedding(openai_key, model)

        # 3. Local fastembed/ONNX — fully offline, no PyTorch
        try:
            import fastembed  # noqa: F401
            model = cfg.model or _DEFAULT_LOCAL_MODEL
            logger.info("Embeddings: auto-detected local (%s)", model)
            return LocalEmbedding(model)
        except ImportError:
            pass

        # 4. Nothing available
        logger.info("Embeddings: no provider available; text search still works")
        return NullEmbedding()

    def _create_explicit_provider(self, cfg):
        from micropaw.memory.embeddings import (
            LocalEmbedding, NullEmbedding, OllamaEmbedding, OpenAIEmbedding,
            _DEFAULT_LOCAL_MODEL, _DEFAULT_OLLAMA_MODEL,
        )
        if cfg.provider == "local":
            try:
                import fastembed  # noqa: F401
            except ImportError:
                logger.warning(
                    "Embeddings: local selected but fastembed not installed; "
                    "falling back to disabled. Install with: pip install fastembed"
                )
                return NullEmbedding()
            return LocalEmbedding(cfg.model or _DEFAULT_LOCAL_MODEL)
        elif cfg.provider == "openai":
            api_key = self.config.provider_config.openai.api_key
            if not api_key:
                logger.warning("Embeddings: OpenAI selected but no API key; disabling")
                return NullEmbedding()
            return OpenAIEmbedding(api_key, cfg.model or "text-embedding-3-small")
        elif cfg.provider == "ollama":
            host = self.config.provider_config.ollama.host
            return OllamaEmbedding(host, cfg.model or _DEFAULT_OLLAMA_MODEL)
        return NullEmbedding()

    @staticmethod
    async def _cancel_task(task: asyncio.Task | None) -> None:
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def stop(self) -> None:
        if hasattr(self, "_scheduler"):
            self._scheduler._running = False
        await self._cancel_task(getattr(self, "_scheduler_task", None))
        if hasattr(self, "_scheduler"):
            await self._scheduler.close()

        if hasattr(self, "_bus"):
            self._bus.close()
        await self._cancel_task(getattr(self, "_bus_task", None))

        for channel in self.channels:
            try:
                await asyncio.wait_for(channel.stop(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Channel [%s] stop timed out", channel.name)
            except Exception as e:
                logger.error("Error stopping [%s]: %s", channel.name, e)

        try:
            from micropaw.tools.web_browse import cleanup_browser
            await cleanup_browser()
        except Exception:
            pass

        # Close AI provider HTTP clients
        try:
            await asyncio.wait_for(self.provider.close(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Provider close timed out")
        except Exception as e:
            logger.error("Error closing provider: %s", e)

        # Close embedding provider HTTP client if applicable
        if self.store and hasattr(self.store.embedder, "close"):
            try:
                await asyncio.wait_for(self.store.embedder.close(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Embedding provider close timed out")
            except Exception as e:
                logger.error("Error closing embedding provider: %s", e)

        if self.store:
            await self.store.close()

        logger.info("Gateway stopped.")
