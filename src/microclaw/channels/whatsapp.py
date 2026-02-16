"""WhatsApp channel using Meta Cloud API."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Callable, Awaitable

import httpx

from microclaw.channels.base import Channel, chunk_text

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 4000
API_VERSION = "v21.0"

MessageHandler = Callable[[str, str, str, str | None], Awaitable[str]]


class WhatsAppChannel(Channel):
    name = "whatsapp"

    def __init__(
        self,
        token: str,
        phone_id: str,
        verify_token: str = "",
        app_secret: str = "",
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self.token = token
        self.phone_id = phone_id
        self.verify_token = verify_token
        self.app_secret = app_secret
        self.host = host
        self.port = port
        self._handler: MessageHandler | None = None
        self._server: Any = None

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def start(self) -> None:
        """Start webhook server for WhatsApp."""
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/webhook", self._verify)
        app.router.add_post("/webhook", self._incoming)

        runner = web.AppRunner(app)
        await runner.setup()
        self._server = web.TCPSite(runner, self.host, self.port)
        await self._server.start()
        logger.info("WhatsApp webhook listening on %s:%d", self.host, self.port)

    async def _verify(self, request: Any) -> Any:
        """Handle Meta webhook verification challenge."""
        from aiohttp import web

        mode = request.query.get("hub.mode")
        token = request.query.get("hub.verify_token")
        challenge = request.query.get("hub.challenge")

        if mode == "subscribe" and token == self.verify_token:
            return web.Response(text=challenge)
        return web.Response(status=403)

    async def _incoming(self, request: Any) -> Any:
        """Handle incoming WhatsApp messages."""
        from aiohttp import web

        body = await request.read()

        # Verify HMAC signature
        if self.app_secret:
            sig = request.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(self.app_secret.encode(), body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return web.Response(status=401)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400)

        # Extract messages
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []):
                    if message.get("type") != "text":
                        continue

                    text = message.get("text", {}).get("body", "").strip()
                    if not text:
                        continue

                    sender = message.get("from", "unknown")

                    if self._handler:
                        response = await self._handler(text, sender, "whatsapp", None)
                        for chunk in chunk_text(response, MAX_MSG_LEN):
                            await self._send(sender, chunk)

        return web.Response(text="OK")

    async def _send(self, to: str, text: str) -> None:
        """Send a message via WhatsApp Cloud API."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"https://graph.facebook.com/{API_VERSION}/{self.phone_id}/messages",
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": text},
                },
            )

    async def stop(self) -> None:
        if self._server:
            await self._server.stop()
            logger.info("WhatsApp channel stopped.")

    async def send_message(self, chat_id: str, text: str) -> None:
        for chunk in chunk_text(text, MAX_MSG_LEN):
            await self._send(chat_id, chunk)
