"""Discord channel using discord.py."""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from microclaw.channels.base import Channel, chunk_text

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 2000

MessageHandler = Callable[[str, str, str, str | None], Awaitable[str]]
StreamHandler = Callable[[str, str, str, str | None, Callable[[str], Awaitable[None]] | None], Awaitable[str]]


class DiscordChannel(Channel):
    name = "discord"

    def __init__(self, token: str, allowed_users: list[str] | None = None) -> None:
        self.token = token
        self.allowed_users = set(allowed_users) if allowed_users else set()
        self._client: Any = None
        self._handler: MessageHandler | None = None
        self._stream_handler: StreamHandler | None = None

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_stream_handler(self, handler: StreamHandler) -> None:
        self._stream_handler = handler

    async def start(self) -> None:
        try:
            import discord
        except ImportError:
            raise RuntimeError("discord.py not installed. Install with: pip install microclaw[discord]")

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready() -> None:
            logger.info("Discord channel connected: %s", client.user)

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
                accumulated = ""

                async def on_chunk(chunk: str) -> None:
                    nonlocal accumulated
                    accumulated += chunk
                    if len(accumulated) % 50 == 0:  # Throttle edits
                        try:
                            await placeholder.edit(content=accumulated[:MAX_MSG_LEN] + " ...")
                        except Exception:
                            pass

                response = await self._stream_handler(text, user_id, "discord", channel_id, on_chunk)

                for i, chunk in enumerate(chunk_text(response, MAX_MSG_LEN)):
                    try:
                        if i == 0:
                            await placeholder.edit(content=chunk)
                        else:
                            await message.channel.send(chunk)
                    except Exception as e:
                        logger.error("Discord send error: %s", e)

            elif self._handler:
                async with message.channel.typing():
                    response = await self._handler(text, user_id, "discord", channel_id)
                for chunk in chunk_text(response, MAX_MSG_LEN):
                    await message.channel.send(chunk)

        # Start in background (non-blocking)
        logger.info("Discord channel starting...")
        await client.start(self.token)

    async def stop(self) -> None:
        if self._client:
            await self._client.close()
            logger.info("Discord channel stopped.")

    async def send_message(self, chat_id: str, text: str) -> None:
        if self._client:
            channel = self._client.get_channel(int(chat_id.replace("discord:", "")))
            if channel:
                for chunk in chunk_text(text, MAX_MSG_LEN):
                    await channel.send(chunk)
