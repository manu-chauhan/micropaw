"""WebSocket-based web chat channel with embedded UI.

Uses raw asyncio TCP server + RFC 6455 WebSocket — no heavy web framework.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import struct
import uuid

from micropaw.channels.base import Channel

logger = logging.getLogger(__name__)

MAX_CONNECTIONS = 50
MAX_MSG_LENGTH = 10_000
MAX_FRAME_SIZE = 1_048_576  # 1MB max WebSocket frame
MAX_HEADERS = 50
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------------------
# WebSocket frame codec (from NanoClaw, RFC 6455)
# ---------------------------------------------------------------------------

def _ws_accept_key(ws_key: str) -> str:
    combined = ws_key.strip() + _WS_GUID
    return base64.b64encode(hashlib.sha1(combined.encode()).digest()).decode()


def _ws_encode_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    first_byte = (0x80 if fin else 0x00) | opcode
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", first_byte, length)
    elif length < 65536:
        header = struct.pack("!BBH", first_byte, 126, length)
    else:
        header = struct.pack("!BBQ", first_byte, 127, length)
    return header + payload


async def _ws_read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    try:
        header = await reader.readexactly(2)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None

    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F

    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]

    if length > MAX_FRAME_SIZE:
        return None  # Reject oversized frames

    mask_key = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length)

    if masked and mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

    return opcode, payload


def _ws_text(data: str) -> bytes:
    payload = data.encode()
    if len(payload) > MAX_FRAME_SIZE:
        payload = payload[:MAX_FRAME_SIZE - 100] + b"\n...(truncated)"
    return _ws_encode_frame(0x1, payload)


def _ws_close() -> bytes:
    return _ws_encode_frame(0x8, b"")


def _ws_pong(payload: bytes) -> bytes:
    return _ws_encode_frame(0xA, payload)


# ---------------------------------------------------------------------------
# HTTP request parser (minimal, for WebSocket upgrade)
# ---------------------------------------------------------------------------

async def _read_http_request(reader: asyncio.StreamReader) -> dict[str, str] | None:
    """Read an HTTP request and return headers as a dict."""
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not request_line:
            return None
    except (TimeoutError, ConnectionError):
        return None

    headers: dict[str, str] = {"_request_line": request_line.decode(errors="replace").strip()}
    count = 0
    while count < MAX_HEADERS:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        decoded = line.decode(errors="replace").strip()
        if ":" in decoded:
            key, _, value = decoded.partition(":")
            headers[key.strip().lower()] = value.strip()
        count += 1
    return headers


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------

class WebChatChannel(Channel):
    """WebSocket web chat channel with embedded UI."""

    name = "webchat"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        auth_token: str = "",
        allowed_origins: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.auth_token = auth_token or secrets.token_urlsafe(32)
        self.allowed_origins = allowed_origins or []
        self._server: asyncio.Server | None = None
        self._connections: dict[str, asyncio.StreamWriter] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        logger.info("WebChat available at http://%s:%d", self.host, self.port)
        logger.info("WebChat auth token: %s...%s", self.auth_token[:4], self.auth_token[-4:])

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle a new TCP connection — either HTTP or WebSocket upgrade."""
        headers = await _read_http_request(reader)
        if not headers:
            writer.close()
            return

        request_line = headers.get("_request_line", "")
        path = request_line.split(" ")[1] if " " in request_line else "/"

        # Check for WebSocket upgrade
        if headers.get("upgrade", "").lower() == "websocket":
            await self._handle_websocket(reader, writer, headers, path)
        elif path == "/" or path.startswith("/?"):
            await self._serve_ui(writer, path)
        elif path.startswith("/api/status"):
            await self._serve_status(writer, path)
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

    def _check_token(self, path: str) -> bool:
        """Extract and verify token from query string."""
        if "token=" in path:
            for param in path.split("?")[-1].split("&"):
                if param.startswith("token="):
                    return secrets.compare_digest(param[6:], self.auth_token)
        return False

    async def _serve_ui(self, writer: asyncio.StreamWriter, path: str) -> None:
        if not self._check_token(path):
            body = b"Access denied. Append ?token=YOUR_TOKEN to the URL."
            writer.write(f"HTTP/1.1 401 Unauthorized\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        else:
            html = CHAT_HTML.replace('"__AUTH_TOKEN__"', json.dumps(self.auth_token))
            body = html.encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()

    async def _serve_status(self, writer: asyncio.StreamWriter, path: str) -> None:
        if not self._check_token(path):
            writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        body = json.dumps({"status": "ok", "connections": len(self._connections)}).encode()
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        writer.close()

    async def _handle_websocket(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, headers: dict, path: str) -> None:
        # Auth check
        if not self._check_token(path):
            writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Origin check
        if self.allowed_origins:
            origin = headers.get("origin", "")
            if origin not in self.allowed_origins:
                logger.warning("WebChat rejected origin: %s", origin)
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return

        # Connection limit
        if len(self._connections) >= MAX_CONNECTIONS:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # WebSocket handshake
        ws_key = headers.get("sec-websocket-key", "")
        accept = _ws_accept_key(ws_key)
        handshake = (
            f"HTTP/1.1 101 Switching Protocols\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        writer.write(handshake.encode())
        await writer.drain()

        conn_id = f"webchat:{uuid.uuid4().hex[:8]}"
        self._connections[conn_id] = writer
        logger.info("WebChat client connected: %s", conn_id)

        try:
            while True:
                frame = await _ws_read_frame(reader)
                if frame is None:
                    break

                opcode, payload = frame

                if opcode == 0x8:  # Close
                    writer.write(_ws_close())
                    await writer.drain()
                    break
                elif opcode == 0x9:  # Ping
                    writer.write(_ws_pong(payload))
                    await writer.drain()
                    continue
                elif opcode != 0x1:  # Not text
                    continue

                try:
                    data = json.loads(payload.decode())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                text = data.get("text", "").strip()
                if not text or len(text) > MAX_MSG_LENGTH:
                    continue

                asyncio.create_task(self._process_message(conn_id, writer, text))

        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._connections.pop(conn_id, None)
            logger.info("WebChat client disconnected: %s", conn_id)
            writer.close()

    async def _process_message(self, conn_id: str, writer: asyncio.StreamWriter, text: str) -> None:
        async def send_json(data: dict) -> None:
            try:
                writer.write(_ws_text(json.dumps(data)))
                await writer.drain()
            except ConnectionError:
                pass

        if self._stream_handler:
            async def on_chunk(chunk: str) -> None:
                await send_json({"type": "chunk", "text": chunk})

            await send_json({"type": "typing"})
            response = await self._stream_handler(text, conn_id, "webchat", None, on_chunk)
            await send_json({"type": "done", "text": response})
        elif self._handler:
            await send_json({"type": "typing"})
            response = await self._handler(text, conn_id, "webchat", None)
            await send_json({"type": "message", "text": response})

    async def stop(self) -> None:
        for writer in list(self._connections.values()):
            try:
                writer.write(_ws_close())
                await writer.drain()
                writer.close()
            except Exception:
                pass
        self._connections.clear()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("WebChat channel stopped.")

    async def send_message(self, chat_id: str, text: str) -> None:
        key = chat_id.replace("webchat:", "", 1)
        writer = self._connections.get(key)
        if writer:
            try:
                writer.write(_ws_text(json.dumps({"type": "message", "text": text})))
                await writer.drain()
            except ConnectionError:
                pass


# ---------------------------------------------------------------------------
# Embedded chat UI
# ---------------------------------------------------------------------------

CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MicroPaw Chat</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#1a1a2e;color:#e0e0e0;height:100vh;display:flex;flex-direction:column}
.header{background:#16213e;padding:16px 20px;border-bottom:1px solid #2a2a4a;display:flex;align-items:center;gap:12px}
.header h1{font-size:18px;color:#00d4ff}
.header .dot{width:10px;height:10px;border-radius:50%;background:#00ff88;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.messages{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:80%;padding:12px 16px;border-radius:16px;line-height:1.5;word-wrap:break-word;white-space:pre-wrap}
.msg.user{align-self:flex-end;background:#0a3d62;border-bottom-right-radius:4px}
.msg.bot{align-self:flex-start;background:#2a2a4a;border-bottom-left-radius:4px}
.msg.bot.streaming{border-left:3px solid #00d4ff}
.typing{align-self:flex-start;padding:12px 16px;color:#888;font-style:italic}
.input-area{background:#16213e;padding:16px 20px;border-top:1px solid #2a2a4a;display:flex;gap:12px}
.input-area input{flex:1;background:#2a2a4a;border:1px solid #3a3a5a;border-radius:24px;padding:12px 20px;color:#e0e0e0;font-size:15px;outline:none}
.input-area input:focus{border-color:#00d4ff}
.input-area button{background:#00d4ff;color:#1a1a2e;border:none;border-radius:24px;padding:12px 24px;font-size:15px;font-weight:600;cursor:pointer}
.input-area button:hover{background:#00b8d4}
.input-area button:disabled{opacity:.5;cursor:not-allowed}
@media(max-width:600px){.msg{max-width:90%}.input-area{padding:12px}}
</style>
</head>
<body>
<div class="header"><span class="dot"></span><h1>MicroPaw</h1></div>
<div class="messages" id="messages"></div>
<div class="input-area">
<input type="text" id="input" placeholder="Type a message..." autocomplete="off">
<button id="send">Send</button>
</div>
<script>
const msgs=document.getElementById('messages'),input=document.getElementById('input'),btn=document.getElementById('send');
let ws,currentBot=null,reconnectTimer=null;
const authToken="__AUTH_TOKEN__";

function connect(){
  const proto=location.protocol==='https:'?'wss':'ws';
  ws=new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(authToken)}`);
  ws.onopen=()=>{btn.disabled=false;addSys('Connected')};
  ws.onclose=()=>{btn.disabled=true;addSys('Disconnected. Reconnecting...');reconnectTimer=setTimeout(connect,2000)};
  ws.onerror=()=>ws.close();
  ws.onmessage=(e)=>{
    const d=JSON.parse(e.data);
    if(d.type==='typing'){showTyping()}
    else if(d.type==='chunk'){hideTyping();appendChunk(d.text)}
    else if(d.type==='done'){hideTyping();finalizeBot(d.text)}
    else if(d.type==='message'){hideTyping();addBot(d.text)}
  };
}

function send(){
  const t=input.value.trim();
  if(!t||!ws||ws.readyState!==1)return;
  addUser(t);ws.send(JSON.stringify({text:t}));input.value='';currentBot=null;
}

function addMsg(cls,text){const d=document.createElement('div');d.className='msg '+cls;d.textContent=text;msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;return d}
function addUser(t){addMsg('user',t)}
function addBot(t){currentBot=null;addMsg('bot',t)}
function addSys(t){const d=document.createElement('div');d.className='typing';d.textContent=t;msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight;setTimeout(()=>d.remove(),3000)}
function appendChunk(t){if(!currentBot){currentBot=addMsg('bot streaming','');}currentBot.textContent+=t;msgs.scrollTop=msgs.scrollHeight}
function finalizeBot(t){if(currentBot){currentBot.textContent=t;currentBot.classList.remove('streaming');currentBot=null;}else{addBot(t)}}
function showTyping(){if(!document.getElementById('ti')){const d=document.createElement('div');d.className='typing';d.id='ti';d.textContent='MicroPaw is thinking...';msgs.appendChild(d);msgs.scrollTop=msgs.scrollHeight}}
function hideTyping(){const d=document.getElementById('ti');if(d)d.remove()}

input.addEventListener('keydown',e=>{if(e.key==='Enter')send()});
btn.addEventListener('click',send);
connect();
</script>
</body>
</html>"""
