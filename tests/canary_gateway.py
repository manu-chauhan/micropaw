"""A gateway-like process for the air-gap canary test (tests/test_privacy_canary.py). Run as a script.

Environment: CANARY_OUT (folder for the logs), CANARY_PORT (a real loopback listener in the test), CANARY_LAN
(an optional private address, with its listener port in CANARY_LAN_PORT), CANARY_MODE (full or probes).
With MICROPAW_TEST_DISABLE_GUARD=1 the egress guard is not installed (the test uses that to prove the canary
notices a leak).

Two recorder hooks write every socket and spawn event to a file. `pre` is added before the guard and sees every
attempt. `post` is added after it, so it only sees what the guard let through.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

OUT = Path(os.environ["CANARY_OUT"])
_EVENTS = {"socket.connect": 1, "socket.sendto": 1, "socket.getaddrinfo": 0, "socket.gethostbyname": 0,
           "socket.gethostbyaddr": 0, "subprocess.Popen": None, "os.system": 0, "os.posix_spawn": None,
           "os.exec": None}


def recorder(name: str) -> None:
    fd = os.open(OUT / f"{name}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND)

    def hook(event: str, args: tuple) -> None:
        if event not in _EVENTS:
            return
        if event.startswith("socket.connect") or event == "socket.sendto":
            target = args[1][0] if isinstance(args[1], tuple) else str(args[1])
        elif event.startswith("socket."):
            target = args[0]
        elif event == "subprocess.Popen":
            target = list(args[1])[0] if args[1] else ""
        elif event == "os.system":
            target = str(args[0])
        else:
            target = str(args[0])
        os.write(fd, (json.dumps({"event": event, "target": str(target)}) + "\n").encode())

    sys.addaudithook(hook)


def probes(results: dict) -> None:
    """Direct attempts from this process: a real local listener, the LAN, and two public destinations."""
    from micropaw import egress

    def attempt(name: str, fn):
        try:
            fn()
            results[name] = "ok"
        except egress.EgressBlocked as e:
            results[name] = f"blocked: {e}"
        except OSError as e:
            results[name] = f"os error: {type(e).__name__}"

    def to_listener(port: int, payload: bytes, host: str = "127.0.0.1") -> None:
        with socket.create_connection((host, port), timeout=3) as s:
            s.sendall(payload)

    def to_public() -> None:
        socket.create_connection(("192.0.2.1", 9), timeout=0.3).close()  # TEST-NET-1, never routed

    attempt("loopback", lambda: to_listener(int(os.environ["CANARY_PORT"]), b"direct"))
    attempt("public_address", to_public)
    attempt("public_name", lambda: socket.getaddrinfo("canary.invalid", 443))
    attempt("public_name_connect", lambda: socket.create_connection(("canary.invalid", 443), timeout=0.3))
    attempt("lan_unused", lambda: socket.create_connection(("192.168.254.254", 9), timeout=0.3).close())
    if os.environ.get("CANARY_LAN"):
        attempt("lan_listener", lambda: to_listener(int(os.environ["CANARY_LAN_PORT"]), b"lan",
                                                    os.environ["CANARY_LAN"]))


def boot() -> str:
    from micropaw import _boot
    return _boot.prepare(install_guard=os.environ.get("MICROPAW_TEST_DISABLE_GUARD") != "1")


async def full_turn(results: dict) -> None:
    from micropaw import cli
    from micropaw.channels.base import Channel
    from micropaw.config import load_config
    from micropaw.gateway import Gateway

    config = load_config()
    models, runtimes = cli._create_runtimes(config)
    main = runtimes["main"]
    done = asyncio.Event()

    class ScriptChannel(Channel):
        name = "webchat"  # an owner channel: no sign-in step

        async def start(self) -> None:
            self.task = asyncio.create_task(self.script())

        async def stop(self) -> None:
            pass

        async def send_message(self, chat_id: str, text: str) -> None:
            pass

        async def script(self) -> None:
            while not hasattr(gw, "_scheduler_task"):
                await asyncio.sleep(0.05)
            await gw._mcp_task
            results["mcp_started"] = any(t.startswith("mcp_probe_") for t in gw.registry.names)

            async def yes(*args) -> bool:
                return True

            gw.agents["main"].approver = yes
            reply = await gw._handle_message("run the canary", "owner", "webchat", None)
            results["reply"] = reply
            done.set()

    gw = Gateway(config, main.provider, cli._create_registry(config), [ScriptChannel()], main.fast, main.vision,
                 runtimes=runtimes, models=models)
    task = asyncio.create_task(gw.start())
    waiter = asyncio.create_task(done.wait())
    await asyncio.wait({task, waiter}, timeout=120, return_when=asyncio.FIRST_COMPLETED)
    results["tools"] = sorted(gw.registry.names)
    await gw.stop()
    task.cancel()
    waiter.cancel()


def main() -> None:
    recorder("pre")
    results: dict = {"mode": boot()}
    recorder("post")
    probes(results)
    if os.environ.get("CANARY_MODE", "full") == "full":
        asyncio.run(full_turn(results))
    (OUT / "results.json").write_text(json.dumps(results))


if __name__ == "__main__":
    main()
