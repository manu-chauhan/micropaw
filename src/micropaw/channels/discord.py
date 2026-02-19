"""Discord channel using discord.py."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from micropaw.channels.base import Channel, chunk_text, stream_response

logger = logging.getLogger(__name__)

STREAM_EDIT_INTERVAL = 0.8
MAX_MSG_LEN = 2000


class DiscordChannel(Channel):
    name = "discord"

    def __init__(self, token: str, allowed_users: list[str] | None = None) -> None:
        super().__init__()
        self.token = token
        self.allowed_users = set(allowed_users) if allowed_users else set()
        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None

    async def start(self) -> None:
        try:
            import discord
        except ImportError:
            raise RuntimeError("discord.py not installed. Install with: pip install micropaw[discord]")

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == client.user:
                return

            user_id = str(message.author.id)
            if self.allowed_users and user_id not in self.allowed_users:
                return

            is_dm = isinstance(message.channel, discord.DMChannel)
            text = message.content

            if not is_dm:
                # In servers, only respond to mentions
                if client.user not in message.mentions:
                    return
                text = text.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()

            channel_id = str(message.channel.id) if not is_dm else None

            if self._stream_handler:
                placeholder = await message.channel.send("...")
                await stream_response(
                    self._stream_handler, text, user_id, "discord", channel_id,
                    edit_fn=lambda t: placeholder.edit(content=t),
                    send_fn=lambda t: message.channel.send(t),
                    max_msg_len=MAX_MSG_LEN, edit_interval=STREAM_EDIT_INTERVAL,
                )

            elif self._handler:
                async with message.channel.typing():
                    response = await self._handler(text, user_id, "discord", channel_id)
                for chunk in chunk_text(response, MAX_MSG_LEN):
                    await message.channel.send(chunk)

        # Start in background — client.start() blocks forever, so run as a task
        logger.info("Discord channel starting...")
        ready_event = asyncio.Event()

        @client.event
        async def on_ready() -> None:
            logger.info("Discord channel connected: %s", client.user)
            ready_event.set()

        @client.event
        async def on_disconnect() -> None:
            logger.warning("Discord disconnected — library will auto-reconnect")

        @client.event
        async def on_resumed() -> None:
            logger.info("Discord session resumed")

        self._task = asyncio.create_task(client.start(self.token))
        self._watcher = asyncio.create_task(self._watch_task())

        # Wait up to 30 seconds for the bot to connect
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=30.0)
        except TimeoutError:
            self._task.cancel()
            raise RuntimeError("Discord bot failed to connect within 30 seconds")

    async def _watch_task(self) -> None:
        """Monitor the background task; log and restart on crash."""
        while True:
            try:
                await self._task
                return  # clean exit
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Discord task crashed: %s — restarting in 5s", e)
                await asyncio.sleep(5)
                self._task = asyncio.create_task(self._client.start(self.token))

    async def stop(self) -> None:
        if self._watcher and not self._watcher.done():
            self._watcher.cancel()
        if self._client:
            await self._client.close()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Discord channel stopped.")

    async def send_message(self, chat_id: str, text: str) -> None:
        if self._client:
            channel = self._client.get_channel(int(chat_id.replace("discord:", "")))
            if channel:
                for chunk in chunk_text(text, MAX_MSG_LEN):
                    await channel.send(chunk)
