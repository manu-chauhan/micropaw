"""Discord channel using discord.py."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from micropaw.channels.base import Channel, approval_buttons, chunk_text, settled_note, stream_response
from micropaw.channels.media import Attachment, fetch_media

logger = logging.getLogger(__name__)

STREAM_EDIT_INTERVAL = 0.8
MAX_MSG_LEN = 2000


class DiscordChannel(Channel):
    name = "discord"

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token
        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._watcher: asyncio.Task | None = None

    def _create_client(self):
        """Create a fresh discord.py client with event handlers."""
        import discord

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        channel_ref = self  # avoid closure over self in nested functions

        @client.event
        async def on_message(message: discord.Message) -> None:
            if message.author == client.user:
                return

            user_id = str(message.author.id)
            is_dm = isinstance(message.channel, discord.DMChannel)

            # Owner-only: strangers get their ID once (in DMs), server members are ignored
            denied = channel_ref.check_access(user_id)
            if denied is not None:
                if denied and is_dm:
                    await message.channel.send(denied)
                return
            text = message.content

            if not is_dm:
                if client.user not in message.mentions:
                    return
                text = text.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()

            channel_id = str(message.channel.id) if not is_dm else None
            media = await channel_ref._read_attachments(message)

            if channel_ref._stream_handler:
                placeholder = await message.channel.send("...")
                await stream_response(
                    channel_ref._stream_handler, text, user_id, "discord", channel_id,
                    edit_fn=lambda t: placeholder.edit(content=t),
                    send_fn=lambda t: message.channel.send(t),
                    max_msg_len=MAX_MSG_LEN, edit_interval=STREAM_EDIT_INTERVAL, media=media,
                )
            elif channel_ref._handler:
                async with message.channel.typing():
                    response = await channel_ref._handler(text, user_id, "discord", channel_id, media=media)
                for chunk in chunk_text(response, MAX_MSG_LEN):
                    await message.channel.send(chunk)

        @client.event
        async def on_disconnect() -> None:
            logger.warning("Discord disconnected — library will auto-reconnect")

        @client.event
        async def on_resumed() -> None:
            logger.info("Discord session resumed")

        return client

    async def _read_attachments(self, message: Any) -> list:
        """Images and audio attached to the message, or forwarded inside it (those are from someone else)."""
        files = [Attachment(a.url, a.content_type or "", a.size or 0, a.filename, outside=False)
                 for a in message.attachments]
        for snap in getattr(message, "message_snapshots", None) or []:
            files += [Attachment(a.url, a.content_type or "", a.size or 0, a.filename, outside=True)
                      for a in getattr(snap, "attachments", [])]
        if not files:
            return []
        media, notes = await fetch_media(files)
        for note in notes:
            await message.channel.send(note)
        return media

    def _approval_view(self, ask_id: str, allow_always: bool) -> Any:
        import discord
        styles = {"yes": discord.ButtonStyle.success, "no": discord.ButtonStyle.danger}
        view = discord.ui.View(timeout=600)
        for label, data in approval_buttons(ask_id, allow_always):
            style = styles.get(label.lower(), discord.ButtonStyle.secondary)
            button = discord.ui.Button(label=label, custom_id=data, style=style)
            button.callback = self._on_press
            view.add_item(button)
        return view

    async def _on_press(self, interaction: Any) -> None:
        # The press is checked against the owner, not just the channel: anyone in a server can click
        channel_id = None if interaction.guild is None else str(interaction.channel_id)
        reply = self.press_approval(interaction.data.get("custom_id", ""), str(interaction.user.id), channel_id)
        if reply is None:
            await interaction.response.send_message("Only the owner can answer this.", ephemeral=True)
        elif (note := settled_note(reply)) is not None:
            await interaction.response.edit_message(content=f"{interaction.message.content}\n\n{note}", view=None)
        else:
            await interaction.response.send_message(reply, ephemeral=True)

    async def start(self) -> None:
        try:
            import discord  # noqa: F401
        except ImportError:
            raise RuntimeError("discord.py not installed. Install with: pip install 'micropaw\\[discord]'")

        self._client = self._create_client()

        logger.info("Discord channel starting...")
        ready_event = asyncio.Event()

        @self._client.event
        async def on_ready() -> None:
            logger.info("Discord channel connected: %s", self._client.user)
            ready_event.set()

        self._task = asyncio.create_task(self._client.start(self.token))
        self._watcher = asyncio.create_task(self._watch_task())

        try:
            await asyncio.wait_for(ready_event.wait(), timeout=30.0)
        except TimeoutError:
            self._task.cancel()
            raise RuntimeError("Discord bot failed to connect within 30 seconds")

    async def _watch_task(self) -> None:
        """Monitor the background task; log and restart on crash with a fresh client."""
        while True:
            try:
                await self._task
                return
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Discord task crashed: %s — restarting in 5s", e)
                await asyncio.sleep(5)
                try:
                    await self._client.close()
                except Exception:
                    pass
                self._client = self._create_client()
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

    async def _target(self, chat_id: str) -> Any:
        """The channel (or the owner's DM) for a chat ID, None if it can't be found."""
        raw = chat_id.replace("discord:", "", 1)
        if not raw:
            logger.error("Discord send_message: empty target ID")
            return None
        try:
            target_id = int(raw)
        except ValueError:
            logger.error("Discord send_message: invalid target ID %r", raw)
            return None
        # Try cached channel first (server channels), fall back to DM
        channel = self._client.get_channel(target_id)
        if not channel:
            try:
                user = await self._client.fetch_user(target_id)
                channel = await user.create_dm()
            except Exception as e:
                logger.error("Discord DM resolve failed for %s: %s", target_id, e)
                return None
        return channel

    async def send_message(self, chat_id: str, text: str) -> None:
        if self._client and (channel := await self._target(chat_id)):
            for chunk in chunk_text(text, MAX_MSG_LEN):
                await channel.send(chunk)

    async def send_approval(self, chat_id: str, text: str, ask_id: str, allow_always: bool) -> None:
        if self._client and (channel := await self._target(chat_id)):
            *head, last = chunk_text(text, MAX_MSG_LEN - 80)  # room for the decision line added later
            for chunk in head:
                await channel.send(chunk)
            await channel.send(last, view=self._approval_view(ask_id, allow_always))
