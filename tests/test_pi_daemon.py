"""The idle daemon: no stop-file polling on POSIX, plain logs off a terminal, light imports, a light healthcheck."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from micropaw import config, health
from micropaw import service as svc
from micropaw.utils import logger as logmod


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "mp")
    monkeypatch.setattr(config, "DB_FILE", tmp_path / "mp" / "data.db")
    (tmp_path / "mp").mkdir()
    return tmp_path / "mp"


def _run(code: str, home: Path, **env) -> subprocess.CompletedProcess:
    full = {**os.environ, "MICROPAW_HOME": str(home), **env}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], env=full, capture_output=True, text=True,
                          timeout=60)


# --- stop: signal on POSIX, stop file on Windows ---

class _Hang:
    async def start(self):
        await asyncio.sleep(3600)

    async def stop(self):
        pass


def test_supervisor_starts_no_stop_file_watcher_on_posix(monkeypatch):
    started = []

    async def spy(stop, poll):
        started.append(poll)

    monkeypatch.setattr(svc, "_watch_stop_file", spy)

    class Clean(_Hang):
        async def start(self):
            return None

    with monkeypatch.context() as m:
        m.setattr(svc.os, "name", "posix")
        assert asyncio.run(svc.supervise(Clean)) == 0
    assert started == []


def test_supervisor_watches_the_stop_file_every_15_seconds_on_windows(monkeypatch):
    started = []

    async def spy(stop, poll):
        started.append(poll)

    monkeypatch.setattr(svc, "_watch_stop_file", spy)

    class Clean(_Hang):
        async def start(self):
            return None

    with monkeypatch.context() as m:
        m.setattr(svc, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
        assert asyncio.run(svc.supervise(Clean)) == 0
    assert started == [15.0]


@pytest.mark.skipif(os.name == "nt", reason="stops with a signal on POSIX; Windows uses the stop file")
def test_stop_ends_a_gateway_started_by_hand(home, monkeypatch):
    code = """
        import asyncio
        from micropaw import service as svc
        class Hang:
            async def start(self): await asyncio.sleep(3600)
            async def stop(self): pass
        raise SystemExit(svc.run_headless(Hang))  # micropaw: the process name has "paw" in it
    """
    child = subprocess.Popen([sys.executable, "-c", textwrap.dedent(code)],
                             env={**os.environ, "MICROPAW_HOME": str(home)})
    threading.Thread(target=child.wait, daemon=True).start()  # reap it, or a zombie still looks alive
    try:
        for _ in range(100):
            if svc.read_pid() == child.pid:
                break
            time.sleep(0.1)
        assert svc.read_pid() == child.pid
        monkeypatch.setattr(svc, "existing_definition", lambda: None)  # no service installed: started by hand
        start = time.monotonic()
        assert svc.stop(timeout=10) is True
        assert child.wait(5) == 0 and time.monotonic() - start < 5
    finally:
        if child.poll() is None:
            child.kill()


# --- the healthcheck ---

def test_health_is_false_without_a_gateway(home):
    ok, why = health.check()
    assert not ok and "not running" in why


def test_health_needs_a_live_paw_process_and_a_readable_database(home, monkeypatch):
    monkeypatch.setenv("MICROPAW_HOME", str(home))
    (home / "micropaw.pid").write_text(str(os.getpid()))
    sqlite3.connect(home / "data.db").executescript("CREATE TABLE t (x);")
    monkeypatch.setattr(health, "process_name", lambda pid: "/x/python /x/bin/paw start --headless")
    assert health.check() == (True, f"running, PID {os.getpid()}")
    monkeypatch.setattr(health, "process_name", lambda pid: "/usr/bin/vim notes")  # the PID was reused
    assert health.check()[0] is False
    monkeypatch.setattr(health, "process_name", lambda pid: "paw")
    (home / "micropaw.pid").write_text("99999999")  # nobody
    assert health.check()[0] is False
    (home / "micropaw.pid").write_text(str(os.getpid()))
    (home / "data.db").write_bytes(b"not a database" * 100)
    ok, why = health.check()
    assert not ok and "database" in why


def test_health_main_exit_codes(home, monkeypatch, capsys):
    monkeypatch.setenv("MICROPAW_HOME", str(home))
    assert health.main() == 1
    assert "not running" in capsys.readouterr().out


def test_health_module_loads_no_heavy_packages(home):
    r = _run("""
        import sys
        import micropaw.health as h
        h.main()
        heavy = [m for m in ("pydantic", "httpx", "rich", "click", "dotenv") if m in sys.modules]
        print("HEAVY", heavy)
    """, home)
    assert "HEAVY []" in r.stdout, r.stdout + r.stderr


def test_docker_healthcheck_uses_the_light_module():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
    check = dockerfile.split("HEALTHCHECK")[1].splitlines()[0]
    assert 'CMD ["python", "-m", "micropaw.health"]' in check and "doctor" not in check


# --- logging ---

@pytest.fixture
def fresh_logging(tmp_path, monkeypatch):
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logmod._configured, logmod._log_content)
    root.handlers = []
    logmod._configured = False
    monkeypatch.setenv("MICROPAW_LOG_FILE", str(tmp_path / "test.log"))
    monkeypatch.delenv("MICROPAW_IN_DOCKER", raising=False)
    monkeypatch.setattr(logmod, "_in_docker", lambda: False)
    # pytest keeps its own handlers on the root logger, which makes basicConfig a no-op: add ours directly
    def basic_config(**kw):
        for h in kw["handlers"]:
            root.addHandler(h)
        root.setLevel(kw["level"])

    monkeypatch.setattr(logging, "basicConfig", basic_config)
    yield tmp_path / "test.log"
    for h in root.handlers:
        h.close()
    root.handlers, root.level = saved[0], saved[1]
    logmod._configured, logmod._log_content = saved[2], saved[3]


def test_off_a_terminal_the_console_handler_is_plain_and_warning_only(fresh_logging, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    sys.modules.pop("rich.logging", None)
    logmod.setup_logging()
    console = next(h for h in logging.getLogger().handlers if h.name == "micropaw-console")
    assert type(console) is logging.StreamHandler and console.level == logging.WARNING
    assert "rich.logging" not in sys.modules
    logging.getLogger("t").info("[web_search] kept")
    assert "[web_search] kept" in fresh_logging.read_text()  # the file still gets every line


def test_in_docker_stderr_keeps_info_lines(fresh_logging, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(logmod, "_in_docker", lambda: True)
    logmod.setup_logging()
    console = next(h for h in logging.getLogger().handlers if h.name == "micropaw-console")
    assert console.level == logging.NOTSET


def test_quiet_console_silences_the_console_but_not_the_file(fresh_logging, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    logmod.setup_logging()
    for h in logging.getLogger().handlers:
        h.setLevel(logging.INFO)
    logmod.quiet_console()
    levels = {h.name or type(h).__name__: h.level for h in logging.getLogger().handlers}
    assert levels["micropaw-console"] == logging.WARNING and levels["_PrivateFileHandler"] == logging.INFO


def test_plain_handler_gets_the_redact_filter(fresh_logging, monkeypatch):
    from micropaw.utils import redact
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False, raising=False)
    logmod.setup_logging()
    redact.set_known_secrets({"k": "sk-secret-value-123456"})
    try:
        logging.getLogger("t").warning("key is sk-secret-value-123456")
    finally:
        redact.set_known_secrets({})
    assert "sk-secret" not in fresh_logging.read_text()


def test_plain_logging_costs_microseconds_per_record():
    handler = logging.StreamHandler(open(os.devnull, "w"))
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    log = logging.getLogger("bench-plain")
    log.handlers, log.propagate = [handler], False
    log.setLevel(logging.INFO)
    start = time.perf_counter()
    for _ in range(2000):
        log.info("[%s] %s", "tool", "args")
    assert (time.perf_counter() - start) / 2000 < 50e-6


# --- imports ---

def test_httpx_does_not_load_its_command_line_tool(home):
    r = _run("""
        import sys
        import micropaw, httpx
        print("LOADED", [m for m in ("rich", "pygments", "markdown_it") if m in sys.modules], callable(httpx.main))
    """, home)
    assert "LOADED [] True" in r.stdout, r.stdout + r.stderr


def test_headless_start_path_loads_no_rich(home):
    r = _run("""
        import sys
        from micropaw import cli
        from micropaw.config import Config
        from micropaw.gateway import Gateway
        from micropaw.utils.logger import setup_logging
        setup_logging()
        c = Config(provider="ollama")
        c.channels.terminal.enabled = False  # what _start_headless does
        c.channels.webchat.enabled = True
        cli._create_channels(c)
        cli._create_registry(c)
        print("LOADED", [m for m in ("rich", "pygments", "markdown_it") if m in sys.modules])
    """, home)
    assert "LOADED []" in r.stdout, r.stdout + r.stderr


def test_headless_make_turns_the_terminal_channel_off(home, monkeypatch):
    seen = {}
    monkeypatch.setattr("micropaw.service.run_headless", lambda make: (seen.setdefault("gw", make()), 0)[1])
    monkeypatch.setattr("micropaw.cli._make_gateway", lambda config, registry, channels: channels)
    monkeypatch.setattr("micropaw.cli._create_registry", lambda config: None)
    from micropaw import cli
    config.CONFIG_FILE.write_text(json.dumps({"channels": {"webchat": {"enabled": True, "port": 1}}}))
    monkeypatch.setattr(cli, "load_config", config.load_config)
    assert cli._start_headless() == 0
    assert [c.name for c in seen["gw"]] == ["webchat"]


def test_install_scripts_compile_bytecode():
    root = Path(__file__).parents[1]
    assert "uv tool install --compile-bytecode" in (root / "install.sh").read_text()
    assert "uv tool install --compile-bytecode" in (root / "install.ps1").read_text()


def test_bench_script_is_standard_library_only_and_prints_help():
    script = Path(__file__).parents[1] / "scripts" / "bench.py"
    import ast
    imports = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(script.read_text()))
               if isinstance(n, ast.Import)} | {n.module.split(".")[0] for n in ast.walk(ast.parse(script.read_text()))
                                                if isinstance(n, ast.ImportFrom) and n.module}
    assert "micropaw" not in imports and "httpx" not in imports
    r = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "--idle" in r.stdout
