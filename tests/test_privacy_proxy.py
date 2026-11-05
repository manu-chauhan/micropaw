"""A proxy variable in the environment must not carry traffic out of local and airgapped mode."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading

import httpx
import pytest

from micropaw import privacy

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


@pytest.mark.parametrize("mode", ["local", "airgapped"])
def test_boot_env_removes_proxies_in_strict_modes(monkeypatch, mode):
    for key in PROXY_VARS:
        monkeypatch.setenv(key, "http://127.0.0.1:3128")
    monkeypatch.setenv("NO_PROXY", "example.com")
    privacy.boot_env(mode)
    assert not [k for k in PROXY_VARS if k in os.environ]
    assert os.environ["NO_PROXY"] == "*" and os.environ["no_proxy"] == "*"
    # a client that trusts the environment (the ones that do not use net.client) now has no proxy mount
    assert not httpx.AsyncClient(trust_env=True)._mounts


def test_normal_mode_leaves_proxies_alone(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
    privacy.boot_env("normal")
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:3128"


CHILD = """
import asyncio
from micropaw import _boot
_boot.prepare()
from micropaw.tools.web_search import _search_duckduckgo
print(asyncio.run(_search_duckduckgo('my private memory: salary 123456', 3)))
import httpx
async def embed():
    async with httpx.AsyncClient(timeout=3) as c:
        try:
            await c.post('https://api.openai.com/v1/embeddings', json={'input': 'secret'})
        except Exception as e:
            print('embed', type(e).__name__)
asyncio.run(embed())
"""


@pytest.mark.parametrize("mode", ["airgapped", "local"])
def test_a_client_that_trusts_the_environment_cannot_reach_a_proxy(tmp_path, mode):
    """The canary variant with proxy variables set: a fake proxy on loopback must see no connection."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    seen: list[bytes] = []

    def run():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            conn.settimeout(1)
            try:
                seen.append(conn.recv(256))
            except OSError:
                seen.append(b"?")
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    url = f"http://127.0.0.1:{server.getsockname()[1]}"
    env = {**os.environ, "MICROPAW_HOME": str(tmp_path), "MICROPAW_PRIVACY": mode, "HTTP_PROXY": url,
           "HTTPS_PROXY": url, "ALL_PROXY": url, "https_proxy": url}
    proc = subprocess.run([sys.executable, "-c", CHILD], env=env, capture_output=True, text=True, timeout=60)
    server.close()
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert seen == [], seen
    assert "embed " in proc.stdout  # the call failed here instead of leaving
