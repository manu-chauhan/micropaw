"""Telegram channel using python-telegram-bot."""

from __future__ import annotations

import logging
from typing import Any

from micropaw.channels.base import Channel, approval_buttons, chunk_text, settled_note, stream_response
from micropaw.providers.base import MediaItem

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 4096
STREAM_EDIT_INTERVAL = 0.5


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token
        self._app: Any = None
        self._bot_username: str | None = None

    async def start(self) -> None:
        try:
            from telegram import LinkPreviewOptions, Update
            from telegram.ext import Application, CallbackQueryHandler, ContextTypes, Defaults, filters
            from telegram.ext import MessageHandler as TGMsgHandler
        except ImportError:
            raise RuntimeError("python-telegram-bot not installed. Install with: pip install 'micropaw\\[telegram]'")

        # Concurrent updates: an approval answer must get through while the turn that asked is waiting
        # Link previews are fetched by Telegram's servers, so a URL in a reply would reach them: always off
        defaults = Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True))
        self._app = Application.builder().token(self.token).concurrent_updates(True).defaults(defaults).build()

        async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.message
            if not msg or not (msg.text or msg.caption or msg.photo or msg.voice or msg.audio):
                return

            chat = update.message.chat
            user = update.message.from_user
            user_id = str(user.id) if user else "unknown"

            is_group = chat.type in ("group", "supergroup")

            # Owner-only: strangers get their ID once (in DMs), group members are ignored
            denied = self.check_access(user_id)
            if denied is not None:
                if denied and not is_group:
                    await update.message.reply_text(denied)
                return
            text = msg.text or msg.caption or ""

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
            media = await _download_media(msg)

            async def send_typing() -> None:
                await context.bot.send_chat_action(chat_id=chat.id, action="typing")

            # A voice reply is spoken instead of typed, so it skips the live-edited text
            voice_in = bool(msg.voice) and self._voice_on is not None and self._voice_on(user_id, channel_id)
            if self._stream_handler and not (voice_in and self._handler):
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
                    max_msg_len=MAX_MSG_LEN, edit_interval=STREAM_EDIT_INTERVAL, media=media,
                )

            elif self._handler:
                await send_typing()
                response = await self._handler(text, user_id, "telegram", channel_id, media=media)
                reply_to = update.message.message_id if is_group else None
                if not (voice_in and await self._send_voice(chat.id, response, user_id, channel_id, reply_to)):
                    for chunk in chunk_text(response, MAX_MSG_LEN):
                        await context.bot.send_message(chat_id=chat.id, text=chunk, reply_to_message_id=reply_to)

        # Commands too: /memory, /forget and /cron enable are handled by MicroPaw itself
        self._app.add_handler(TGMsgHandler(filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO, on_message))
        self._app.add_handler(CallbackQueryHandler(self._on_press, pattern=r"^ap:"))

        logger.info("Telegram channel starting...")
        await self._app.initialize()
        await self._app.start()
        # A long poll of 50 s (the most Telegram allows) means about one request a minute while idle
        await self._app.updater.start_polling(timeout=50, drop_pending_updates=True)
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

    async def _on_press(self, update: Any, context: Any) -> None:
        q = update.callback_query
        msg = q.message
        if not msg or not getattr(msg, "chat", None):
            await q.answer()
            return
        channel_id = str(msg.chat.id) if msg.chat.type in ("group", "supergroup") else None
        # Checked against the owner list, not just the chat: any group member can press a button
        reply = self.press_approval(q.data, str(q.from_user.id), channel_id)
        if reply is None:
            await q.answer("Only the owner can answer this.", show_alert=True)
            return
        await q.answer(reply)
        if (note := settled_note(reply)) is not None:
            await q.edit_message_text(f"{msg.text}\n\n{note}")  # no reply_markup: the buttons go away

    async def send_approval(self, chat_id: str, text: str, ask_id: str, allow_always: bool) -> None:
        if not self._app:
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        try:
            tid = int(chat_id.replace("telegram:", "", 1))
        except ValueError:
            logger.error("Telegram send_approval: invalid chat ID %r", chat_id)
            return
        *head, last = chunk_text(text, MAX_MSG_LEN - 80)  # room for the decision line added later
        for chunk in head:
            await self._app.bot.send_message(chat_id=tid, text=chunk)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)
                                          for label, data in approval_buttons(ask_id, allow_always)]])
        await self._app.bot.send_message(chat_id=tid, text=last, reply_markup=keyboard)

    async def _send_voice(self, chat_id: int, text: str, user_id: str, channel_id: str | None,
                          reply_to: int | None) -> bool:
        """Speak the reply. False (nothing sent) when synthesis or the upload failed."""
        if not self._voice_say:
            return False
        try:
            spoken = await self._voice_say(text, user_id, channel_id)
            if spoken is None:
                return False
            from telegram import InputFile
            data, mime = spoken
            bot = self._app.bot
            if mime == "audio/ogg":
                await bot.send_voice(chat_id=chat_id, voice=InputFile(data, filename="reply.ogg"),
                                     reply_to_message_id=reply_to)
            else:
                await bot.send_audio(chat_id=chat_id, audio=InputFile(data, filename="reply.mp3"),
                                     reply_to_message_id=reply_to)
            return True
        except Exception as e:
            logger.warning("Telegram voice reply failed, sending text: %s", e)
            return False


async def _download_media(msg: Any) -> list[MediaItem]:
    """The largest version of a photo, and voice notes or audio files (Telegram caps bot downloads at 20 MB)."""
    items: list[MediaItem] = []
    outside = bool(getattr(msg, "forward_origin", None))  # a forwarded message was written by someone else
    if msg.photo:
        file = await msg.photo[-1].get_file()
        data = bytes(await file.download_as_bytearray())
        items.append(MediaItem(type="image", data=data, mime_type="image/jpeg", outside=outside))
    clip = msg.voice or msg.audio
    if clip:
        file = await clip.get_file()
        data = bytes(await file.download_as_bytearray())
        items.append(MediaItem(type="audio", data=data, mime_type=clip.mime_type or "audio/ogg", outside=outside))
    return items
