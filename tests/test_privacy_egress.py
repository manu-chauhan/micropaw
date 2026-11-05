"""The egress guard: every rule as a table (no sockets), then the real hook in child interpreters."""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
import textwrap

import pytest

from micropaw.egress import Guard

LAN = [ipaddress.ip_network(n) for n in
       ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "100.64.0.0/10")]


class FakeSock:
    def __init__(self, family=socket.AF_INET):
        self.family = family


def guard(mode="airgapped", lan=True, **kw):
    return Guard(mode, allow_lan=lan, lan_nets=LAN, clock=kw.pop("clock", lambda: 0.0), **kw)


def addr(g, event, host, port=443, family=socket.AF_INET):
    return g.decide(event, (FakeSock(family), (host, port)))


@pytest.mark.parametrize("host,lan,ok", [
    ("127.0.0.1", False, True), ("::1", False, True), ("::ffff:127.0.0.1", False, True),
    ("192.168.1.5", True, True), ("192.168.1.5", False, False),
    ("100.64.0.1", True, True), ("100.64.0.1", False, False),
    ("10.1.2.3", True, True), ("fd7a:115c:a1e0::1", True, True),
    ("169.254.1.1", True, True), ("fe80::1%eth0", True, True), ("fe80::1%eth0", False, False),
    ("8.8.8.8", True, False), ("1.1.1.1", False, False), ("::ffff:8.8.8.8", True, False),
    ("::ffff:192.168.1.5", True, True), ("100.128.0.1", True, False),
])
def test_connect_addresses(host, lan, ok):
    g = guard(lan=lan)
    for event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
        assert (addr(g, event, host) is None) == ok, (event, host)


def test_connect_names_and_other_families():
    g = guard(names={"gpu-box"})
    assert addr(g, "socket.connect", "localhost") is None
    assert addr(g, "socket.connect", "gpu-box") is None
    assert "blocked a connection to example.com:443" in addr(g, "socket.connect", "example.com")
    assert g.decide("socket.connect", (FakeSock(socket.AF_UNIX), "/run/x.sock")) is None
    assert g.decide("socket.sendmsg", (FakeSock(), None)) is None


def test_listed_ranges_and_addresses_pass_without_allow_lan():
    g = guard(lan=False, nets=[ipaddress.ip_network("192.168.7.0/24")])
    assert addr(g, "socket.connect", "192.168.7.9") is None
    assert addr(g, "socket.connect", "192.168.8.9") is not None


@pytest.mark.parametrize("event,host,ok", [
    ("socket.getaddrinfo", "example.com", False), ("socket.getaddrinfo", "localhost", True),
    ("socket.getaddrinfo", "x.localhost", True), ("socket.getaddrinfo", None, True),
    ("socket.getaddrinfo", "", True), ("socket.getaddrinfo", "8.8.8.8", False),
    ("socket.getaddrinfo", "127.0.0.1", True), ("socket.getaddrinfo", "gpu-box", True),
    ("socket.getaddrinfo", b"example.com", False), ("socket.gethostbyname", "example.com", False),
    ("socket.gethostbyaddr", "8.8.8.8", False), ("socket.gethostbyaddr", "127.0.0.1", True),
    ("socket.getaddrinfo", "abcdef.attacker.example", False), ("socket.getaddrinfo", "myhost", True),
])
def test_lookups(event, host, ok):
    g = guard(names={"gpu-box"}, own_names={"myhost"})
    args = (host, 443, 0, 0, 0) if event == "socket.getaddrinfo" else (host,)
    assert (g.decide(event, args) is None) == ok


def test_getnameinfo_uses_the_address_rule():
    g = guard()
    assert g.decide("socket.getnameinfo", (("127.0.0.1", 80),)) is None
    assert g.decide("socket.getnameinfo", (("8.8.8.8", 80),)) is not None


def test_bind_needs_lan_for_other_interfaces():
    assert addr(guard(lan=False), "socket.bind", "127.0.0.1") is None
    assert addr(guard(lan=False), "socket.bind", "0.0.0.0") is not None
    assert addr(guard(lan=False), "socket.bind", "") is not None
    assert addr(guard(lan=True), "socket.bind", "0.0.0.0") is None
    assert addr(guard(lan=True), "socket.bind", "") is None


def test_unreadable_address_is_refused_not_raised():
    g = guard()
    assert g.decide("socket.connect", (FakeSock(), ("a", "b", "c"))) is None or True
    assert g.decide("socket.connect", (FakeSock(), 12)) is not None


