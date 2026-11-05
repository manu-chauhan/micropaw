"""Gateway — central coordinator that wires channels, agent, and tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from micropaw.channels.base import Channel
from micropaw.config import CONFIG_DIR, CONFIG_FILE, DB_FILE, Config, load_config
from micropaw.core.agent import Agent
from micropaw.core.profile import AgentProfile, AgentRuntime, build_view, providers_in, unknown_tool_names
from micropaw.memory.ledger import Ledger
from micropaw.memory.store import MemoryStore
from micropaw.providers.base import AIProvider
from micropaw.providers.registry import Registry
from micropaw.scheduler import AgentGoneError
from micropaw.tools.base import ToolRegistry
from micropaw.utils.redact import add_known_secret, config_secrets, redact, secret_env_names, set_known_secrets

logger = logging.getLogger(__name__)



def _redacting(on_chunk):
    """Hide known secrets in streamed text too (a key split across two chunks is caught in the final reply)."""
    if on_chunk is None:
        return None

    async def send(chunk: str) -> None:
        await on_chunk(redact(chunk))
    return send

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


class _AccessGuard:
    """Owner-only access: a sender must be listed in channels.<platform>.allowed_users.

    An empty list means nobody, not everybody. Terminal and web chat prove the owner
    themselves (local console, auth token); heartbeat and system runs are internal.
    The lists are re-read when config.json changes, so `paw allow` needs no restart.
    """

    _SELF_AUTHED = frozenset({"terminal", "webchat", "heartbeat", "system"})
    _DAY = 86400.0
    _MAX_REPLIES_PER_DAY = 50  # so strangers can't use the bot to spam

    def __init__(self, config: Config) -> None:
        self._channels = config.channels
        self._mtime = self._config_mtime()
        self._replied: set[tuple[str, str]] = set()
        self._day_start = time.monotonic()

    @staticmethod
    def _config_mtime() -> float:
        try:
            return CONFIG_FILE.stat().st_mtime
        except OSError:
            return 0.0

    def is_allowed(self, platform: str, user_id: str) -> bool:
        if platform in self._SELF_AUTHED:
            return True
        mtime = self._config_mtime()
        if mtime != self._mtime:
            self._mtime = mtime
            self._channels = load_config().channels
        allowed = getattr(getattr(self._channels, platform, None), "allowed_users", None) or []
        return str(user_id) in {str(u).strip() for u in allowed}

    def check(self, platform: str, user_id: str) -> str | None:
        """None if allowed; otherwise a one-a-day reply telling the sender their ID ("" = silent)."""
        if self.is_allowed(platform, user_id):
            return None
        now = time.monotonic()
        if now - self._day_start >= self._DAY:
            self._day_start = now
            self._replied.clear()
        key = (platform, str(user_id))
        if key in self._replied or len(self._replied) >= self._MAX_REPLIES_PER_DAY:
            return ""
        self._replied.add(key)
        logger.warning(
            "Refused %s user %s (not allowed). If this is you, run: paw allow %s %s",
            platform, user_id, platform, user_id,
        )
        return (
            f"This assistant is private. Your {platform} ID is {user_id}.\n"
            f"If this is your assistant, run this on its computer: paw allow {platform} {user_id}"
        )


class _Approvals:
    """Asks the owner before side effects and waits for the answer.

    Chat channels get a message with a short ID; the owner replies "yes <id>", "no <id>",
    or "always <id>" (saves an exact-match rule, never offered once outside content was
    read). An ask is single-use, bound to the chat it came from, and denied after
    10 minutes or a restart. The terminal asks inline instead.
    """

    TIMEOUT = 600.0
    _REPLY = re.compile(r"^(yes|no|always)\s+([0-9a-f]{4})$", re.IGNORECASE)

    def __init__(self, rules_file: Path) -> None:
        self._rules_file = rules_file
        self._pending: dict[str, tuple[str, asyncio.Future[bool], str]] = {}  # id -> (chat, answer, rule)
        try:
            self._rules: set[str] = set(json.loads(rules_file.read_text()))
        except (OSError, ValueError):
            self._rules = set()

    def _rule(self, tool: str, text: str, marked: bool) -> str:
        """The exact action as a rule key, or "" when rules don't apply (outside content was read)."""
        return "" if marked else hashlib.sha256(f"{tool}\n{text}".encode()).hexdigest()

    def _remember(self, rule: str) -> None:
        self._rules.add(rule)
        self._rules_file.parent.mkdir(parents=True, exist_ok=True)
        self._rules_file.write_text(json.dumps(sorted(self._rules)))
        if hasattr(os, "chmod"):
            os.chmod(self._rules_file, 0o600)

    async def ask(self, send: Callable[[str], Awaitable[None]], chat: str, tool: str, text: str, marked: bool) -> bool:
        rule = self._rule(tool, text, marked)
        if rule in self._rules:
            return True
        ask_id = secrets.token_hex(2)
        while ask_id in self._pending:
            ask_id = secrets.token_hex(2)
        answer: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[ask_id] = (chat, answer, rule)
        always = f", or 'always {ask_id}' to allow this exact action from now on" if rule else ""
        try:
            await send(
                f"Approval needed [{ask_id}]: {tool}\n\n{text}\n\n"
                f"Reply 'yes {ask_id}' or 'no {ask_id}'{always}. This expires in 10 minutes."
            )
            return await asyncio.wait_for(answer, self.TIMEOUT)
        except TimeoutError:
            return False
        finally:
            self._pending.pop(ask_id, None)

    async def ask_terminal(self, terminal: Any, tool: str, text: str, marked: bool) -> bool:
        rule = self._rule(tool, text, marked)
        if rule in self._rules:
            return True
        reply = await terminal.ask_owner(f"{tool}\n\n{text}", allow_always=bool(rule))
        if reply == "always":
            self._remember(rule)
        return reply in ("yes", "always")

    def waiting(self, chat: str) -> str | None:
        """The id of the approval this chat is waiting on, if any."""
        return next((i for i, (c, fut, _) in self._pending.items() if c == chat and not fut.done()), None)

    def answer(self, chat: str, text: str) -> str | None:
        """Handle "yes/no/always <id>" from the owner. None if the message isn't an answer."""
        m = self._REPLY.match(text.strip())
        if not m:
            return None
        word, ask_id = m.group(1).lower(), m.group(2).lower()
        entry = self._pending.get(ask_id)
        if not entry or entry[0] != chat:
            return f"No pending approval {ask_id} in this chat (it may have expired)."
        _, answer, rule = entry
        if word == "always":
            if not rule:
                return "'always' isn't offered for this one. Reply yes or no."
            self._remember(rule)
        if not answer.done():
            answer.set_result(word != "no")
        return "Declined." if word == "no" else "Approved."


