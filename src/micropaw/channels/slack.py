"""Slack channel using slack-bolt with Socket Mode."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from micropaw.channels.base import Channel, approval_buttons, chunk_text, settled_note, stream_response
from micropaw.channels.media import Attachment, fetch_media

logger = logging.getLogger(__name__)

# Slack's servers fetch every link in a message to build a preview, so a URL in a reply would reach them
_NO_UNFURL = {"unfurl_links": False, "unfurl_media": False}

MAX_MSG_LEN = 4000
STREAM_EDIT_INTERVAL = 1.0


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "plain_text", "text": text}}


class SlackChannel(Channel):
    name = "slack"

    def __init__(self, bot_token: str, app_token: str, signing_secret: str = "") -> None:
        super().__init__()
        self.bot_token = bot_token
        self.app_token = app_token
        self.signing_secret = signing_secret
        self._app: Any = None
        self._socket_handler: Any = None

    async def start(self) -> None:
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
            from slack_bolt.async_app import AsyncApp
        except ImportError:
            raise RuntimeError("slack-bolt not installed. Install with: pip install 'micropaw\\[slack]'")

        self._app = AsyncApp(token=self.bot_token, signing_secret=self.signing_secret or "not-used-in-socket-mode")

        @self._app.event("message")
        async def handle_message(event: dict, say: Any, client: Any) -> None:
            # Skip bot messages and edits; a message with files has the file_share subtype
            if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
                return

            text = event.get("text", "").strip()
            if not text and not event.get("files"):
                return

            user_id = event.get("user", "unknown")
            slack_channel = event.get("channel", "")

            # Owner-only: strangers get their ID once (in DMs), channel members are ignored
            denied = self.check_access(user_id)
            if denied is not None:
                if denied and slack_channel.startswith("D"):
                    await say(text=denied, **_NO_UNFURL)
                return
            # Slack DMs have channel IDs starting with "D" — treat as DMs (no channel_id)
            # so they share the unified DM conversation with other channels
            conv_channel_id = None if slack_channel.startswith("D") else slack_channel
            thread_ts = event.get("thread_ts") or event.get("ts", "")
            media = await self._read_files(event, user_id, say, thread_ts)

            if self._stream_handler:
                placeholder = await say(text="...", thread_ts=thread_ts, **_NO_UNFURL)
                await stream_response(
                    self._stream_handler, text, user_id, "slack", conv_channel_id,
                    edit_fn=lambda t: client.chat_update(
                        channel=slack_channel, ts=placeholder["ts"], text=t, **_NO_UNFURL,
                    ),
                    send_fn=lambda t: say(text=t, thread_ts=thread_ts, **_NO_UNFURL),
                    max_msg_len=MAX_MSG_LEN, edit_interval=STREAM_EDIT_INTERVAL, media=media,
                )

            elif self._handler:
                response = await self._handler(text, user_id, "slack", conv_channel_id, media=media)
                for chunk in chunk_text(response, MAX_MSG_LEN):
                    await say(text=chunk, thread_ts=thread_ts, **_NO_UNFURL)

        self._app.action(re.compile(r"^ap:[0-9a-f]{4}:(yes|no|always)$"))(self._on_press)

        self._socket_handler = AsyncSocketModeHandler(self._app, self.app_token)
        logger.info("Slack channel starting (Socket Mode)...")
        # start_async() blocks forever, so run as a background task
        self._task = asyncio.create_task(self._socket_handler.start_async())
        self._supervise(self._relaunch)
        # Give it a moment to connect
        await asyncio.sleep(2.0)
        if self._task.done() and self._task.exception():
            raise self._task.exception()
        logger.info("Slack channel connected.")

    async def _on_press(self, ack: Any, body: dict, client: Any) -> None:
        await ack()
        user_id = body["user"]["id"]
        slack_channel = body["channel"]["id"]
        # Checked against the owner list, not just the channel: anyone in it can press a button
        reply = self.press_approval(
            body["actions"][0]["action_id"], user_id, None if slack_channel.startswith("D") else slack_channel,
        )
        if reply is None:
            await client.chat_postEphemeral(channel=slack_channel, user=user_id, text="Only the owner can answer this.")
        elif (note := settled_note(reply)) is not None:
            text = f"{body['message']['text']}\n\n{note}"
            ts = body["message"]["ts"]
            await client.chat_update(channel=slack_channel, ts=ts, text=text, blocks=[_section(text)], **_NO_UNFURL)
        else:
            await client.chat_postEphemeral(channel=slack_channel, user=user_id, text=reply)

    async def _read_files(self, event: dict, user_id: str, say: Any, thread_ts: str) -> list:
        """Images and audio shared in the message, fetched with the bot token (only ever sent to slack.com).
        A file someone else uploaded and this message shares is outside content."""
        files = [Attachment(f.get("url_private", ""), f.get("mimetype", ""), f.get("size") or 0,
                            f.get("name") or "a file", outside=f.get("user") != user_id)
                 for f in event.get("files") or []]
        if not files:
            return []
        media, notes = await fetch_media(files, token=self.bot_token, token_host="slack.com")
        for note in notes:
            await say(text=note, thread_ts=thread_ts, **_NO_UNFURL)
        return media

    async def _relaunch(self):
        """A fresh socket handler for the watcher; the coroutine that runs it."""
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        self._socket_handler = AsyncSocketModeHandler(self._app, self.app_token)
        return self._socket_handler.start_async()

    async def stop(self) -> None:
        await self._stop_task(self._socket_handler.close_async() if self._socket_handler else None)
        logger.info("Slack channel stopped.")

    async def send_message(self, chat_id: str, text: str) -> None:
        if self._app:
            channel = chat_id.replace("slack:", "", 1)
            for chunk in chunk_text(text, MAX_MSG_LEN):
                await self._app.client.chat_postMessage(channel=channel, text=chunk, **_NO_UNFURL)

    async def send_approval(self, chat_id: str, text: str, ask_id: str, allow_always: bool) -> None:
        if not self._app:
            return
        channel = chat_id.replace("slack:", "", 1)
        *head, last = chunk_text(text, 2900 - 80)  # a section holds 3000 chars; room for the decision line
        for chunk in head:
            await self._app.client.chat_postMessage(channel=channel, text=chunk, **_NO_UNFURL)
        buttons = [
            {"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": data,
             **({"style": "primary"} if label == "Yes" else {"style": "danger"} if label == "No" else {})}
            for label, data in approval_buttons(ask_id, allow_always)
        ]
        await self._app.client.chat_postMessage(
            channel=channel, text=last, **_NO_UNFURL,
            blocks=[_section(last), {"type": "actions", "block_id": f"ap:{ask_id}", "elements": buttons}],
        )
