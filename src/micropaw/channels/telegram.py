"""Telegram channel using python-telegram-bot."""

from __future__ import annotations

import logging
from typing import Any

from micropaw.channels.base import Channel, chunk_text, stream_response

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 4096
STREAM_EDIT_INTERVAL = 0.5


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str, allowed_users: list[str] | None = None) -> None:
        super().__init__()
        self.token = token
        self.allowed_users = set(allowed_users) if allowed_users else set()
        self._app: Any = None
        self._bot_username: str | None = None

    async def start(self) -> None:
        try:
            from telegram import Update
            from telegram.ext import Application, ContextTypes, filters
            from telegram.ext import MessageHandler as TGMsgHandler
        except ImportError:
            raise RuntimeError("python-telegram-bot not installed. Install with: pip install 'micropaw\\[telegram]'")

        self._app = Application.builder().token(self.token).build()

        async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not update.message or not update.message.text:
                return

            chat = update.message.chat
            user = update.message.from_user
            user_id = str(user.id) if user else "unknown"

            # Allow-list filtering
            if self.allowed_users and user_id not in self.allowed_users:
                return

            is_group = chat.type in ("group", "supergroup")
            text = update.message.text

            if is_group:
                bot_un = self._bot_username
                if bot_un and f"@{bot_un}" not in text:
                    if not (update.message.reply_to_message and
                            update.message.reply_to_message.from_user and
                            update.message.reply_to_message.from_user.id == context.bot.id):
                        return
                if bot_un:
                    text = text.replace(f"@{bot_un}", "").strip()

            channel_id = str(chat.id) if is_group else None

            async def send_typing() -> None:
                await context.bot.send_chat_action(chat_id=chat.id, action="typing")

            if self._stream_handler:
                placeholder = await context.bot.send_message(
                    chat_id=chat.id, text="...",
                    reply_to_message_id=update.message.message_id if is_group else None,
                )
                await send_typing()
                await stream_response(
                    self._stream_handler, text, user_id, "telegram", channel_id,
                    edit_fn=lambda t: context.bot.edit_message_text(
                        chat_id=chat.id, message_id=placeholder.message_id, text=t,
                    ),
                    send_fn=lambda t: context.bot.send_message(chat_id=chat.id, text=t),
                    max_msg_len=MAX_MSG_LEN, edit_interval=STREAM_EDIT_INTERVAL,
                )

            elif self._handler:
                await send_typing()
                response = await self._handler(text, user_id, "telegram", channel_id)
                for chunk in chunk_text(response, MAX_MSG_LEN):
                    await context.bot.send_message(
                        chat_id=chat.id, text=chunk,
                        reply_to_message_id=update.message.message_id if is_group else None,
                    )

        self._app.add_handler(TGMsgHandler(filters.TEXT & ~filters.COMMAND, on_message))

        logger.info("Telegram channel starting...")
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        me = await self._app.bot.get_me()
        self._bot_username = me.username
        logger.info("Telegram channel connected: @%s", self._bot_username)

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram channel stopped.")

    async def send_message(self, chat_id: str, text: str) -> None:
        if self._app:
            raw = chat_id.replace("telegram:", "", 1)
            if not raw:
                logger.error("Telegram send_message: empty chat ID")
                return
            try:
                tid = int(raw)
            except ValueError:
                logger.error("Telegram send_message: invalid chat ID %r", raw)
                return
            for chunk in chunk_text(text, MAX_MSG_LEN):
                await self._app.bot.send_message(chat_id=tid, text=chunk)
