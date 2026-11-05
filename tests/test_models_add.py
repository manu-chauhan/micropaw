"""paw models add | remove | scan (hosts, configured servers, network) and the doctor's server rows."""

from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

import micropaw.modelscan as ms
from micropaw.cli import main
from micropaw.config import load_config

pytest_plugins = ["tests.lan_fixtures"]


def run(*args, input=None):
    return CliRunner().invoke(main, list(args), input=input)


def saved(path):
    return json.loads(path.read_text())


# --- paw models add ---

def test_add_probes_saves_and_prints_the_spec(lan_home, model_server):
    box = model_server(models=("qwen3.6:27b", "tiny"))
    r = run("models", "add", "box", f"127.0.0.1:{box.port}")
    assert r.exit_code == 0, r.output
    assert "box:qwen3.6:27b" in r.output
    entry = saved(lan_home)["providers"]["box"]
    assert entry == {"base_url": box.url, "kind": "ollama"}
    assert load_config().providers["box"].base_url == box.url
    assert {p for p, _ in box.requests} >= {"/api/tags"}


def test_add_use_main_writes_the_main_agent_model(lan_home, model_server):
    box = model_server()
    assert run("models", "add", "box", f"127.0.0.1:{box.port}", "--use", "main").exit_code == 0
    assert saved(lan_home)["agents"]["main"]["model"] == "box:qwen3.6:27b"
    assert load_config().agents["main"].model == "box:qwen3.6:27b"


@pytest.mark.parametrize("use,where", [("fast", ("models", "fast")), ("vision", ("models", "vision"))])
def test_add_use_fast_or_vision_and_fallback(lan_home, model_server, use, where):
    box = model_server(kind="openai", models=("m1",))
    assert run("models", "add", "box", f"127.0.0.1:{box.port}", "--use", use).exit_code == 0
    assert saved(lan_home)[where[0]][where[1]] == "box:m1"
    assert saved(lan_home)["providers"]["box"]["base_url"] == f"{box.url}/v1"  # OpenAI-style keeps /v1


def test_add_fallback_appends(lan_home, model_server):
    box = model_server()
    lan_home.write_text(json.dumps({"fallback_providers": ["claude:x"]}))
    run("models", "add", "box", f"127.0.0.1:{box.port}", "--use", "fallback")
    assert saved(lan_home)["fallback_providers"] == ["claude:x", "box:qwen3.6:27b"]


def test_add_a_server_that_wants_a_key_prints_the_hint_and_writes_nothing(lan_home, model_server):
    box = model_server(key="secret")
    r = run("models", "add", "box", f"127.0.0.1:{box.port}")
    assert r.exit_code != 0
    assert "needs a key" in r.output and "--key-env NAME" in r.output
    assert not lan_home.exists()


def test_add_with_key_env_sends_the_bearer_and_never_stores_the_value(lan_home, model_server, monkeypatch):
    monkeypatch.setenv("GPUBOX_KEY", "secret-123")
    box = model_server(key="secret-123")
    r = run("models", "add", "gpubox", f"127.0.0.1:{box.port}", "--key-env", "GPUBOX_KEY")
    assert r.exit_code == 0, r.output
    assert ("/api/tags", "Bearer secret-123") in box.requests
    text = lan_home.read_text()
    assert "secret-123" not in text and saved(lan_home)["providers"]["gpubox"]["api_key_env"] == "GPUBOX_KEY"


