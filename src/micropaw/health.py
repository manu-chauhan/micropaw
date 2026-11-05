"""Is the gateway alive? Standard library only, so `python -m micropaw.health` (the Docker healthcheck) runs in
tens of milliseconds and loads no pydantic, httpx or rich. Same checks as service.healthy()."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def home() -> Path:
    """The data folder, found the way config.py finds it."""
    return Path(os.environ.get("MICROPAW_HOME") or Path.home() / ".micropaw")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # never on Windows: there it would terminate the process
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_name(pid: int) -> str | None:
    """Command line (POSIX) or image path (Windows) of a process; None when it can't be read."""
    try:
        if sys.platform == "linux":
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        if sys.platform == "darwin":
            return subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                                  timeout=5).stdout
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = k32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                return buf.value if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else None
            finally:
                k32.CloseHandle(handle)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def is_paw(pid: int) -> bool:
    name = process_name(pid)
    if not name:
        return True  # can't tell: assume yes, so two gateways never run by mistake
    keys = ("paw", "python") if os.name == "nt" else ("paw",)
    return any(k in name.lower() for k in keys)


def check() -> tuple[bool, str]:
    """True when the gateway process is alive (and is ours) and the database opens."""
    folder = home()
    try:
        pid = int((folder / "micropaw.pid").read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if not pid or not pid_alive(pid) or not is_paw(pid):
        return False, "gateway is not running"
    try:
        con = sqlite3.connect(f"{(folder / 'data.db').as_uri()}?mode=ro", uri=True, timeout=3)
        try:
            con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        return False, f"database does not open: {e}"
    return True, f"running, PID {pid}"


def main() -> int:
    ok, why = check()
    print(why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
