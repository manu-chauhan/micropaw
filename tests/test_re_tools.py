"""Tests for the inspect and har tools, using small files built in the test."""

from __future__ import annotations

import hashlib
import json
import os
import struct

import pytest

from micropaw.tools import inspect_tool
from micropaw.tools.base import ToolContext, run_tool
from micropaw.tools.har_tool import HarTool, summarize_har
from micropaw.tools.inspect_tool import InspectTool, inspect_file


def _ctx(tmp_path):
    return ToolContext(conversation_id="c", user_id="u", platform="terminal", workspace=str(tmp_path))


def _pe() -> bytes:
    d = bytearray(0x400)
    d[:2] = b"MZ"
    struct.pack_into("<I", d, 0x3C, 0x40)
    d[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", d, 0x44, 0x8664, 1, 1_700_000_000, 0, 0, 240, 0x22)
    struct.pack_into("<H", d, 0x58, 0x20B)
    struct.pack_into("<H", d, 0x58 + 68, 3)
    struct.pack_into("<II", d, 0x58 + 112 + 8, 0x1000, 40)
    struct.pack_into("<8sIIII", d, 0x58 + 240, b".idata", 0x200, 0x1000, 0x200, 0x200)
    struct.pack_into("<IIIII", d, 0x200, 0x1060, 0, 0, 0x1040, 0x1060)
    d[0x240:0x24D] = b"KERNEL32.dll\0"
    struct.pack_into("<Q", d, 0x260, 0x1080)
    d[0x282:0x28E] = b"ExitProcess\0"
    d[0x300:0x320] = b"hello from the sample file!!".ljust(32, b" ")
    d[0x330:0x33E] = "wide string".encode("utf-16le")
    return bytes(d)


def _elf() -> bytes:
    shstr = b"\0.dynamic\0.dynstr\0.shstrtab\0"
    dynstr = b"\0libfoo.so\0"
    dyn = struct.pack("<qQqQ", 1, 1, 0, 0)
    body = shstr + dynstr + dyn
    base = 64
    shoff = base + len(body)
    sh = lambda n, t, off, size, link=0: struct.pack("<IIQQQQIIQQ", n, t, 0, 0, off, size, link, 0, 1, 0)  # noqa: E731
    secs = (sh(0, 0, 0, 0) + sh(1, 6, base + len(shstr) + len(dynstr), len(dyn), 2)
            + sh(10, 3, base + len(shstr), len(dynstr)) + sh(17, 3, base, len(shstr)))
    head = b"\x7fELF\x02\x01\x01" + bytes(9) + struct.pack("<HHI", 3, 0x3E, 1) + struct.pack("<QQQ", 0, 0, shoff)
    head += struct.pack("<IHHHHHH", 0, 64, 0, 0, 64, 4, 3)
    return head + body + secs


def _macho() -> bytes:
    name = b"/usr/lib/libSystem.B.dylib\0".ljust(32, b"\0")
    cmd = struct.pack("<IIIIII", 0xC, 24 + len(name), 24, 0, 0, 0) + name
    return b"\xcf\xfa\xed\xfe" + struct.pack("<iiIIIII", 0x0100000C, 0, 2, 1, len(cmd), 0, 0) + cmd


def test_hashes_entropy_and_strings(tmp_path):
    data = b"A" * 100 + b"visible text here" + os.urandom(70_000) + "wide text".encode("utf-16le")
    f = tmp_path / "blob.bin"
    f.write_bytes(data)
    out = inspect_file(str(f))
    assert hashlib.sha256(data).hexdigest() in out and hashlib.md5(data).hexdigest() in out
    assert "Type: unknown" in out and f"Size: {len(data):,} bytes" in out
    assert "visible text here" in out and "wide text" in out
    assert "2 blocks of 64 KB" in out and "2 above 7.2" in out


def test_hash_covers_the_whole_file_but_parsing_stops_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(inspect_tool, "_PARSE_CAP", 64 * 1024)
    data = b"early marker string" + bytes(70_000) + b"late marker string"
    f = tmp_path / "big.bin"
    f.write_bytes(data)
    out = inspect_file(str(f))
    assert hashlib.sha256(data).hexdigest() in out
    assert "early marker string" in out and "late marker string" not in out and "only the first" in out


def test_string_count_is_capped(tmp_path):
    f = tmp_path / "s.bin"
    f.write_bytes(b"\0".join(b"string number %d" % i for i in range(500)))
    assert len(inspect_file(str(f)).split("first found, up to 200):\n")[1].splitlines()) == 120


def test_pe_headers(tmp_path):
    f = tmp_path / "a.exe"
    f.write_bytes(_pe())
    out = inspect_file(str(f))
    assert "Type: Windows PE" in out and "PE32+, machine x86-64, subsystem windows console" in out
    assert "2023-11-14" in out and ".idata" in out and "KERNEL32.dll: ExitProcess" in out
    assert "hello from the sample file" in out and "wide string" in out


def test_elf_headers(tmp_path):
    f = tmp_path / "a.out"
    f.write_bytes(_elf())
    out = inspect_file(str(f))
    assert "ELF 64-bit little endian, machine x86-64, type shared object" in out
    assert ".dynamic" in out and "Needed libraries: libfoo.so" in out


def test_macho_headers(tmp_path):
    f = tmp_path / "a.macho"
    f.write_bytes(_macho())
    out = inspect_file(str(f))
    assert "Mach-O cpu ARM64, type executable, 1 load commands" in out
    assert "Linked dylibs: /usr/lib/libSystem.B.dylib" in out


def test_truncated_or_damaged_headers_do_not_crash(tmp_path):
    for name, data in (("pe", _pe()[:0x60]), ("elf", _elf()[:30]), ("macho", _macho()[:20])):
        f = tmp_path / name
        f.write_bytes(data)
        assert "Header parse stopped" in inspect_file(str(f))


@pytest.mark.asyncio
async def test_inspect_tool_rules(tmp_path):
    tool = InspectTool()
    assert tool.marks and tool.needs_approval({"path": "x"}, _ctx(tmp_path)) is None
    (tmp_path / "a.bin").write_bytes(b"MZ not really")
    ctx = _ctx(tmp_path)
    assert "Type: Windows PE" in await run_tool(tool, {"path": "a.bin"}, ctx)
    assert ctx.marked
    assert (await tool.execute({"path": "nope.bin"}, ctx)).startswith("Error: Not a file")
    assert (await tool.execute({"path": str(tmp_path)}, ctx)).startswith("Error: Not a file")
    assert (await tool.execute({"path": "~/.ssh/id_rsa"}, ctx)).startswith("Error: Access denied")


# ---------------------------------------------------------------------------
# HAR
# ---------------------------------------------------------------------------

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
_KEY = "sk9f8a7d6c5b4a39281706f5e4d3c2b1a0"


def _entry(method, url, status=200, mime="application/json", headers=(), query=(), body=None):
    req = {"method": method, "url": url, "headers": [{"name": n, "value": v} for n, v in headers],
           "queryString": [{"name": n, "value": v} for n, v in query]}
    if body:
        req["postData"] = {"mimeType": body, "text": "SECRET-BODY"}
    return {"request": req, "response": {"status": status, "content": {"mimeType": mime + "; charset=utf-8"}}}


def _har():
    h = [("Authorization", f"Bearer {_KEY}"), ("Cookie", "session=COOKIEVALUE"), ("Accept", "*/*")]
    entries = [
        _entry("GET", "https://api.example.com/v1/users/42?page=2&access_token=" + _KEY, headers=h,
               query=[("page", "2"), ("access_token", _KEY)]),
        _entry("GET", "https://api.example.com/v1/users/7?page=1", query=[("page", "1")]),
        _entry("GET", "https://api.example.com/v1/users/550e8400-e29b-41d4-a716-446655440000", status=404),
        _entry("GET", "https://api.example.com/v1/files/5f1d7f3a9c8b4e2d1a0b3c4d/parts/9"),
        _entry("POST", "https://api.example.com/v1/users", status=201, body="application/json"),
        _entry("GET", f"https://api.example.com/reset/{_JWT}"),
        _entry("GET", "https://cdn.example.net/app.js", mime="text/javascript"),
    ]
    return json.dumps({"log": {"entries": entries}})


def test_har_groups_and_folds_ids():
    out = summarize_har(_har())
    assert "GET api.example.com/v1/users/{user_id}  x3  status 200:2, 404:1" in out
    assert "query: access_token, page" in out
    assert "GET api.example.com/v1/files/{file_id}/parts/{part_id}" in out
    assert "POST api.example.com/v1/users  x1  status 201" in out and "body: application/json" in out
    assert "GET api.example.com/reset/{reset_token}" in out
    assert "7 requests, 5 endpoints" in out and "cdn.example.net" in out


def test_har_output_has_no_secrets():
    out = summarize_har(_har())
    for secret in (_KEY, _JWT, "COOKIEVALUE", "SECRET-BODY", "Bearer"):
        assert secret not in out
    assert "Authorization" in out and "Cookie" in out  # names only


def test_har_openapi_and_stub_are_valid():
    out = summarize_har(_har())
    spec = json.loads(out.split("OpenAPI 3.1 sketch for api.example.com:\n")[1].split("\n\nPython client stub")[0])
    assert spec["openapi"] == "3.1.0" and spec["servers"] == [{"url": "https://api.example.com"}]
    op = spec["paths"]["/v1/users/{user_id}"]["get"]
    assert {"name": "user_id", "in": "path", "required": True, "schema": {"type": "string"}} in op["parameters"]
    assert set(op["responses"]) == {"200", "404"}
    assert "requestBody" in spec["paths"]["/v1/users"]["post"]
    stub = out.split("Python client stub (add your own auth headers):\n")[1]
    ns: dict = {}
    exec(compile(stub, "stub", "exec"), ns)  # noqa: S102  (the stub is plain source we generated)
    assert hasattr(ns["Client"], "get_v1_users") and hasattr(ns["Client"], "post_v1_users")


@pytest.mark.asyncio
async def test_har_tool_rules_and_errors(tmp_path):
    tool = HarTool()
    ctx = _ctx(tmp_path)
    assert tool.marks
    (tmp_path / "ok.har").write_text(_har())
    assert "HAR: 7 requests" in await run_tool(tool, {"path": "ok.har"}, ctx) and ctx.marked
    (tmp_path / "bad.har").write_text("not json")
    assert (await tool.execute({"path": "bad.har"}, ctx)).startswith("Error: could not read")
    (tmp_path / "empty.har").write_text('{"log": {"entries": []}}')
    assert await tool.execute({"path": "empty.har"}, ctx) == "No requests found in this HAR file."
    assert (await tool.execute({"path": "missing.har"}, ctx)).startswith("Error: Not a file")
    assert (await tool.execute({"path": "~/.aws/credentials"}, ctx)).startswith("Error: Access denied")


def test_both_tools_are_registered():
    from micropaw.cli import _create_registry
    from micropaw.config import Config
    registry = _create_registry(Config())
    assert {"inspect", "har"} <= set(registry.names)
    assert registry.get("inspect").marks and registry.get("har").marks