def test_add_key_env_that_is_not_set_is_a_plain_error(lan_home, monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    r = run("models", "add", "box", "127.0.0.1:1", "--key-env", "NOPE_KEY")
    assert r.exit_code != 0 and "NOPE_KEY is not set" in r.output


@pytest.mark.parametrize("name", ["ollama", "groq", "claude", "Bad Name", "-x"])
def test_add_refuses_builtin_preset_and_bad_names(lan_home, name):
    r = run("models", "add", name, "127.0.0.1:1")
    assert r.exit_code != 0 and not lan_home.exists()


def test_add_refuses_plain_http_to_a_public_address_before_sending_anything(lan_home, monkeypatch):
    sent = []

    async def spy(*a, **k):
        sent.append(a)

    monkeypatch.setattr(ms, "detect", spy)
    monkeypatch.setattr(ms, "probe_host", spy)
    r = run("models", "add", "far", "http://203.0.113.5:8000")
    assert r.exit_code != 0 and "plain http" in r.output.replace("\n", " ")
    assert not sent and not lan_home.exists()


def test_add_warns_when_another_computer_answers_without_a_key(lan_home, model_server, monkeypatch):
    box = model_server()
    monkeypatch.setattr(ms, "is_loopback", lambda url: False)
    r = run("models", "add", "box", f"127.0.0.1:{box.port}")
    assert "Anyone on your network can use this server" in r.output and "README" in r.output
    r = run("models", "add", "box2", f"127.0.0.1:{box.port}")  # same, second name
    assert r.exit_code == 0


def test_add_nothing_answering_saves_nothing_unless_yes(lan_home):
    r = run("models", "add", "box", "127.0.0.1:1")
    assert r.exit_code != 0 and "Nothing answered" in r.output and not lan_home.exists()
    assert run("models", "add", "box", "127.0.0.1:1", "--yes").exit_code == 0
    assert "box" in saved(lan_home)["providers"]


def test_add_without_a_port_tries_the_known_ports_of_that_host(lan_home, model_server, monkeypatch):
    box = model_server()
    monkeypatch.setattr(ms, "KNOWN_PORTS", ((box.port, "Ollama", "ollama"), (1, "vLLM", "openai")))
    r = run("models", "add", "box", "127.0.0.1")
    assert r.exit_code == 0, r.output
    assert saved(lan_home)["providers"]["box"]["base_url"] == box.url


def test_add_asks_when_more_than_one_port_answers(lan_home, model_server, monkeypatch):
    a, b = model_server(), model_server(kind="openai", models=("other",))
    monkeypatch.setattr(ms, "KNOWN_PORTS", ((a.port, "Ollama", "ollama"), (b.port, "vLLM", "openai")))
    r = run("models", "add", "box", "127.0.0.1", input="2\n")
    assert r.exit_code == 0 and "1. " in r.output and "2. " in r.output
    assert saved(lan_home)["providers"]["box"]["base_url"].startswith(b.url)


# --- paw models remove ---

def test_remove_refuses_while_something_uses_it_then_removes(lan_home, model_server):
    box = model_server()
    run("models", "add", "box", f"127.0.0.1:{box.port}")
    raw = saved(lan_home)
    raw["models"] = {"fast": "box:qwen3.6:27b"}
    lan_home.write_text(json.dumps(raw))
    r = run("models", "remove", "box")
    assert r.exit_code != 0 and "models.fast" in r.output and "box" in saved(lan_home)["providers"]
    raw["models"] = {}
    lan_home.write_text(json.dumps(raw))
    assert run("models", "remove", "box").exit_code == 0
    assert "box" not in saved(lan_home).get("providers", {})


def test_remove_an_unknown_name_is_a_plain_error(lan_home):
    assert "no server named" in run("models", "remove", "nope").output


# --- paw models scan ---

def test_scan_lists_the_ollama_host_and_provider_entries_with_their_key(lan_home, model_server, monkeypatch):
    monkeypatch.setenv("BOXKEY", "k1")
    a = model_server(models=("alpha",))
    b = model_server(kind="openai", models=("beta",), key="k1")
    lan_home.write_text(json.dumps({"provider_config": {"ollama": {"host": a.url}},
                                    "providers": {"box": {"base_url": f"{b.url}/v1", "api_key_env": "BOXKEY"}}}))
    r = run("models", "scan")
    assert r.exit_code == 0, r.output
    assert "ollama:alpha" in r.output and "box:beta" in r.output
    assert ("/v1/models", "Bearer k1") in b.requests
    assert all(h == "" for _, h in a.requests)  # the key went to its own host only


def test_scan_prints_a_configured_server_that_does_not_answer(lan_home):
    lan_home.write_text(json.dumps({"providers": {"box": {"base_url": "http://127.0.0.1:1/v1"}}}))
    r = run("models", "scan")
    assert "box at http://127.0.0.1:1" in r.output and "not answering (asleep, off, or a firewall)" in r.output


def test_scan_host_finds_a_server_on_a_known_port_and_sends_no_key(lan_home, model_server, monkeypatch):
    box = model_server(models=("m",))
    monkeypatch.setattr(ms, "KNOWN_PORTS", ((box.port, "Ollama", "ollama"),))
    r = run("models", "scan", "--host", "127.0.0.1")
    assert "paw models add" in r.output and f"127.0.0.1:{box.port}" in r.output
    assert all(h == "" for _, h in box.requests)


def test_scan_a_remote_server_is_sized_for_that_computer_not_this_one(lan_home, model_server, monkeypatch):
    box = model_server(models=("huge", "small"))
    lan_home.write_text(json.dumps({"provider_config": {"ollama": {"host": box.url}}}))
    monkeypatch.setattr("micropaw.providers.ollama.ram_bytes", lambda: 10**9)  # this computer has 1 GB
    monkeypatch.setattr(ms, "is_loopback", lambda url: False)
    r = run("models", "scan")
    assert "Suggested main model: ollama:" in r.output and "not this one" in r.output


# --- paw models scan --lan / --range ---

def test_lan_range_from_the_own_address_has_254_hosts(monkeypatch):
    monkeypatch.setattr(ms, "own_ip", lambda: "192.168.1.37")
    net, hosts = ms.lan_range()
    assert str(net) == "192.168.1.0/24" and len(hosts) == 254
    assert "192.168.1.0" not in hosts and "192.168.1.255" not in hosts


@pytest.mark.parametrize("cidr", ["8.8.8.0/24", "10.0.0.0/16", "fd00::/64", "nonsense"])
def test_lan_range_refuses_public_big_and_odd_ranges(cidr):
    with pytest.raises(ValueError):
        ms.lan_range(cidr)


def test_lan_range_accepts_a_tailnet_range_and_a_public_own_ip_asks_for_range(monkeypatch):
    assert len(ms.lan_range("100.64.1.0/24")[1]) == 254
    monkeypatch.setattr(ms, "own_ip", lambda: "8.8.8.8")
    with pytest.raises(ValueError, match="--range 192.168.1.0/24"):
        ms.lan_range()
    monkeypatch.setattr(ms, "own_ip", lambda: None)
    with pytest.raises(ValueError, match="Could not find this computer's network address"):
        ms.lan_range()


async def test_scan_lan_asks_only_the_open_ports_and_sends_no_key(model_server, monkeypatch):
    box = model_server(models=("m",))
    opened = []

    async def fake_open(host, port):
        opened.append((host, port))
        if (host, port) != ("127.0.0.1", box.port):
            raise ConnectionRefusedError

        class W:
            def close(self): pass
            async def wait_closed(self): pass
        return None, W()

    monkeypatch.setattr(asyncio, "open_connection", fake_open)
    monkeypatch.setattr(ms, "KNOWN_PORTS", ((box.port, "Ollama", "ollama"), (1, "vLLM", "openai")))
    hits = await ms.scan_lan(["127.0.0.1", "127.0.0.2"])
    assert [(h.host, h.port) for h in hits] == [("127.0.0.1", box.port)]
    assert len(opened) == 4
    assert all(h == "" for _, h in box.requests) and {p for p, _ in box.requests} == {"/api/tags"}


async def test_scan_lan_gives_up_at_the_deadline_and_never_runs_more_than_the_limit(monkeypatch):
    live = peak = 0

    async def hang(host, port):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(60)
        finally:
            live -= 1

    monkeypatch.setattr(asyncio, "open_connection", hang)
    loop = asyncio.get_running_loop()
    start = loop.time()
    hits = await ms.scan_lan([f"10.0.0.{i}" for i in range(1, 60)], timeout=60, deadline=0.2, limit=128)
    assert hits == [] and loop.time() - start < 1.2 and 0 < peak <= 128


def test_scan_lan_cli_asks_first_and_says_what_it_sends(monkeypatch):
    monkeypatch.setattr(ms, "own_ip", lambda: "192.168.1.37")
    called = []

    async def fake_scan(hosts, **k):
        called.append(len(hosts))
        return []

    monkeypatch.setattr(ms, "scan_lan", fake_scan)
    r = run("models", "scan", "--lan", input="n\n")
    assert "254 addresses on 192.168.1.0/24" in r.output
    assert "ports 11434, 9931, 8080, 8000, 1337, 5001, 1234" in r.output
    assert "Only scan a network you own or may scan" in r.output and not called
    assert run("models", "scan", "--lan", "--yes").exit_code == 0 and called == [254]


def test_no_agent_tool_can_scan_the_network():
    from micropaw.cli import _create_registry
    from micropaw.config import Config
    assert not [n for n in _create_registry(Config()).names if "scan" in n]


# --- doctor ---

def test_doctor_rows_cover_every_server_in_use(lan_home, model_server, monkeypatch):
    monkeypatch.setenv("BOXKEY", "k1")
    ok = model_server(models=("alpha",))
    locked = model_server(kind="openai", models=("beta",), key="other")
    lan_home.write_text(json.dumps({
        "provider_config": {"ollama": {"host": ok.url}},
        "agents": {"main": {"model": "ollama:alpha"}},
        "models": {"fast": "ollama:ghost"},
        "providers": {"locked": {"base_url": f"{locked.url}/v1", "api_key_env": "BOXKEY"},
                      "gone": {"base_url": "http://127.0.0.1:1/v1"}},
        "fallback_providers": ["locked:beta", "gone:x"],
    }))
    rows = {name: (mark, detail) for name, mark, detail in asyncio.run(ms.doctor_rows(load_config()))}
    ok_row = rows[f"Server 127.0.0.1:{ok.port}"]
    assert "ghost not on that server" in ok_row[1] and ok_row[0] == "warn"
    assert "401" in rows[f"Server 127.0.0.1:{locked.port}"][1]
    assert "asleep or unreachable" in rows["Server 127.0.0.1:1"][1]


def test_doctor_row_for_a_good_server_shows_latency(lan_home, model_server):
    ok = model_server(models=("alpha",))
    lan_home.write_text(json.dumps({"provider_config": {"ollama": {"host": ok.url}},
                                    "agents": {"main": {"model": "ollama:alpha"}}}))
    rows = asyncio.run(ms.doctor_rows(load_config()))
    assert rows and rows[0][1] == "pass" and " ms" in rows[0][2] and "this computer" in rows[0][2]


def test_doctor_warns_about_a_remote_server_without_a_key(lan_home, model_server, monkeypatch):
    ok = model_server(models=("alpha",))
    lan_home.write_text(json.dumps({"provider_config": {"ollama": {"host": ok.url}},
                                    "agents": {"main": {"model": "ollama:alpha"}}}))
    monkeypatch.setattr(ms, "is_loopback", lambda url: False)
    rows = asyncio.run(ms.doctor_rows(load_config()))
    assert rows[0][1] == "warn" and "without a key" in rows[0][2]


def test_paw_doctor_prints_the_server_rows_and_quiet_skips_them(lan_home, monkeypatch):
    calls = []

    async def rows(config):
        calls.append(1)
        return [("Server box:11434", "fail", "local network, asleep or unreachable")]

    monkeypatch.setattr(ms, "doctor_rows", rows)
    r = run("doctor")
    assert "Server box:11434" in r.output and "asleep or unreachable" in r.output.replace("\n", " ")
    monkeypatch.setattr("micropaw.service.healthy", lambda: (True, ""))
    calls.clear()
    q = run("doctor", "--quiet")
    assert q.exit_code == 0 and q.output == "" and not calls


def test_a_bad_range_fails_before_any_scan_starts(lan_home, monkeypatch):
    called = []

    async def spy(targets=None):
        called.append(1)
        return []

    monkeypatch.setattr(ms, "scan_many", spy)
    r = run("models", "scan", "--range", "8.8.8.0/24")
    assert r.exit_code != 0 and "Only private IPv4 ranges" in r.output and not called


def test_doctor_calls_an_unused_ollama_for_memory_search_a_warning_not_a_failure(lan_home):
    rows = asyncio.run(ms.doctor_rows(load_config()))
    assert [r[1] for r in rows] == ["warn"] and "Not needed" in rows[0][2]
    lan_home.write_text(json.dumps({"embedding": {"enabled": "true", "provider": "ollama"}}))
    assert [r[1] for r in asyncio.run(ms.doctor_rows(load_config()))] == ["fail"]


def test_the_first_saved_server_writes_a_schema_version(lan_home, model_server):
    box = model_server()
    run("models", "add", "box", f"127.0.0.1:{box.port}")
    assert saved(lan_home)["schema_version"] == 1
