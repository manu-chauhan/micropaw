"""WhatsApp channel using Meta Cloud API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from collections import OrderedDict
from typing import Any

import httpx

from micropaw import net
from micropaw.channels.base import Channel, chunk_text
from micropaw.utils.logger import short_hash

logger = logging.getLogger(__name__)

MAX_MSG_LEN = 4000
API_VERSION = "v21.0"
SEEN_IDS = 1000  # message ids remembered for de-duplication


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
        super().__init__()
        self.token = token
        self.phone_id = phone_id
        self.verify_token = verify_token
        self.app_secret = app_secret
        self.host = host
        self.port = port
        self._server: Any = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Start webhook server for WhatsApp."""
        if not self.app_secret:
            raise RuntimeError(
                "WhatsApp needs app_secret (WHATSAPP_APP_SECRET) so webhook signatures can be checked. "
                "Without it anyone who reaches the port could pose as any sender."
            )
        try:
            from aiohttp import web
        except ImportError:
            raise RuntimeError("aiohttp not installed. Install with: pip install 'micropaw\\[whatsapp]'")

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

        if mode == "subscribe" and secrets.compare_digest(token or "", self.verify_token):
            return web.Response(text=challenge)
        return web.Response(status=403)

    async def _incoming(self, request: Any) -> Any:
        """Handle incoming WhatsApp messages."""
        from aiohttp import web

        body = await request.read()

        # Verify HMAC signature (always: without it anyone can forge any sender)
        sig = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(self.app_secret.encode(), body, hashlib.sha256).hexdigest()
        if not self.app_secret or not hmac.compare_digest(sig, expected):
            return web.Response(status=401)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400)

        # Meta retries webhooks that are not answered quickly, so ack now and work in the background
        batch: list[tuple[str, str]] = []
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []):
                    if message.get("type") != "text":
                        continue

                    text = message.get("text", {}).get("body", "").strip()
                    if not text or self._is_duplicate(message.get("id")):
                        continue

                    batch.append((text, message.get("from", "unknown")))

        if batch and self._handler:
            task = asyncio.create_task(self._process(batch))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return web.Response(text="OK")

    def _is_duplicate(self, msg_id: str | None) -> bool:
        if not msg_id:
            return False
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = None
        if len(self._seen) > SEEN_IDS:
            self._seen.popitem(last=False)
        return False

    async def _process(self, batch: list[tuple[str, str]]) -> None:
        for text, sender in batch:
            denied = self.check_access(sender)
            if denied is not None:
                if denied:
                    await self._send(sender, denied)
                continue
            try:
                response = await self._handler(text, sender, "whatsapp", None)
                for chunk in chunk_text(response, MAX_MSG_LEN):
                    await self._send(sender, chunk)
            except Exception:
                logger.exception("WhatsApp message handling failed")

    async def _send(self, to: str, text: str) -> None:
        """Send a message via WhatsApp Cloud API."""
        try:
            url = f"https://graph.facebook.com/{API_VERSION}/{self.phone_id}/messages"
            async with net.client(url, timeout=15.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
                    json={
                        "messaging_product": "whatsapp",
                        "to": to,
                        "type": "text",
                        "text": {"body": text, "preview_url": False},
                    },
                )
                if resp.status_code >= 400:
                    logger.error("WhatsApp API error %d: %s", resp.status_code, resp.text[:200])
        except httpx.TimeoutException:
            logger.error("WhatsApp send timed out for recipient %s", short_hash(to))
        except Exception as e:
            logger.error("WhatsApp send failed: %s", e)

    async def stop(self) -> None:
        if self._server:
            await self._server.stop()
            logger.info("WhatsApp channel stopped.")

    async def send_message(self, chat_id: str, text: str) -> None:
        to = chat_id.replace("whatsapp:", "", 1)
        for chunk in chunk_text(text, MAX_MSG_LEN):
            await self._send(to, chunk)