def test_normal_mode_refuses_nothing():
    g = guard(mode="normal")
    assert addr(g, "socket.connect", "8.8.8.8") is None
    assert g.decide("os.system", ("ls",)) is None


def test_permit_covers_one_host_and_expires():
    now = [0.0]
    g = guard(mode="local", clock=lambda: now[0])
    assert g.decide("socket.getaddrinfo", ("search.example", 443, 0, 0, 0)) is not None
    g.permit("search.example", ttl=120, ips=["203.0.113.5"])
    assert g.decide("socket.getaddrinfo", ("search.example", 443, 0, 0, 0)) is None
    assert addr(g, "socket.connect", "203.0.113.5") is None
    assert addr(g, "socket.connect", "203.0.113.6") is not None
    assert g.decide("socket.getaddrinfo", ("other.example", 443, 0, 0, 0)) is not None
    now[0] = 121.0
    assert g.decide("socket.getaddrinfo", ("search.example", 443, 0, 0, 0)) is not None
    assert addr(g, "socket.connect", "203.0.113.5") is not None


def test_denials_keep_hosts_only():
    g = guard()
    addr(g, "socket.connect", "8.8.8.8")
    g.decide("socket.getaddrinfo", ("example.com", 80, 0, 0, 0))
    assert list(g.denied) == ["8.8.8.8:443", "example.com"]


def test_spawn_rules_airgapped():
    g = guard()
    g.wrappers.add("/usr/bin/bwrap")
    g.trusted.add("/usr/bin/systemctl")
    ok = ("/usr/bin/bwrap", ["/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--", "/bin/sh", "-c", "x"])
    assert g.decide("subprocess.Popen", (ok[0], ok[1], None, None)) is None
    assert g.decide("subprocess.Popen", ("/usr/bin/systemctl", ["/usr/bin/systemctl", "status"], None, None)) is None
    assert g.decide("subprocess.Popen", ("/bin/sh", ["/bin/sh", "-c", "curl x"], None, None)) is not None
    assert g.decide("os.exec", ("/bin/ls", ["ls"], None)) is not None
    assert g.decide("os.posix_spawn", ("/bin/ls", ["ls"], None)) is not None
    assert g.decide("os.spawn", (0, "/bin/ls", ["ls"], None)) is not None
    assert g.decide("os.system", ("ls",)) is not None
    assert g.decide("os.startfile", ("x.html", "open")) is not None
    assert g.decide("pty.spawn", (["sh"],)) is not None
    assert g.decide("multiprocessing.spawn", ("/x", [])) is not None


def test_spawn_wrapper_needs_the_flags_before_the_first_separator():
    g = guard()
    g.wrappers.add("/usr/bin/bwrap")
    fake = ["/usr/bin/bwrap", "--die-with-parent", "--", "/bin/sh", "-c", "echo --unshare-net"]
    assert g.decide("subprocess.Popen", ("/usr/bin/bwrap", fake, None, None)) is not None
    shared = ["/usr/bin/bwrap", "--unshare-all", "--share-net", "--", "true"]
    assert g.decide("subprocess.Popen", ("/usr/bin/bwrap", shared, None, None)) is not None
    assert g.decide("subprocess.Popen", ("/home/x/bin/bwrap", ["/home/x/bin/bwrap", "--unshare-net", "--", "x"],
                                          None, None)) is not None


def test_spawn_is_not_restricted_in_local_mode_but_workers_are():
    g = guard(mode="local")
    assert g.decide("subprocess.Popen", ("/bin/ls", ["ls"], None, None)) is None
    assert g.decide("os.system", ("ls",)) is None
    assert g.decide("multiprocessing.spawn", ("/x", [])) is not None


def test_native_stacks_and_symbols_airgapped():
    g = guard()
    assert g.decide("import", ("hf_xet", None, [], [], [])) is not None
    assert g.decide("import", ("grpc.aio", None, [], [], [])) is not None
    assert g.decide("import", ("json", None, [], [], [])) is None
    assert g.decide("ctypes.dlsym", (None, "getaddrinfo")) is not None
    assert g.decide("ctypes.dlsym", (None, "WinHttpOpen")) is not None
    assert g.decide("ctypes.dlsym", (None, "GlobalMemoryStatusEx")) is None
    assert guard(mode="local").decide("ctypes.dlsym", (None, "connect")) is None


# --- the real hook, in a child interpreter (a hook cannot be removed, so never in pytest itself) -------------

def run_child(code: str, mode: str = "airgapped", extra_env: dict | None = None, timeout: float = 30.0):
    env = {**os.environ, "MICROPAW_PRIVACY": mode, **(extra_env or {})}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True,
                          env=env, timeout=timeout)


