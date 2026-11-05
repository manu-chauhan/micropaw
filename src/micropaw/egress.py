"""The egress guard: a process-wide audit hook that refuses network and spawn events outside the policy.

`Guard.decide` holds every rule and works without sockets, so it is unit tested directly. `install` adds the
hook (PEP 578). A hook cannot be removed. It is a guardrail, not the boundary: children and native code are
handled by the sandbox (tools/sandbox.py) and by refusing features at construction (privacy.py).
The hook body imports nothing and does not log: it can run inside the logging and import machinery.
"""

from __future__ import annotations

import collections
import ipaddress
import os
import shutil
import socket
import sys
import time
from collections.abc import Callable, Iterable

SELFTEST_EVENT = "micropaw.egress.selftest"
_INET = (socket.AF_INET, socket.AF_INET6)
# Names that give a process its own way out: native network stacks and shells that open URLs.
_BLOCKED_IMPORTS = frozenset({"hf_xet", "grpc", "pycurl", "curl_cffi"})
_BLOCKED_SYMBOLS = frozenset({
    "connect", "sendto", "sendmsg", "getaddrinfo", "WSAConnect", "ShellExecuteW", "ShellExecuteExW",
    "CreateProcessW", "CreateProcessA",
})
_BLOCKED_SYMBOL_PREFIXES = ("WSASend", "WinHttp", "Internet")


class EgressBlocked(PermissionError):  # noqa: N818 - the name the plan and the docs use
    """The privacy mode refused a connection, a name lookup or a new program."""


def _clean(host: object) -> str:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    return str(host).split("%", 1)[0].strip().lower().rstrip(".")


