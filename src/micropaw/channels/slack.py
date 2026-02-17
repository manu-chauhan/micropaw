"""Slack channel using slack-bolt with Socket Mode."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Awaitable

from micropaw.channels.base import Channel, chunk_text

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 4000
STREAM_EDIT_INTERVAL = 1.0

MessageHandler = Callable[[str, str, str, str | None], Awaitable[str]]
StreamHandler = Callable[[str, str, str, str | None, Callable[[str], Awaitable[None]] | None], Awaitable[str]]


class SlackChannel(Channel):
    name = "slack"

    def __init__(self, bot_token: str, app_token: str, signing_secret: str = "") -> None:
        self.bot_token = bot_token
        self.app_token = app_token
        self.signing_secret = signing_secret
        self._handler: MessageHandler | None = None
        self._stream_handler: StreamHandler | None = None
        self._app: Any = None
        self._socket_handler: Any = None
        self._task: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_stream_handler(self, handler: StreamHandler) -> None:
        self._stream_handler = handler

    async def start(self) -> None:
        try:
            from slack_bolt.async_app import AsyncApp
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        except ImportError:
            raise RuntimeError("slack-bolt not installed. Install with: pip install micropaw[slack]")

        self._app = AsyncApp(token=self.bot_token, signing_secret=self.signing_secret or "not-used-in-socket-mode")

        @self._app.event("message")
        async def handle_message(event: dict, say: Any, client: Any) -> None:
            # Skip bot messages
            if event.get("bot_id") or event.get("subtype"):
                return

            text = event.get("text", "").strip()
            if not text:
                return

            user_id = event.get("user", "unknown")
            channel_id = event.get("channel", "")
            thread_ts = event.get("thread_ts") or event.get("ts", "")

            # Get user name
            user_name = user_id
            try:
                info = await client.users_info(user=user_id)
                user_name = info["user"]["real_name"] or info["user"]["name"]
            except Exception as e:
                logger.debug("Slack user info lookup failed: %s", e)

            if self._stream_handler:
                placeholder = await say(text="...", thread_ts=thread_ts)
                accumulated = ""
                last_edit = 0.0

                async def on_chunk(chunk: str) -> None:
                    nonlocal accumulated, last_edit
                    accumulated += chunk
                    now = time.monotonic()
                    if now - last_edit >= STREAM_EDIT_INTERVAL:
                        last_edit = now
                        try:
                            await client.chat_update(
                                channel=channel_id, ts=placeholder["ts"],
                                text=accumulated[:MAX_MSG_LEN - 4] + " ...",
                            )
                        except Exception as e:
                            logger.debug("Slack stream edit failed: %s", e)

                response = await self._stream_handler(text, user_id, "slack", channel_id, on_chunk)

                for i, chunk in enumerate(chunk_text(response, MAX_MSG_LEN)):
                    try:
                        if i == 0:
                            await client.chat_update(
                                channel=channel_id, ts=placeholder["ts"], text=chunk,
                            )
                        else:
                            await say(text=chunk, thread_ts=thread_ts)
                    except Exception as e:
                        logger.error("Slack send error: %s", e)

            elif self._handler:
                response = await self._handler(text, user_id, "slack", channel_id)
                for chunk in chunk_text(response, MAX_MSG_LEN):
                    await say(text=chunk, thread_ts=thread_ts)

        self._socket_handler = AsyncSocketModeHandler(self._app, self.app_token)
        logger.info("Slack channel starting (Socket Mode)...")
        # start_async() blocks forever, so run as a background task
        self._task = asyncio.create_task(self._socket_handler.start_async())
        self._watcher = asyncio.create_task(self._watch_task())
        # Give it a moment to connect
        await asyncio.sleep(2.0)
        if self._task.done() and self._task.exception():
            raise self._task.exception()
        logger.info("Slack channel connected.")

    async def _watch_task(self) -> None:
        """Monitor the background task; log and restart on crash."""
        while True:
            try:
                await self._task
                return
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Slack task crashed: %s — restarting in 5s", e)
                await asyncio.sleep(5)
                from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
                self._socket_handler = AsyncSocketModeHandler(self._app, self.app_token)
                self._task = asyncio.create_task(self._socket_handler.start_async())

    async def stop(self) -> None:
        if self._watcher and not self._watcher.done():
            self._watcher.cancel()
        if self._socket_handler:
            await self._socket_handler.close_async()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Slack channel stopped.")

    async def send_message(self, chat_id: str, text: str) -> None:
        if self._app:
            channel = chat_id.replace("slack:", "")
            for chunk in chunk_text(text, MAX_MSG_LEN):
                await self._app.client.chat_postMessage(channel=channel, text=chunk)
