"""The air-gap canary: a gateway-like process in airgapped mode must not connect or look up a name outside this
machine (and the allowed LAN), from Python, from an MCP child or from the shell, while loopback still works.

Method: the child (tests/canary_gateway.py) records every socket and spawn event twice, before the guard (all
attempts) and after it (what got through). A real listener in this process counts who reached it: only the
child's own Python may, never a sandboxed child. A mutation run with the guard off must fail the same checks.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from micropaw.tools import sandbox

HERE = Path(__file__).parent
LINUX = sys.platform.startswith("linux")
HAVE_BWRAP = LINUX and shutil.which("bwrap") is not None and sandbox._selftest()
SHELL_PYTHON = Path("/usr/bin/python3").exists()

MCP_SERVER = """
import json, os, socket, sys
def probe():
    out = {}
    for name, addr in (("listener", ("127.0.0.1", int(os.environ["PROBE_PORT"]))), ("public", ("192.0.2.1", 9))):
        s = socket.socket(); s.settimeout(1.0)
        try:
            s.connect(addr); s.sendall(b"mcp"); out[name] = "connected"
        except OSError as e:
            out[name] = type(e).__name__
    return out
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "tools/list":
        res = {"tools": [{"name": "probe", "description": "p", "inputSchema": {"type": "object"}}]}
    elif m["method"] == "tools/call":
        res = {"content": [{"type": "text", "text": json.dumps(probe())}]}
    else:
        res = {}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)
"""

SHELL_SCRIPT = """
import socket
for a in (('127.0.0.1', {port}), ('192.0.2.1', 9)):
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect(a); s.sendall(b'shell'); print(a[0], 'connected')
    except OSError as e:
        print(a[0], type(e).__name__)
