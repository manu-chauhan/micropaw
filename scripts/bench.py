"""Small benchmark for the headless gateway. Standard library only, Linux (needs /proc).

    python scripts/bench.py                  # all numbers, 60 s idle window
    python scripts/bench.py --idle 20 --starts 5 --json

Run it with the Python that has MicroPaw installed. It starts `paw start --headless` in a temporary
MICROPAW_HOME with the webchat channel on, embeddings off and a fake Ollama server on loopback, then reports:

  cold start        spawn to webchat port accepting, one core (taskset -c 0 when available), warm .pyc
  idle RSS          VmRSS after the start has settled
  wakeups / minute  asyncio selector returns (a sitecustomize shim) and voluntary context switches, all threads
  idle written      write_bytes and wchar per idle minute
  turn written      write_bytes and wchar for one scripted turn (one tool call, two model calls)

Numbers from different machines are not comparable. Run it before and after a change on the same machine.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import signal
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_SHIM = '''\
import selectors, signal, os
_n = [0]
_cls = selectors.EpollSelector if hasattr(selectors, "EpollSelector") else selectors.DefaultSelector
_orig = _cls.select
def _select(self, timeout=None):
    _n[0] += 1
    return _orig(self, timeout)
_cls.select = _select
def _dump(*_):
    with open(os.environ["BENCH_COUNT_FILE"], "w") as f:
        f.write(str(_n[0]))
signal.signal(signal.SIGUSR1, _dump)
'''


class _FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, obj: dict | list[dict], status: int = 200) -> None:
        lines = obj if isinstance(obj, list) else [obj]
        body = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send({"models": []})

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.endswith("/api/show"):
            return self._send({"capabilities": ["completion", "tools"], "model_info": {"llama.context_length": 8192}})
        body = json.loads(raw or b"{}")
        last = (body.get("messages") or [{}])[-1]
        if last.get("role") == "tool":
            msg = {"role": "assistant", "content": "Saved."}
        else:
            msg = {"role": "assistant", "content": "", "tool_calls": [{"function": {
                "name": "memory", "arguments": {"action": "save", "key": "bench", "value": "scripted turn"}}}]}
        done = {"model": "fake", "message": msg, "done": True, "prompt_eval_count": 10, "eval_count": 5}
        self._send(done)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _proc_int(pid: int, path: str, key: str) -> int:
    for line in Path(f"/proc/{pid}/{path}").read_text().splitlines():
        if line.startswith(key + ":"):
            return int(line.split()[1])
    return 0


def _io(pid: int) -> dict[str, int]:
    return {k: _proc_int(pid, "io", k) for k in ("wchar", "write_bytes")}


def _ctx_switches(pid: int) -> int:
    total = 0
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            total += _proc_int(pid, f"task/{task.name}/status", "voluntary_ctxt_switches")
        except OSError:
            pass
    return total


def _selector_returns(pid: int, count_file: Path) -> int:
    os.kill(pid, signal.SIGUSR1)
    for _ in range(100):
        time.sleep(0.02)
        if count_file.exists() and count_file.read_text().strip():
            n = int(count_file.read_text())
            count_file.unlink()
            return n
    return -1


class _Daemon:
    def __init__(self, fake_port: int, pinned: bool) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="paw-bench-"))
        self.web_port = _free_port()
        self.count_file = self.home / "count"
        shim_dir = self.home / "shim"
        shim_dir.mkdir()
        (shim_dir / "sitecustomize.py").write_text(_SHIM)
        (self.home / "config.json").write_text(json.dumps({
            "schema_version": 1,
            "provider": "ollama",
            "provider_config": {"ollama": {"host": f"http://127.0.0.1:{fake_port}", "model": "fake"}},
            "channels": {"terminal": {"enabled": False},
                         "webchat": {"enabled": True, "port": self.web_port, "auth_token": "bench"}},
            "embedding": {"enabled": "false"},
            "permissions": {"sandbox": "off"},
        }))
        env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_TOKEN", "OLLAMA_HOST"))}
        env.update(MICROPAW_HOME=str(self.home), HOME=str(self.home), BENCH_COUNT_FILE=str(self.count_file),
                   PYTHONPATH=str(shim_dir), PYTHONDONTWRITEBYTECODE="")
        cmd = [sys.executable, "-c", "from micropaw.cli import main; main()", "start", "--headless"]
        if pinned and shutil.which("taskset"):
            cmd = ["taskset", "-c", "0", *cmd]
        self.t0 = time.perf_counter()
        self.proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.start_seconds = self._wait_port()

    def _wait_port(self, limit: float = 60.0) -> float:
        while time.perf_counter() - self.t0 < limit:
            if self.proc.poll() is not None:
                raise RuntimeError(f"gateway exited early with code {self.proc.returncode}")
            try:
                socket.create_connection(("127.0.0.1", self.web_port), timeout=0.2).close()
                return time.perf_counter() - self.t0
            except OSError:
                time.sleep(0.005)
        raise RuntimeError("webchat port did not open")

    def turn(self) -> float:
        """One scripted chat turn over the webchat WebSocket; returns the seconds it took."""
        s = socket.create_connection(("127.0.0.1", self.web_port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET /ws?token=bench HTTP/1.1\r\nHost: 127.0.0.1:{self.web_port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += s.recv(4096)
        payload = json.dumps({"text": "remember the bench"}).encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        head = bytes([0x81]) + (bytes([0x80 | len(payload)]) if len(payload) < 126
                                else bytes([0x80 | 126]) + struct.pack(">H", len(payload)))
        t0 = time.perf_counter()
        s.sendall(head + mask + masked)
        seen = b""
        while b'"done"' not in seen and b'"message"' not in seen:
            chunk = s.recv(65536)
            if not chunk:
                break
            seen += chunk
        took = time.perf_counter() - t0
        s.close()
        return took

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.home, ignore_errors=True)


def _warm(fake_port: int) -> None:
    """One throwaway start so the .pyc files exist before anything is timed."""
    d = _Daemon(fake_port, pinned=False)
    d.stop()


def run(idle: int, starts: int) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fake_port = server.server_address[1]
    _warm(fake_port)
    out: dict = {"python": sys.version.split()[0], "cpus": os.cpu_count(), "idle_seconds": idle}

    starts_s = []
    for _ in range(starts):
        d = _Daemon(fake_port, pinned=True)
        starts_s.append(d.start_seconds)
        d.stop()
    out["cold_start_s_min"] = round(min(starts_s), 3)
    out["cold_start_s_median"] = round(statistics.median(starts_s), 3)

    d = _Daemon(fake_port, pinned=False)
    try:
        time.sleep(5)  # let the start settle (probes, first writes)
        pid = d.proc.pid
        out["idle_rss_mb"] = round(_proc_int(pid, "status", "VmRSS") / 1024, 1)
        io0, cs0, sel0 = _io(pid), _ctx_switches(pid), _selector_returns(pid, d.count_file)
        t0 = time.monotonic()
        time.sleep(idle)
        io1, cs1, sel1 = _io(pid), _ctx_switches(pid), _selector_returns(pid, d.count_file)
        minutes = (time.monotonic() - t0) / 60
        out["idle_selector_wakeups_per_min"] = round((sel1 - sel0 - 1) / minutes, 1)
        out["idle_ctx_switches_per_min"] = round((cs1 - cs0) / minutes, 1)
        out["idle_write_bytes_per_min"] = round((io1["write_bytes"] - io0["write_bytes"]) / minutes)
        out["idle_wchar_per_min"] = round((io1["wchar"] - io0["wchar"]) / minutes)
        d.turn()  # first turn warms imports and the WAL
        time.sleep(2)
        io0 = _io(pid)
        out["turn_seconds"] = round(d.turn(), 3)
        time.sleep(2.5)  # let the WAL writes and background tasks finish
        io1 = _io(pid)
        out["turn_write_bytes"] = io1["write_bytes"] - io0["write_bytes"]
        out["turn_wchar"] = io1["wchar"] - io0["wchar"]
    finally:
        d.stop()
        server.shutdown()
    return out


def main() -> None:
    if not sys.platform.startswith("linux"):
        sys.exit("bench.py reads /proc, so it runs on Linux only.")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--idle", type=int, default=60, help="idle window in seconds (default 60)")
    ap.add_argument("--starts", type=int, default=5, help="cold starts to time (default 5)")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    args = ap.parse_args()
    result = run(args.idle, args.starts)
    if args.json:
        print(json.dumps(result))
    else:
        for k, v in result.items():
            print(f"{k:34} {v}")


if __name__ == "__main__":
    main()
