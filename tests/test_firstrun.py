"""First-run rough edges: headless with no channel, status, doctor, privacy labels, quiet start, messages."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import micropaw.cli as cli
import micropaw.config as config_mod
from micropaw import privacy

pytest_plugins = ["tests.lan_fixtures"]


def run(*args, input=None):
    return CliRunner().invoke(cli.main, list(args), input=input)


@pytest.fixture
def home(lan_home, tmp_path, monkeypatch):
    """lan_home plus the names cli bound at import, so no test reads or writes the real ~/.micropaw."""
    monkeypatch.setattr(cli, "CONFIG_FILE", lan_home)
    monkeypatch.setattr(cli, "DB_FILE", tmp_path / "data.db")
    monkeypatch.setattr(config_mod, "DB_FILE", tmp_path / "data.db")
    return lan_home


FAKE = {"provider": "ollama", "providers": {"fakeoai": {"base_url": "http://127.0.0.1:18080/v1"}},
        "agents": {"main": {"model": "fakeoai:fake-gpt"}}}


def write(home, data):
    home.write_text(json.dumps(data))


def test_headless_with_only_the_terminal_says_what_to_add_and_runs_nothing(home, monkeypatch):
    write(home, {"channels": {"terminal": {"enabled": True}}})
    monkeypatch.setattr("micropaw.service.run_headless", lambda make: pytest.fail("must not start"))
    r = run("start", "--headless")
    assert r.exit_code == 1
    assert "paw connect telegram" in r.output and "Traceback" not in r.output
    assert "paw setup" not in r.output


def test_privacy_does_not_call_an_unset_kiwix_the_internet(home):
    write(home, {"privacy": {"mode": "local"}, "search": {"provider": "offline", "kiwix_url": ""}})
    privacy._reset_for_tests()
    privacy.freeze("local")
    out = run("privacy").output
    assert "Kiwix: not set up (off)" in out and "Kiwix (the internet)" not in out


def test_status_shows_the_gateway_and_the_model_chats_really_use(home, monkeypatch):
    write(home, FAKE)
    monkeypatch.setattr("micropaw.service.running_gateway", lambda: None)
    out = run("status").output
    assert "fakeoai:fake-gpt" in out and "Gateway" in out and "not running" in out
    monkeypatch.setattr("micropaw.service.running_gateway", lambda: 4242)
    assert "running, PID 4242" in run("status").output


def test_doctor_database_not_created_is_a_note_and_a_missing_config_says_run_setup(home, monkeypatch):
    monkeypatch.setattr("micropaw.modelscan.doctor_rows", lambda config: _async([]))
    out = run("doctor").output
    assert "not found. Run 'paw setup'" in " ".join(out.split())
    assert "FAIL │ not created" not in out and "created on the first chat" in " ".join(out.split())
    db_row = next(line for line in out.splitlines() if "Database" in line)
    assert "FAIL" not in db_row


async def _async(value):
    return value


def test_doctor_provider_row_follows_the_main_model_and_keyless_models_need_no_key(home, monkeypatch):
    write(home, FAKE)
    monkeypatch.setattr("micropaw.modelscan.doctor_rows", lambda config: _async([]))
    out = " ".join(run("doctor").output.split())
    assert "Provider (fakeoai:fake-gpt)" in out and "no API key needed" in out
    assert "API key configured" not in out


def test_doctor_sandbox_off_on_purpose_in_airgapped_windows_is_a_warning():
    from micropaw.tools.sandbox import Sandbox
    assert "FAIL" in cli._sandbox_mark(Sandbox("none", error="bwrap is missing"), True)  # not air-gapped
    privacy._reset_for_tests()
    privacy.freeze("airgapped")
    try:
        assert "warn" in cli._sandbox_mark(Sandbox("none", error="Windows cannot cut a child process off"), True)
        assert "FAIL" in cli._sandbox_mark(Sandbox("none", error="bwrap is missing"), False)
    finally:
        privacy._reset_for_tests()


def test_doctor_service_state_unknown_is_a_warning_not_a_pass(home, monkeypatch):
    from micropaw import service as svc
    monkeypatch.setattr("micropaw.modelscan.doctor_rows", lambda config: _async([]))
    monkeypatch.setattr(svc, "state", lambda: (_ for _ in ()).throw(svc.ServiceError("schtasks blocked")))
    row = next(line for line in run("doctor").output.splitlines() if "Service" in line)
    assert "warn" in row and "unknown" in row


class _Model:
    """A model that answers, or fails with `error`."""

    name, model = "ollama", "m"

    def __init__(self, error=None):
        self.error = error

    async def chat(self, messages, tools=None):
        from micropaw.providers.base import AIResponse
        if self.error:
            raise self.error
        return AIResponse(content="hello", tool_calls=[], usage={"input": 1, "output": 1})

    async def chat_stream(self, messages, tools=None):
        resp = await self.chat(messages, tools)
        yield resp.content
        yield resp


def _gateway(tmp_path, monkeypatch, terminal, model):
    import asyncio

    import micropaw.gateway as gw_mod
    from micropaw.config import Config
    from micropaw.core.profile import build_runtimes
    from micropaw.providers.registry import Registry
    from micropaw.tools.base import ToolRegistry

    monkeypatch.setattr(gw_mod, "DB_FILE", tmp_path / "d.db")
    monkeypatch.setattr(gw_mod, "CONFIG_DIR", tmp_path)
    c = Config(provider="ollama")
    c.embedding.enabled = "false"
    monkeypatch.setattr("micropaw.scheduler.Scheduler.run", lambda self: asyncio.sleep(0))
    registry = Registry(c)
    runtimes = build_runtimes(c, registry)
    runtimes["main"].provider = model
    return gw_mod.Gateway(c, model, ToolRegistry(), [terminal], runtimes=runtimes, models=registry)


class _Terminal:
    name = "terminal"

    def __init__(self, work):
        self.work, self.stopped = work, False

    def set_handler(self, h):
        self.handler = h

    def set_stream_handler(self, h):
        self.stream = h

    def set_access(self, h): ...

    async def stop(self):
        self.stopped = True

    async def start(self):
        await self.work(self)


async def test_a_failing_terminal_still_stops_the_gateway_so_the_process_can_exit(tmp_path, monkeypatch):
    async def work(t):
        raise BrokenPipeError("closed")

    term = _Terminal(work)
    gw = _gateway(tmp_path, monkeypatch, term, _Model())
    stops = []
    real_stop = gw.stop

    async def stop():
        stops.append(1)
        await real_stop()

    gw.stop = stop
    with pytest.raises(BrokenPipeError):
        await gw.start()
    assert stops == [1]


async def test_an_error_reply_has_no_via_line_and_a_good_reply_has(tmp_path, monkeypatch, caplog):
    from micropaw.providers.failover import FriendlyError
    got = {}

    async def work(t):
        got["bad"] = await t.handler("hi", "u", "terminal", None)

    down = _Model(FriendlyError("The box is not running.", OSError("refused")))
    gw = _gateway(tmp_path, monkeypatch, _Terminal(work), down)
    with caplog.at_level("INFO"):
        await gw.start()
    assert got["bad"] == "The box is not running."
    assert not [r for r in caplog.records if r.levelname == "ERROR" and "AI provider error" in r.message]
    assert [r for r in caplog.records if r.levelname == "INFO" and "AI provider error" in r.message]

    async def good(t):
        got["good"] = await t.handler("hi", "u", "terminal", None)

    await _gateway(tmp_path, monkeypatch, _Terminal(good), _Model()).start()
    assert "[via" in got["good"]


async def test_a_screen_error_is_not_blamed_on_the_provider(tmp_path, monkeypatch):
    async def work(t):
        async def chunk(text):
            raise UnicodeEncodeError("charmap", "x", 0, 1, "cannot encode")
        await t.stream("hi", "u", "terminal", None, chunk)

    from micropaw.core.agent import OutputError
    gw = _gateway(tmp_path, monkeypatch, _Terminal(work), _Model())
    with pytest.raises(OutputError):
        await gw.start()


def test_run_gateway_exits_one_when_the_output_fails():
    class Gw:
        async def start(self):
            raise BrokenPipeError("closed")

    with pytest.raises(SystemExit) as e:
        cli._run_gateway(Gw())
    assert e.value.code == 1


def test_output_that_cannot_hold_a_check_mark_or_emoji_is_replaced_not_a_crash(monkeypatch):
    import io
    import sys
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    cli._safe_streams()
    sys.stdout.write("ollama \u2713 caf\u00e9 \U0001f43e")
    sys.stdout.flush()
    assert raw.getvalue().decode("cp1252") == "ollama ? caf\u00e9 ?"


def test_setup_without_a_terminal_says_so_in_one_plain_line(home):
    r = run("setup", input="\n")
    text = " ".join(r.output.split())
    assert r.exit_code == 1 and "paw setup needs a terminal" in text and "docker run -it" in text
    assert "Traceback" not in r.output and "Aborted" not in r.output


def test_connect_webchat_prints_the_link_to_open(home, monkeypatch, capsys):
    import questionary

    import micropaw.connector as conn

    class Answer:
        def __init__(self, value):
            self.value = value

        def ask(self):
            return self.value

    monkeypatch.setattr(questionary, "text", lambda msg, default="": Answer(default))
    monkeypatch.setattr(questionary, "confirm", lambda msg, default=True: Answer(True))
    monkeypatch.setattr(conn, "save_config", lambda config: None)
    config = config_mod.Config()
    conn._connect_webchat(config)
    out = capsys.readouterr().out.replace("\n", "")
    assert f"http://127.0.0.1:8765/?token={config.channels.webchat.auth_token}" in out and "paw start" in out


def test_a_webchat_without_a_token_gets_one_that_is_kept_across_restarts(home):
    write(home, {"channels": {"webchat": {"enabled": True, "port": 1}}})
    first = cli._create_channels(config_mod.load_config())[-1].auth_token
    assert first and json.loads(home.read_text())["channels"]["webchat"]["auth_token"] == first
    assert cli._create_channels(config_mod.load_config())[-1].auth_token == first


def test_the_webchat_link_names_the_address_a_browser_can_open():
    from micropaw.channels.webchat import link
    assert link("0.0.0.0", 8765, "a b") == "http://127.0.0.1:8765/?token=a%20b"
    assert link("192.168.1.5", 80, "t") == "http://192.168.1.5:80/?token=t"


def test_models_remove_in_use_says_how_to_change_it(home):
    write(home, FAKE)
    r = run("models", "remove", "fakeoai")
    text = " ".join(r.output.split())
    assert r.exit_code == 1 and "agents.main.model" in text
    assert "paw models add <name> <address> --use main" in text and "Change those first" not in text


def test_service_stop_still_stops_a_running_gateway_when_the_service_query_is_blocked(monkeypatch):
    from micropaw import service as svc
    killed = []

    def blocked():
        raise svc.ServiceError("air-gapped mode blocked a new program schtasks")

    monkeypatch.setattr(svc, "existing_definition", blocked)
    monkeypatch.setattr(svc, "running_gateway", lambda: 4242)
    monkeypatch.setattr(svc, "request_stop", lambda: killed.append("stop-file"))
    monkeypatch.setattr(svc.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(svc, "wait_exit", lambda pid, timeout: True)
    assert svc.stop() is True
    assert killed in (["stop-file"], [4242])  # Windows writes the stop file, the others send a signal


def test_service_status_prints_the_gateway_line_even_when_the_service_query_is_blocked(monkeypatch):
    from micropaw import service as svc

    def blocked():
        raise svc.ServiceError("air-gapped mode blocked schtasks")

    monkeypatch.setattr(svc, "state", blocked)
    monkeypatch.setattr(svc, "healthy", lambda: (True, "running, PID 7"))
    r = run("service", "status")
    assert r.exit_code == 0 and "Service: unknown" in r.output and "Gateway: running, PID 7" in r.output


def test_update_does_not_offer_a_downgrade_when_pypi_is_behind(monkeypatch):
    from micropaw import service as svc
    ran = []
    monkeypatch.setattr(svc, "install_method", lambda: "uv")
    monkeypatch.setattr(svc, "latest_version", lambda: "0.0.1")
    monkeypatch.setattr(cli, "_run_install_command", lambda cmd: ran.append(cmd))
    r = run("update", input="y\n")
    assert not ran and "Upgrade now?" not in r.output and "up to date" in r.output
    assert cli._version_key("0.10.0") > cli._version_key("0.9.9")


def test_paw_stop_is_the_same_as_paw_service_stop(monkeypatch):
    from micropaw import service as svc
    monkeypatch.setattr(svc, "stop", lambda: True)
    r = run("stop")
    assert r.exit_code == 0 and "Stopped." in r.output
    assert "paw stop" in run("start", "--help").output