"""


class Listener:
    """A real local listener that records what each connection says."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.sock = socket.socket()
        self.sock.bind((host, 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.said: list[bytes] = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            conn.settimeout(2)
            try:
                self.said.append(conn.recv(64))
            except OSError:
                self.said.append(b"?")
            conn.close()

    def close(self) -> None:
        self.sock.close()


class FakeOllama(BaseHTTPRequestHandler):
    """Answers /api/show and /api/tags, and /api/chat with a scripted tool round, then a final text."""

    calls: list[dict] = []
    script: list[dict] = []

    def log_message(self, *a):
        pass

    def _reply(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply({"models": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/show":
            return self._reply({"capabilities": ["completion", "tools"], "model_info": {}})
        type(self).calls.append(body)
        first = len(type(self).calls) == 1
        msg = {"role": "assistant", "content": "canary done"}
        if first:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": n, "arguments": a}} for n, a in type(self).script]}
        self._reply({"message": msg, "done": True, "prompt_eval_count": 1, "eval_count": 1})


def private_address() -> str | None:
    """This machine's address on a private network, if it has one (a real LAN host to talk to)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    addr = ipaddress.ip_address(ip)
    return ip if addr.is_private and not addr.is_loopback else None


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def is_local(target: str, lan_ok: bool = True) -> bool:
    if target in ("localhost", "") or target.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(target.split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or (lan_ok and ip.is_private)


def assert_nothing_left(out: Path, allowed_programs: set[str], lan_ok: bool = True) -> None:
    """The canary's claim, checked against the 'post' log (what the guard let through)."""
    post = events(out / "post.jsonl")
    for e in post:
        if e["event"].startswith("socket."):
            assert is_local(e["target"], lan_ok), f"left the machine: {e}"
        else:
            assert e["target"] in allowed_programs, f"unexpected program: {e}"


def run_child(tmp_path: Path, config: dict, *, mode: str = "full", env: dict | None = None,
              wrap: list[str] | None = None):
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(json.dumps(config))
    full_env = {**os.environ, "MICROPAW_HOME": str(home), "CANARY_OUT": str(out), "CANARY_MODE": mode,
                "MICROPAW_PRIVACY": "airgapped", **(env or {})}
    full_env.pop("OLLAMA_HOST", None)
    proc = subprocess.run([*(wrap or []), sys.executable, str(HERE / "canary_gateway.py")],
                          capture_output=True, text=True, env=full_env, timeout=180, cwd=tmp_path)
    return out, proc


def base_config(port: int, **privacy) -> dict:
    return {
        "provider": "ollama", "provider_config": {"ollama": {"host": f"http://127.0.0.1:{port}", "model": "canary"}},
        "privacy": {"mode": "airgapped", **privacy}, "embedding": {"enabled": "false"},
        "permissions": {"allow_shell": True, "confirm_dangerous": False, "workspace": "WORKSPACE"},
        "channels": {"terminal": {"enabled": False}}, "scheduler": {"heartbeat_interval": 100000},
    }


@pytest.fixture
def listener():
    lst = Listener()
    yield lst
    lst.close()


@pytest.fixture
def lan_listener():
    ip = private_address()
    if ip is None:
        yield None, None
        return
    lst = Listener(ip)
    yield ip, lst
    lst.close()


@pytest.fixture
def fake_ollama():
    FakeOllama.calls, FakeOllama.script = [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


# --- scenario 1: a full turn ----------------------------------------------------------------------

@pytest.mark.skipif(not (HAVE_BWRAP and SHELL_PYTHON), reason="needs Linux with bubblewrap and /usr/bin/python3")
def test_airgapped_gateway_turn_makes_no_outside_connection(tmp_path, listener, lan_listener, fake_ollama):
    lan_ip, lan = lan_listener
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = base_config(fake_ollama.server_address[1], allow_lan=True)
    cfg["permissions"]["workspace"] = str(ws)
    # a script file, because the shell blocklist stops inline socket code
    (ws / "probe.py").write_text(SHELL_SCRIPT.format(port=listener.port))
    mcp_dir = tmp_path / "mcp"
    mcp_dir.mkdir()
    (mcp_dir / "server.py").write_text(MCP_SERVER)
    cfg["mcp_servers"] = {"probe": {
        "command": str(Path(sys.executable).resolve()), "args": [str(mcp_dir / "server.py")],
        "paths": [str(mcp_dir), sys.base_prefix], "env": {"PROBE_PORT": str(listener.port)}}}
    FakeOllama.script = [
        ("web_search", {"query": "canary search"}),
        ("http_request", {"url": "https://canary.invalid/x"}),
        ("shell", {"command": "python3 probe.py"}),
        ("mcp_probe_probe", {}),
        ("memory", {"action": "save", "key": "canary", "value": "v"}),
    ]
    env = {"CANARY_PORT": str(listener.port)}
    if lan_ip:
        env |= {"CANARY_LAN": lan_ip, "CANARY_LAN_PORT": str(lan.port)}
    out, proc = run_child(tmp_path, cfg, env=env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    results = json.loads((out / "results.json").read_text())

    # direct Python: loopback works, public destinations and names are refused, an unused LAN address is not
    assert results["mode"] == "airgapped"
    assert results["loopback"] == "ok"
    assert results["public_address"].startswith("blocked: air-gapped mode blocked")
    assert results["public_name"].startswith("blocked: air-gapped mode blocked a lookup of canary.invalid")
    assert results["public_name_connect"].startswith("blocked")
    assert not results["lan_unused"].startswith("blocked")  # allowed by the rules, then simply unreachable
    if lan_ip:
        assert results["lan_listener"] == "ok" and b"lan" in lan.said

    # the turn ran, with the tools that are allowed in this mode
    assert results["mcp_started"] and results["reply"].strip() == "canary done"
    assert "http_request" not in results["tools"] and "web_browse" not in results["tools"]
    assert {"shell", "memory", "web_search"} <= set(results["tools"])

    # what the sandboxed children saw (second request carries the tool results)
    tool_text = "\n".join(m["content"] for m in FakeOllama.calls[1]["messages"] if m["role"] == "tool")
    assert "127.0.0.1 connected" not in tool_text and "192.0.2.1 connected" not in tool_text
    assert tool_text.count("192.0.2.1 ") >= 1 and "127.0.0.1 " in tool_text
    # only this process's own loopback probe reached the real listener; neither the shell nor the MCP child did
    assert listener.said == [b"direct"], listener.said

    # the log: nothing but loopback and LAN sockets, and only isolated programs
    bwrap = os.path.normcase(shutil.which("bwrap"))
    assert_nothing_left(out, {bwrap})
    pre = events(out / "pre.jsonl")
    attempted = {e["target"] for e in pre if e["event"].startswith("socket.")}
    assert {"192.0.2.1", "canary.invalid"} <= attempted  # the canary did try, so a clean 'post' means something
    assert any(e["event"] == "subprocess.Popen" and os.path.normcase(e["target"]) == bwrap
               for e in events(out / "post.jsonl"))


# --- scenario 2: the rules without the gateway ----------------------------------------------------

def test_lan_off_blocks_a_private_address(tmp_path, listener):
    out, proc = run_child(tmp_path, base_config(1, allow_lan=False), mode="probes",
                          env={"CANARY_PORT": str(listener.port)})
    assert proc.returncode == 0, proc.stderr[-3000:]
    results = json.loads((out / "results.json").read_text())
    assert results["loopback"] == "ok"
    assert results["lan_unused"].startswith("blocked")
    assert_nothing_left(out, set(), lan_ok=False)


# --- mutation: the same checks must fail when the guard is off ------------------------------------

def test_the_canary_notices_a_leak_when_the_guard_is_off(tmp_path, listener):
    out, proc = run_child(tmp_path, base_config(1), mode="probes",
                          env={"CANARY_PORT": str(listener.port), "MICROPAW_TEST_DISABLE_GUARD": "1"})
    assert proc.returncode == 0, proc.stderr[-3000:]
    results = json.loads((out / "results.json").read_text())
    assert not results["public_address"].startswith("blocked")
    assert not results["public_name"].startswith("blocked")
    post_targets = {e["target"] for e in events(out / "post.jsonl")}
    assert {"192.0.2.1", "canary.invalid"} <= post_targets
    with pytest.raises(AssertionError, match="left the machine"):
        assert_nothing_left(out, set())


@pytest.mark.skipif(not HAVE_BWRAP, reason="needs Linux with bubblewrap")
def test_even_with_the_guard_off_a_network_namespace_stops_the_probes(tmp_path, listener):
    """Defence in depth: the unhooked process inside bwrap --unshare-net still cannot reach anything."""
    wrap = [shutil.which("bwrap"), "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--bind", str(tmp_path), str(tmp_path), "--unshare-net", "--die-with-parent", "--"]
    out, proc = run_child(tmp_path, base_config(1), mode="probes", wrap=wrap,
                          env={"CANARY_PORT": str(listener.port), "MICROPAW_TEST_DISABLE_GUARD": "1"})
    assert proc.returncode == 0, proc.stderr[-3000:]
    results = json.loads((out / "results.json").read_text())
    for name in ("loopback", "public_address", "public_name", "public_name_connect", "lan_unused"):
        assert results[name].startswith("os error"), (name, results[name])
    assert listener.said == []
