"""Slack channel using slack-bolt with Socket Mode."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from micropaw.channels.base import Channel, chunk_text, stream_response

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 4000
STREAM_EDIT_INTERVAL = 1.0


class SlackChannel(Channel):
    name = "slack"

    def __init__(self, bot_token: str, app_token: str, signing_secret: str = "") -> None:
        super().__init__()
        self.bot_token = bot_token
        self.app_token = app_token
        self.signing_secret = signing_secret
        self._app: Any = None
        self._socket_handler: Any = None
        self._task: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None

    async def start(self) -> None:
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
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
            slack_channel = event.get("channel", "")
            # Slack DMs have channel IDs starting with "D" — treat as DMs (no channel_id)
            # so they share the unified DM conversation with other channels
            conv_channel_id = None if slack_channel.startswith("D") else slack_channel
            thread_ts = event.get("thread_ts") or event.get("ts", "")

            # Get user name
            try:
                info = await client.users_info(user=user_id)
                info["user"]["real_name"] or info["user"]["name"]
            except Exception as e:
                logger.debug("Slack user info lookup failed: %s", e)

            if self._stream_handler:
                placeholder = await say(text="...", thread_ts=thread_ts)
                await stream_response(
                    self._stream_handler, text, user_id, "slack", conv_channel_id,
                    edit_fn=lambda t: client.chat_update(
                        channel=slack_channel, ts=placeholder["ts"], text=t,
                    ),
                    send_fn=lambda t: say(text=t, thread_ts=thread_ts),
                    max_msg_len=MAX_MSG_LEN, edit_interval=STREAM_EDIT_INTERVAL,
                )

            elif self._handler:
                response = await self._handler(text, user_id, "slack", conv_channel_id)
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
            channel = chat_id.replace("slack:", "", 1)
            for chunk in chunk_text(text, MAX_MSG_LEN):
                await self._app.client.chat_postMessage(channel=channel, text=chunk)