_CRON_CMD = re.compile(r"^/cron\s+(enable|disable)\s+#?(\d+)$", re.IGNORECASE)
_FORGET_CMD = re.compile(r"^/forget\s+(.+)$", re.IGNORECASE)
_AGENT_CMD = re.compile(r"^/agent(?:\s+(\S+))?$", re.IGNORECASE)


class Gateway:
    """Starts all channels, initializes the database, and routes messages to the agent."""

    def __init__(
        self,
        config: Config,
        provider: AIProvider,
        registry: ToolRegistry,
        channels: list[Channel],
        fast_provider: AIProvider | None = None,
        vision_provider: AIProvider | None = None,
        *,
        runtimes: dict[str, AgentRuntime] | None = None,
        models: Registry | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.fast_provider = fast_provider
        self.vision_provider = vision_provider
        self.registry = registry
        self.channels = channels
        self.store: MemoryStore | None = None
        self.agent: Agent | None = None  # the main agent
        self.agents: dict[str, Agent] = {}
        self._runtimes = runtimes or {}
        self.models = models
        self._chat_agent: dict[str, str] = {}  # chat -> agent chosen with /agent, kept in data.db
        self._rate_limiter = _RateLimiter(config.rate_limit.messages_per_minute, config.rate_limit.burst_limit)
        self._access = _AccessGuard(config)
        self._approvals = _Approvals(CONFIG_DIR / "approved_actions.json")
        self._chat_locks: dict[Any, asyncio.Lock] = {}  # one turn at a time per conversation
        self.ledger: Ledger | None = None  # usage ledger; created on first /usage if nobody set it

    async def start(self) -> None:
        """Initialize everything and start all channels."""
        set_known_secrets(config_secrets(self.config))
        from micropaw.tools import shell
        shell.EXTRA_SECRET_ENV.update(n.upper() for n in secret_env_names(self.config))
        for name in secret_env_names(self.config):  # keys read from env vars the config names
            if os.environ.get(name):
                add_known_secret(name, os.environ[name])
        from micropaw.tools import sandbox
        sandbox.configure(self.config.permissions)
        sb = await asyncio.to_thread(sandbox.resolve)  # probe now, off the event loop
        if sb.host_asks and sys.platform != "win32":
            logger.warning("No sandbox found; shell commands run on this computer and always ask.")
        # Create embedding provider (auto-detect or explicit)
        embedding_provider = await self._create_embedding_provider()

        # Pre-load embedding model (downloads from HF on first run)
        if hasattr(embedding_provider, "warmup"):
            await embedding_provider.warmup()

        # Init database
        self.store = MemoryStore(str(DB_FILE), embedding_provider=embedding_provider)
        await self.store.init()
        self.ledger = Ledger(self.store.db)
        await self.ledger.init()
        if self.models:
            self.models.ledger.bind(self.ledger)

        # Wire tools that need runtime dependencies
        from micropaw.tools.memory_tool import set_store as set_memory_store
        set_memory_store(self.store)

        from micropaw.tools.contacts_tool import set_store as set_contacts_store
        set_contacts_store(self.store)

        from micropaw.tools.sessions import set_store as set_sessions_store
        set_sessions_store(self.store)

        from micropaw.tools.vision import set_provider
        set_provider(self.vision_provider or self.provider)

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
            self.fast_provider or self.provider, self._bus,
            registry=self.registry,
            max_iterations=self.config.context.max_subagent_iterations,
        )

        from micropaw.tools.channel_send import set_channel_send_deps
        set_channel_send_deps(self._bus)

        # MCP servers start in the background; their tools join the registry as each one is ready
        from micropaw.mcp import start_servers_background
        self._mcp_servers = []
        disabled = set(self.config.permissions.disabled_tools)

        def _mcp_ready(server, tools) -> None:
            self._mcp_servers.append(server)
            for tool in tools:
                if tool.name not in disabled:
                    self.registry.register(tool)

        self._mcp_task = start_servers_background(self.config.mcp_servers, _mcp_ready)

        # Init agents: "main" from the top-level fields unless the config names more
        runtimes = self._runtimes or {"main": self._main_runtime()}
        for name, rt in runtimes.items():
            self._warn_unknown_tools(rt.profile)
            agent = Agent(rt.config, rt.provider, build_view(self.registry, rt), self.store, rt.profile)
            agent.approver = self._approve
            agent.fast_provider = rt.fast
            agent.vision_provider = rt.vision
            agent.ledger = self.ledger
            self.agents[name] = agent
        self.agent = self.agents["main"]
        await self._load_chat_agents()
        self._probe_task = asyncio.create_task(self._probe_models(runtimes))

        # Wire handler callbacks to all channels
        terminal_coro = None
        other_coros: list[tuple[Channel, Any]] = []

        from micropaw.tools.channel_send import update_channel_chat_id

        for channel in self.channels:
            channel.set_handler(self._handle_message)
            channel.set_stream_handler(self._handle_message_stream)
            channel.set_access(self._access.check)
            if channel.name == "terminal":
                channel._registry = self.registry
                channel._config = self.config
                channel._on_new = self._on_new
                channel._on_rejoin = self._on_rejoin
                channel._on_newall = self._on_newall
                update_channel_chat_id("terminal", "terminal")
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
            email_poll_seconds=self.config.scheduler.email_poll_seconds, store=self.store,
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

    def _validate(self, text: str, user_id: str, platform: str = "", has_media: bool = False) -> str | None:
        if not self.agent:
            return "Agent not initialized."
        # Backstop: channels check access before replying, but nothing reaches the agent without it
        if not self._access.is_allowed(platform, user_id):
            return self._access.check(platform, user_id) or ""
        # An empty user turn is rejected by Claude and would break the shared conversation
        if not text.strip() and not has_media:
            return "I can only read text, photos and voice notes."
        # Internal platforms (heartbeat, system) bypass rate limiting
        if platform not in self._INTERNAL_PLATFORMS:
            if not self._rate_limiter.check(user_id):
                return "You're sending messages too fast. Please wait a moment."
        max_size = self.config.context.max_message_size
        if len(text) > max_size:
            return f"Message too long ({len(text):,} chars). Maximum is {max_size:,} characters."
        return None

    async def _approve(self, platform: str, chat_id: str, tool: str, text: str, marked: bool) -> bool:
        """Ask the owner on the chat the request came from."""
        if platform == "terminal":
            terminal = self._get_terminal_channel()
            return terminal is not None and await self._approvals.ask_terminal(terminal, tool, text, marked)
        channel = next((c for c in self.channels if c.name == platform), None)
        if channel is None:
            return False
        return await self._approvals.ask(lambda msg: channel.send_message(chat_id, msg), chat_id, tool, text, marked)

    def _approval_answer(self, text: str, user_id: str, platform: str, channel_id: str | None) -> str | None:
        if not self._access.is_allowed(platform, user_id):
            return None
        return self._approvals.answer(f"{platform}:{channel_id or user_id}", text)

    # -- agents: routing and the commands that change it ---------------------------------

    async def _load_chat_agents(self) -> None:
        try:
            await self.store.db.execute("CREATE TABLE IF NOT EXISTS chat_agent (chat TEXT PRIMARY KEY, agent TEXT)")
            await self.store.db.commit()
            async with self.store.db.execute("SELECT chat, agent FROM chat_agent") as cur:
                self._chat_agent = {chat: agent for chat, agent in await cur.fetchall() if agent in self.agents}
        except Exception as e:
            logger.warning("Could not read per-chat agent choices: %s", e)

    def _route(self, platform: str, user_id: str, channel_id: str | None) -> Agent | None:
        """Per-chat /agent choice, then an exact route, then a platform route, then main."""
        if len(self.agents) <= 1 or platform in self._INTERNAL_PLATFORMS or platform == "cron":
            return self.agent
        chat = f"{platform}:{channel_id or user_id}"
        routes = self.config.routes
        name = self._chat_agent.get(chat) or routes.get(chat) or routes.get(f"{platform}:*") or "main"
        return self.agents.get(name) or self.agent

    def _describe(self, agent: Agent) -> str:
        p = agent.profile
        kind = "local only" if p and p.private else "cloud or mixed"
        extras = [f"fast {p.fast}"] if p and p.fast else []
        extras += [f"falls back to {', '.join(p.fallbacks)}"] if p and p.fallbacks else []
        return f"{agent.name}: {p.main if p else agent.provider.name} ({kind}{''.join(', ' + e for e in extras)})"

    async def _agent_command(self, name: str | None, platform: str, user_id: str, channel_id: str | None) -> str:
        current = self._route(platform, user_id, channel_id)
        if name is None:
            return f"This chat talks to: {self._describe(current)}\nSwitch with /agent <name>; list with /agents."
        name = name.lower()
        if name not in self.agents:
            return f"No agent called '{name}'. Available: {', '.join(self.agents)}."
        chat = f"{platform}:{channel_id or user_id}"
        self._chat_agent[chat] = name
        await self.store.db.execute("INSERT OR REPLACE INTO chat_agent (chat, agent) VALUES (?, ?)", (chat, name))
        await self.store.db.commit()
        return f"This chat now talks to {self._describe(self.agents[name])}."

    async def _owner_command(
        self, text: str, platform: str = "", user_id: str = "", channel_id: str | None = None,
    ) -> str | None:
        """Commands the owner types that must not go through the model."""
        text = text.strip()
        if text.lower() == "/whoami":
            where = f"{platform}:{channel_id or user_id}"
            return (f"Platform: {platform}\nYour user ID: {user_id}\nThis chat: {where}\n"
                    f"Agent here: {self._route(platform, user_id, channel_id).name}\n"
                    "Use the chat ID in routes, for example paw agent add coder --model ... --route " + where)
        if text.lower() == "/agents":
            return "Agents:\n" + "\n".join(self._describe(a) for a in self.agents.values())
        if m := _AGENT_CMD.match(text):
            return await self._agent_command(m.group(1), platform, user_id, channel_id)
        if text.lower() == "/memory" and self.store:
            memories = await self.store.get_all_memories()
            if not memories:
                return "Nothing remembered yet."
            lines = [f"- {m['key']}: {m['value']}" + (" (saved after reading outside content)" if m["marked"] else "")
                     for m in memories]
            return "What I remember (remove one with /forget <key>):\n" + "\n".join(lines)
        if text.lower() == "/usage" and self.store:
            return await self._usage_text()
        if (m := _FORGET_CMD.match(text)) and self.store:
            key = m.group(1).strip()
            return f"Forgot: {key}" if await self.store.delete_memory(key) else f"Nothing saved as: {key}"
        m = _CRON_CMD.match(text)
        if not m or not hasattr(self, "_scheduler"):
            return None
        action, job_id = m.group(1).lower(), int(m.group(2))
        job = await self._scheduler.set_enabled(job_id, action == "enable")
        if not job:
            return f"Cron job #{job_id} not found."
        return f"Cron job #{job_id} {action}d: \"{job['description']}\""

    async def _usage_text(self) -> str:
        """Today's spend by agent and model, with the cap when one is set."""
        if self.ledger is None:
            self.ledger = Ledger(self.store.db)
            await self.ledger.init()
        rows = await self.ledger.breakdown(1)
        if not rows:
            return "No model calls recorded today."
        total = sum(t.usd for *_, t in rows)
        cap = self.config.budget.daily_usd
        lines = [f"Today: ${total:.2f}" + (f" of ${cap:.2f}" if cap else "") + ". It resets at midnight."]
        for _, agent, model, t in rows:
            price = f"${t.usd:.2f}" + (" plus calls with no known price" if t.unpriced_tokens else "")
            lines.append(f"- {agent}, {model}: {price}, {t.tokens:,} tokens, {t.calls} calls")
        return "\n".join(lines)

    def _chat_lock(self, platform: str, user_id: str, channel_id: str | None) -> asyncio.Lock:
        key = self.agent.conversations.get_conversation_id(platform, user_id, channel_id)
        return self._chat_locks.setdefault(key, asyncio.Lock())

    async def _handle_message(
        self, text: str, user_id: str, platform: str, channel_id: str | None, media: list | None = None,
        agent: str = "",
    ) -> str:
        return await self._handle(text, user_id, platform, channel_id, media, streaming=False, agent_name=agent)

    async def _handle_message_stream(
        self, text: str, user_id: str, platform: str, channel_id: str | None,
        on_chunk=None,
        media: list | None = None,
    ) -> str:
        return await self._handle(text, user_id, platform, channel_id, media, streaming=True, on_chunk=on_chunk)

    async def _handle(
        self, text: str, user_id: str, platform: str, channel_id: str | None, media: list | None,
        *, streaming: bool, on_chunk=None, agent_name: str = "",
    ) -> str:
        if (answer := self._approval_answer(text, user_id, platform, channel_id)) is not None:
            return answer
        err = self._validate(text, user_id, platform, bool(media))
        if err is not None:
            return err
        if (reply := await self._owner_command(text, platform, user_id, channel_id)) is not None:
            return reply
        if (waiting := self._approvals.waiting(f"{platform}:{channel_id or user_id}")) is not None:
            return f"Waiting for your answer to [{waiting}]: reply yes {waiting} or no {waiting}."
        self._record_channel_activity(platform, user_id, channel_id)
        logger.debug("[%s] %s: %s", platform, user_id, text[:80])
        if agent_name:  # a scheduled job runs on the agent that made it, never on another one
            agent = self.agents.get(agent_name)
            if agent is None:
                raise AgentGoneError(f"agent '{agent_name}' is not configured any more, so the job did not run. "
                                "Remove the job, or add the agent back.")
        else:
            agent = self._route(platform, user_id, channel_id)
        async with self._chat_lock(platform, user_id, channel_id):
            if streaming:
                response = await agent.handle_message_stream(
                    text, user_id, platform, channel_id, on_chunk=_redacting(on_chunk), media=media,
                )
            else:
                response = await agent.handle_message(text, user_id, platform, channel_id, media=media)
        self._track_usage(agent)
        logger.debug("[%s] MicroPaw: %s", platform, response[:80])
        return redact(response)

    # Internal platforms that should not be registered as real channels
    _INTERNAL_PLATFORMS = frozenset({"heartbeat", "system"})

    def _record_channel_activity(self, platform: str, user_id: str, channel_id: str | None) -> None:
        """Record last-known chat_id per channel so the scheduler and channel_send can route."""
        if platform in self._INTERNAL_PLATFORMS:
            return
        effective_chat_id = f"{platform}:{channel_id or user_id}"
        if hasattr(self, "_scheduler"):
            self._scheduler.record_activity(platform, effective_chat_id)
        from micropaw.tools.channel_send import update_channel_chat_id
        update_channel_chat_id(platform, effective_chat_id)

    def _track_usage(self, agent: Agent | None = None) -> None:
        """Pass accumulated usage from agent to terminal channel."""
        agent = agent or self.agent
        if agent:
            terminal = self._get_terminal_channel()
            if terminal and hasattr(terminal, "track_usage"):
                terminal.track_usage(agent.last_usage)

    def _all_agents(self) -> list[Agent]:
        return list(self.agents.values()) or ([self.agent] if self.agent else [])

    async def _on_new(self) -> None:
        """Branch terminal into a fresh conversation (/new)."""
        import uuid
        new_id = f"dm:terminal:{uuid.uuid4().hex[:8]}"
        for agent in self._all_agents():
            agent.conversations.new_channel_session("terminal", new_id)

    async def _on_rejoin(self) -> None:
        """Return terminal to the shared conversation (/rejoin)."""
        for agent in self._all_agents():
            agent.conversations.rejoin_channel("terminal")

    async def _on_newall(self) -> None:
        """Start a fresh conversation for all channels (/newall)."""
        import uuid
        new_id = f"dm:owner:{uuid.uuid4().hex[:8]}"
        for agent in self._all_agents():
            agent.conversations.new_all_sessions(new_id)
            agent.session_usage = {"input": 0, "output": 0}
            agent.message_count = 0

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
            _DEFAULT_LOCAL_MODEL,
            _DEFAULT_OLLAMA_MODEL,
            LocalEmbedding,
            NullEmbedding,
            OllamaEmbedding,
            OpenAIEmbedding,
            probe_ollama,
        )
        cfg = self.config.embedding

        if cfg.enabled == "false":
            return NullEmbedding()

        # Every message is embedded, so a private (all-local) agent, main included, rules out a cloud embedding service
        cloud_ok = not any(rt.profile.private for rt in self._runtimes.values())
        if not cloud_ok:
            logger.warning("An agent is private (local models only), so cloud embeddings are off.")

        # Explicit provider (user set provider != auto, or enabled == "true")
        if cfg.provider != "auto":
            if cfg.provider == "openai" and not cloud_ok:
                logger.warning("embedding.provider is openai, but an agent is private: embeddings are off.")
                return NullEmbedding()
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
        if openai_key and cloud_ok:
            logger.info("Embeddings: auto-detected OpenAI (text-embedding-3-small)")
            return OpenAIEmbedding(openai_key, "text-embedding-3-small")

        # 3. Local fastembed/ONNX — fully offline, no PyTorch
        try:
            import fastembed  # noqa: F401
            logger.info("Embeddings: auto-detected local (%s)", _DEFAULT_LOCAL_MODEL)
            return LocalEmbedding(_DEFAULT_LOCAL_MODEL)
        except ImportError:
            pass

        # 4. Nothing available
        logger.info("Embeddings: no provider available; text search still works")
        return NullEmbedding()

    def _create_explicit_provider(self, cfg):
        from micropaw.memory.embeddings import (
            _DEFAULT_LOCAL_MODEL,
            _DEFAULT_OLLAMA_MODEL,
            LocalEmbedding,
            NullEmbedding,
            OllamaEmbedding,
            OpenAIEmbedding,
        )
        if cfg.provider == "local":
            try:
                import fastembed  # noqa: F401
            except ImportError:
                logger.warning(
                    "Embeddings: local selected but fastembed not installed; "
                    "falling back to disabled. Install with: pip install 'micropaw[embeddings]'"
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

    def _main_runtime(self) -> AgentRuntime:
        profile = AgentProfile(name="main", main=self.config.provider)
        return AgentRuntime(profile, self.config, self.provider, self.fast_provider, self.vision_provider)

    def _warn_unknown_tools(self, profile: AgentProfile) -> None:
        for rule in unknown_tool_names([*profile.allow, *profile.deny], self.registry.names):
            logger.warning(
                "Agent '%s': tool '%s' is not a known tool name. Use * for patterns such as mcp_<server>_*.",
                profile.name, rule)

    async def _probe_models(self, runtimes: dict[str, AgentRuntime]) -> None:
        """Read /api/show for every Ollama model in use, in the background: it never loads a model."""
        seen: dict[tuple[str, str], Any] = {}
        for rt in runtimes.values():
            for provider in (rt.provider, rt.fast, rt.vision):
                for p in providers_in(provider):
                    if hasattr(p, "load_capabilities"):
                        seen.setdefault((p.host, p.model), p)
        for (host, model), p in seen.items():
            try:
                info = await p.load_capabilities()
            except Exception as e:
                logger.warning("Could not check Ollama model %s: %s", model, e)
                continue
            if info is None:
                logger.warning("Ollama model '%s' is not available at %s. Is Ollama running? Run: ollama pull %s",
                               model, host, model)
            elif not info.tools:
                logger.warning(
                    "Ollama model '%s' cannot use tools, so it can chat but not search, read files or run "
                    "anything. Pick a model with tools (paw models scan).", model)
            else:
                logger.info("Ollama model %s: tools ok%s", model, ", vision ok" if info.vision else "")

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

        await self._cancel_task(getattr(self, "_mcp_task", None))
        for server in getattr(self, "_mcp_servers", []):
            await server.close()

        from micropaw.tools.channel_send import cleanup as cleanup_channel_send
        cleanup_channel_send()

        for channel in self.channels:
            try:
                await asyncio.wait_for(channel.stop(), timeout=5.0)
            except TimeoutError:
                logger.warning("Channel [%s] stop timed out", channel.name)
            except Exception as e:
                logger.error("Error stopping [%s]: %s", channel.name, e)

        try:
            from micropaw.tools.web_browse import cleanup_browser
            await cleanup_browser()
        except Exception:
            pass

        probe = getattr(self, "_probe_task", None)
        await self._cancel_task(probe)

        # Close AI provider HTTP clients (the registry owns the shared ones)
        if self.models:
            await self.models.close()
        for provider in filter(None, (self.provider, self.fast_provider, self.vision_provider)):
            try:
                await asyncio.wait_for(provider.close(), timeout=5.0)
            except TimeoutError:
                logger.warning("Provider close timed out")
            except Exception as e:
                logger.error("Error closing provider: %s", e)

        # Close embedding provider HTTP client if applicable
        if self.store and hasattr(self.store.embedder, "close"):
            try:
                await asyncio.wait_for(self.store.embedder.close(), timeout=5.0)
            except TimeoutError:
                logger.warning("Embedding provider close timed out")
            except Exception as e:
                logger.error("Error closing embedding provider: %s", e)

        if self.store:
            await self.store.close()

        logger.info("Gateway stopped.")