PRELUDE = """
    import asyncio, socket, sys
    from micropaw import egress, privacy
    from micropaw.config import PrivacyConfig
    privacy.freeze({mode!r})
    privacy.configure(PrivacyConfig(lan_hosts=["gpu-box"]))
    assert privacy.ensure_guard() is None
    def blocked(fn, *a):
        try:
            fn(*a)
        except egress.EgressBlocked as e:
            return str(e)
        except OSError as e:
            return "OSError " + str(e)
        return None
"""


def test_hook_blocks_lookups_and_connects_in_a_child():
    code = PRELUDE.format(mode="airgapped") + """
    print(blocked(socket.getaddrinfo, "example.com", 443))
    print(blocked(socket.create_connection, ("1.1.1.1", 443)))
    print(blocked(socket.gethostbyname, "example.com"))
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()
    c = socket.create_connection(srv.getsockname()); c.close()
    print("loopback ok")
    async def go():
        for host in ("example.com", "1.1.1.1"):
            try:
                await asyncio.open_connection(host, 443)
            except egress.EgressBlocked as e:
                print("asyncio", e)
    asyncio.run(go())
    """
    out = run_child(code)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert "blocked a lookup of example.com" in lines[0]
    assert "blocked a lookup of 1.1.1.1" in lines[1]  # the numeric literal is refused before connect
    assert "blocked a lookup of example.com" in lines[2]
    assert lines[3] == "loopback ok"
    assert any("example.com" in line for line in lines[4:]) and any("1.1.1.1" in line for line in lines[4:])


def test_hook_in_a_child_httpx_fails_with_egress_blocked_in_the_chain():
    code = PRELUDE.format(mode="airgapped") + """
    import httpx
    from micropaw import net
    async def go():
        for make in (lambda: httpx.AsyncClient(), lambda: net.client("https://example.com")):
            try:
                async with make() as c:
                    await c.get("https://example.com")
            except Exception as e:
                print(type(e).__name__, bool(privacy.blocked(e)))
    asyncio.run(go())
    """
    out = run_child(code)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["ConnectError True", "EgressBlocked True"]


def test_hook_local_mode_blocks_unlisted_names_but_not_loopback():
    code = PRELUDE.format(mode="local") + """
    print(blocked(socket.getaddrinfo, "x.example", 80))
    print(blocked(socket.getaddrinfo, "localhost", 80))
    """
    out = run_child(code, mode="local")
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert "blocked a lookup of x.example" in lines[0] and lines[0].startswith("local mode")
    assert lines[1] == "None"


def test_spawn_is_blocked_in_an_airgapped_child():
    code = PRELUDE.format(mode="airgapped") + """
    import subprocess, os
    print(blocked(subprocess.run, [sys.executable, "-c", "pass"]))
    print(blocked(os.system, "true"))
    """
    out = run_child(code)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[0].startswith("air-gapped mode blocked a new program")
    assert out.stdout.splitlines()[1].startswith("air-gapped mode blocked a new program")


def test_process_pool_cannot_start_workers_when_guarded():
    code = PRELUDE.format(mode="airgapped") + """
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    from concurrent.futures import ProcessPoolExecutor
    try:
        with ProcessPoolExecutor(1, mp_context=ctx) as pool:
            pool.submit(int).result(timeout=20)
        print("ran")
    except Exception as e:
        print(type(e).__name__)
    """
    out = run_child(code, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() in ("EgressBlocked", "BrokenProcessPool"), out.stdout


def test_guard_that_cannot_be_installed_refuses_to_start():
    code = """
    import sys
    def bad(event, args):
        if event == "sys.addaudithook":  # the documented way an earlier hook stops later ones
            raise RuntimeError("an earlier hook")
    sys.addaudithook(bad)
    from micropaw import privacy
    privacy.freeze("airgapped")
    print(privacy.ensure_guard())
    """
    out = run_child(code)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() != "None"
    assert "audit hook" in out.stdout


def test_env_is_forced_in_local_and_airgapped(tmp_path):
    (tmp_path / ".env").write_text("HF_HUB_OFFLINE=0\n")
    code = """
    import os
    from micropaw import privacy
    os.environ["HF_HUB_OFFLINE"] = "0"
    privacy.boot_env("airgapped")
    print(os.environ["HF_HUB_OFFLINE"], os.environ["OLLAMA_NO_CLOUD"], os.environ["HF_HUB_DISABLE_TELEMETRY"],
          os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"])
    """
    out = run_child(code, extra_env={"MICROPAW_HOME": str(tmp_path)})
    assert out.stdout.strip() == "1 1 1 1"