def parse_ip(host: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """An address from a literal, with IPv4-mapped IPv6 unwrapped and a scope id dropped. None for a name."""
    try:
        ip = ipaddress.ip_address(_clean(host))
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


class Guard:
    """The rules for one process. `mode` is airgapped or local; in normal mode nothing is refused."""

    def __init__(
        self, mode: str, *, allow_lan: bool = True, nets: Iterable[object] = (), lan_nets: Iterable[object] = (),
        names: Iterable[str] = (), own_names: Iterable[str] = (), clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mode = mode
        self.allow_lan = allow_lan
        self.nets = tuple(nets)            # owner-listed addresses and ranges (privacy.lan_hosts)
        self.lan_nets = tuple(lan_nets)    # the private ranges that count as "my network" when allow_lan is on
        self.names = frozenset(n.lower() for n in names)  # owner-listed names, never decided by DNS
        self.own = frozenset(n.lower() for n in own_names)
        self.clock = clock
        self.denied: collections.deque[str] = collections.deque(maxlen=50)  # hosts only, for /privacy
        self._names_ok: dict[str, float] = {}
        self._ips_ok: dict[str, float] = {}
        self.wrappers: set[str] = set()    # absolute paths of the programs that cut a child off the network
        self.trusted: set[str] = set()     # absolute paths of system tools the service code may run
        self.spawn_log: list[list[str]] = []

    # -- permits: short-lived exceptions the owner approved (local mode only) ------------------------------

    def permit(self, host: str, ttl: float = 120.0, ips: Iterable[str] = ()) -> None:
        until = self.clock() + ttl
        if host:
            self._names_ok[_clean(host)] = until
        for ip in ips:
            self._ips_ok[str(parse_ip(ip) or ip)] = until

    def _live(self, table: dict[str, float], key: str) -> bool:
        until = table.get(key)
        if until is None:
            return False
        if until < self.clock():
            del table[key]
            return False
        return True

    # -- rules -------------------------------------------------------------------------------------------------

    def address_ok(self, host: object, *, bind: bool = False) -> bool:
        ip = parse_ip(host)
        if ip is None:
            return False
        if ip.is_loopback or (ip.is_unspecified and not bind):
            return True
        if bind:
            return self.allow_lan  # a listener on another interface is a LAN service
        if any(ip in net for net in self.nets):
            return True
        if self.allow_lan and (ip.is_link_local or any(ip in net for net in self.lan_nets)):
            return True
        return self._live(self._ips_ok, str(ip))

    def name_ok(self, host: object) -> bool:
        if host is None or host == "" or host == b"":
            return True
        name = _clean(host)
        if not name or name == "localhost" or name.endswith(".localhost"):
            return True
        if parse_ip(name) is not None:
            return self.address_ok(name)
        return name in self.names or name in self.own or self._live(self._names_ok, name)

    def host_ok(self, host: object) -> bool:
        return self.address_ok(host) if parse_ip(host) is not None else self.name_ok(host)

    def _refuse(self, what: str, host: object = "") -> str:
        label = _clean(host) or "?"
        self.denied.append(label)
        word = "air-gapped" if self.mode == "airgapped" else "local"
        return f"{word} mode blocked {what} {label}".strip()

    def check_host(self, host: object) -> str | None:
        """Used by the HTTP client hook, before any lookup. A refusal text, or None."""
        if self.mode == "normal" or self.host_ok(host):
            return None
        return self._refuse("a connection to", host)

    # -- the events ----------------------------------------------------------------------------------------------

    def decide(self, event: str, args: tuple) -> str | None:
        """A refusal text for this audit event, or None to let it happen."""
        if self.mode == "normal":
            return None
        try:
            return self._decide(event, args)
        except Exception:  # an address this code cannot read is refused, never passed
            return self._refuse("an unreadable address for", event)

    def _decide(self, event: str, args: tuple) -> str | None:
        if event.startswith("socket."):
            return self._socket(event, args)
        if event == "multiprocessing.spawn":
            return self._refuse("a worker process", "")
        if event == "import":
            if self.mode == "airgapped" and str(args[0]).split(".", 1)[0] in _BLOCKED_IMPORTS:
                return self._refuse("the library", args[0])
            return None
        if event in ("ctypes.dlsym", "ctypes.dlsym/handle"):
            name = args[1] if len(args) > 1 else None
            if self.mode == "airgapped" and isinstance(name, str) and (
                    name in _BLOCKED_SYMBOLS or name.startswith(_BLOCKED_SYMBOL_PREFIXES)):
                return self._refuse("the native call", name)
            return None
        if self.mode == "airgapped":
            return self._spawn(event, args)
        return None

    def _socket(self, event: str, args: tuple) -> str | None:
        if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            host = args[0]
            return None if self.host_ok(host) else self._refuse("a lookup of", host)
        if event == "socket.getnameinfo":
            host = args[0][0] if args and isinstance(args[0], tuple) and args[0] else ""
            return None if self.host_ok(host) else self._refuse("a lookup of", host)
        if event in ("socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind"):
            sock, address = args[0], args[1] if len(args) > 1 else None
            if address is None or getattr(sock, "family", None) not in _INET:
                return None  # AF_UNIX and other local families
            host = address[0]
            if event == "socket.bind":
                if host in ("", None, b""):
                    ok = self.allow_lan  # a listener on every interface is a LAN service
                else:
                    ok = self.address_ok(host, bind=True) if parse_ip(host) is not None else self.name_ok(host)
                return None if ok else self._refuse("a listener on", host)
            return None if self.host_ok(host) else self._refuse("a connection to", f"{host}:{address[1]}")
        return None

    def _trusted(self, exe: str) -> bool:
        """A system tool the service code runs (systemctl and the like), by its exact resolved path."""
        if os.path.basename(exe) == exe:  # a bare name: whatever PATH finds now must be the system one
            exe = shutil.which(exe) or exe
        return os.path.normcase(exe) in self.trusted

    def _spawn(self, event: str, args: tuple) -> str | None:
        if event == "subprocess.Popen":
            argv = args[1]
        elif event in ("os.exec", "os.posix_spawn"):
            argv = args[1]
        elif event == "os.spawn":
            argv = args[2]
        elif event == "pty.spawn":
            argv = args[0]
        elif event == "_winapi.CreateProcess":
            line = str(args[1] or "").strip()
            exe = str(args[0] or (line.split('"')[1] if line.startswith('"') else line.split(" ")[0]))
            return None if self._trusted(exe) else self._refuse("a new program", exe)
        elif event in ("os.system", "os.startfile"):
            return self._refuse("a new program", event)
        else:
            return None
        if isinstance(argv, (str, bytes)):
            argv = [argv]
        argv = [os.fsdecode(a) for a in argv]
        if not argv:
            return self._refuse("an empty command", event)
        self.spawn_log.append(argv[:1])
        exe = os.path.normcase(argv[0])
        if self._trusted(argv[0]):
            return None
        if exe in self.wrappers:
            head = argv[1:argv.index("--")] if "--" in argv else argv[1:]  # only the flags before the first --
            if "--unshare-net" in head or "--unshare-all" in head:
                if "--share-net" not in head:
                    return None
            elif os.path.basename(exe).startswith("sandbox-exec") and "-p" in head:
                return None
        return self._refuse("a new program", argv[0])


_installed: Guard | None = None


def install(guard: Guard) -> str | None:
    """Add the hook. Returns None when it works, else the reason it does not. Repeated calls keep the first."""
    global _installed
    if _installed is not None:
        return None
    table = guard

    def hook(event: str, args: tuple) -> None:
        if event == SELFTEST_EVENT:
            raise EgressBlocked("egress guard selftest")
        reason = table.decide(event, args)
        if reason:
            raise EgressBlocked(reason)

    try:
        sys.addaudithook(hook)
    except Exception as e:
        return f"the audit hook could not be added ({e})"
    try:
        sys.audit(SELFTEST_EVENT)
    except EgressBlocked:
        _installed = guard
        _wrap_multiprocessing(guard)
        return None
    except Exception:
        pass
    return "the audit hook did not run (another hook stopped it from being added)"


def installed() -> Guard | None:
    return _installed


def _wrap_multiprocessing(guard: Guard) -> None:
    """multiprocessing's spawn and forkserver children raise no audit event and start with no hook."""
    try:
        from multiprocessing import util
    except ImportError:
        return
    original = getattr(util, "spawnv_passfds", None)
    if original is None:
        return

    def spawnv_passfds(path, args, passfds):
        reason = guard.decide("multiprocessing.spawn", (path, args))
        if reason:
            raise EgressBlocked(reason)
        return original(path, args, passfds)

    util.spawnv_passfds = spawnv_passfds
    try:  # popen_spawn_posix and popen_forkserver bind the name at import
        import multiprocessing.popen_forkserver as pf
        import multiprocessing.popen_spawn_posix as ps
        for mod in (pf, ps):
            if hasattr(mod.util, "spawnv_passfds"):
                mod.util.spawnv_passfds = spawnv_passfds
    except ImportError:
        pass
