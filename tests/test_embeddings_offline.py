"""Embeddings that start late, work offline, and can live on another machine."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from unittest.mock import MagicMock

import httpx
import pytest

import micropaw.memory.embeddings as emb
from micropaw import privacy
from micropaw.config import Config
from micropaw.core.profile import build_runtimes
from micropaw.gateway import Gateway
from micropaw.memory.embeddings import (
    _DEFAULT_LOCAL_MODEL,
    _NEW_INSTALL_LOCAL_MODEL,
    LocalEmbedding,
    NullEmbedding,
    OllamaEmbedding,
    OpenAIEmbedding,
    choose_local_model,
)
from micropaw.providers.registry import Registry

GIB = 1024 ** 3


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    # net.client decides "local" from the host, which looks a plain name up; keep that off the network
    import micropaw.providers.registry as reg
    monkeypatch.setattr(reg, "_lookup", lambda host: True)


@pytest.fixture(autouse=True)
def _normal_mode(monkeypatch, tmp_path):
    monkeypatch.setattr("micropaw.config.CONFIG_DIR", tmp_path)
    privacy._reset_for_tests()
    privacy.freeze("normal")
    yield
    privacy._reset_for_tests()


def _gateway(**kw) -> Gateway:
    c = Config(provider="ollama", **kw)
    return Gateway(c, MagicMock(), MagicMock(), [], runtimes=build_runtimes(c, Registry(c)))


def _probe(monkeypatch, answer: bool) -> None:
    async def probe(host, model):
        return answer

    monkeypatch.setattr(emb, "probe_ollama", probe)


def _fastembed_present(monkeypatch, ram: int | None = 8 * GIB) -> None:
    import importlib.util as util
    real = util.find_spec
    monkeypatch.setattr("micropaw.gateway.importlib.util.find_spec",
                        lambda name, *a: object() if name == "fastembed" else real(name, *a))
    monkeypatch.setattr("micropaw.gateway.ram_bytes", lambda: ram)


# --- automatic choice ---

async def test_auto_never_picks_openai(monkeypatch):
    _probe(monkeypatch, False)
    monkeypatch.setattr("micropaw.gateway.importlib.util.find_spec", lambda name, *a: None)
    gw = _gateway()
    gw.config.provider_config.openai.api_key = "sk-fake"
    assert isinstance(await gw._create_embedding_provider(), NullEmbedding)


async def test_auto_picks_ollama_when_it_answers(monkeypatch):
    _probe(monkeypatch, True)
    got = await _gateway()._create_embedding_provider()
    assert isinstance(got, OllamaEmbedding) and got.keep_alive == "1m"


async def test_small_machine_gets_no_local_model_automatically(monkeypatch):
    _probe(monkeypatch, False)
    _fastembed_present(monkeypatch, ram=2 * GIB)
    assert isinstance(await _gateway()._create_embedding_provider(), NullEmbedding)
    _fastembed_present(monkeypatch, ram=8 * GIB)
    assert isinstance(await _gateway()._create_embedding_provider(), LocalEmbedding)


async def test_choosing_local_does_not_import_fastembed_or_load_a_model(monkeypatch):
    _probe(monkeypatch, False)
    _fastembed_present(monkeypatch)
    monkeypatch.delitem(sys.modules, "fastembed", raising=False)
    got = await _gateway()._create_embedding_provider()
    assert isinstance(got, LocalEmbedding) and got._model is None and "fastembed" not in sys.modules


async def test_explicit_local_without_fastembed_turns_embeddings_off(monkeypatch, caplog):
    monkeypatch.setattr("micropaw.gateway.importlib.util.find_spec", lambda name, *a: None)
    with caplog.at_level("WARNING"):
        got = await _gateway(embedding={"provider": "local"})._create_embedding_provider()
    assert isinstance(got, NullEmbedding) and "fastembed not installed" in caplog.text


async def test_remote_server_that_does_not_answer_is_named_in_the_log(monkeypatch, caplog):
    _probe(monkeypatch, False)
    _fastembed_present(monkeypatch)
    gw = _gateway()
    gw.config.provider_config.ollama.host = "http://gpu-box:11434"
    with caplog.at_level("WARNING"):
        got = await gw._create_embedding_provider()
    assert isinstance(got, LocalEmbedding)
    assert "http://gpu-box:11434 did not answer at start" in caplog.text and "embedding.provider" in caplog.text


async def test_the_start_probe_gives_up_on_a_dead_host_within_a_second(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, timeout, **kw):  # net.client also passes its hooks and TLS settings
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.ConnectTimeout("no route")

    monkeypatch.setattr(emb.httpx, "AsyncClient", Client)
    assert await emb.probe_ollama("http://192.0.2.1:11434") is False
    assert seen["timeout"].connect <= 1.0


# --- which local model ---

def _db(path, sql: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(sql)
    con.commit()
    con.close()


def test_new_install_gets_the_small_model(tmp_path):
    assert choose_local_model(tmp_path / "none.db") == _NEW_INSTALL_LOCAL_MODEL


def test_database_with_vectors_keeps_the_old_model(tmp_path):
    _db(tmp_path / "d.db", "CREATE TABLE memories (embedding TEXT); INSERT INTO memories VALUES ('[0.1]');")
    assert choose_local_model(tmp_path / "d.db") == _DEFAULT_LOCAL_MODEL


def test_recorded_model_and_explicit_choice_win(tmp_path):
    _db(tmp_path / "d.db", "CREATE TABLE kv_meta (key TEXT, value TEXT); "
                           "INSERT INTO kv_meta VALUES ('embedding_model', 'local:BAAI/bge-base-en-v1.5');")
    assert choose_local_model(tmp_path / "d.db") == "BAAI/bge-base-en-v1.5"
    assert choose_local_model(tmp_path / "d.db", "mine") == "mine"


def test_dimensions_follow_the_model():
    assert LocalEmbedding(_NEW_INSTALL_LOCAL_MODEL).dimensions == 384
    assert LocalEmbedding(_DEFAULT_LOCAL_MODEL).dimensions == 768


async def test_the_model_name_is_remembered_in_the_database(tmp_path):
    from micropaw.memory.store import MemoryStore
    s = MemoryStore(tmp_path / "data.db", LocalEmbedding(_NEW_INSTALL_LOCAL_MODEL))
    await s.init()
    assert await s._get_meta("embedding_model") == f"local:{_NEW_INSTALL_LOCAL_MODEL}"
    await s.close()


# --- loading the model ---

class _FakeTextEmbedding:
    calls: list[dict] = []
    size = 384

    def __init__(self, model_name, **kwargs):
        type(self).calls.append({"model_name": model_name, **kwargs})
        if kwargs.get("local_files_only") and not type(self).has_files:
            raise ValueError("Could not load model from any source.")

    has_files = False

    def embed(self, texts):
        import array
        for _ in texts:
            yield type("V", (), {"tolist": lambda self_: list(array.array("f", [0.5] * _FakeTextEmbedding.size))})()


@pytest.fixture
def fake_fastembed(monkeypatch):
    mod = type(sys)("fastembed")
    _FakeTextEmbedding.calls = []
    _FakeTextEmbedding.has_files = False
    _FakeTextEmbedding.size = 384
    mod.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    return _FakeTextEmbedding


async def test_model_loads_on_first_embed_from_the_models_folder(fake_fastembed, tmp_path):
    e = LocalEmbedding(_NEW_INSTALL_LOCAL_MODEL)
    assert not fake_fastembed.calls
    fake_fastembed.has_files = True
    (emb.models_dir()).mkdir(parents=True, exist_ok=True)
    (emb.models_dir() / "model").write_text("x")
    assert len(await e.embed("hello")) == 384
    call = fake_fastembed.calls[0]
    assert call["cache_dir"] == str(tmp_path / "models" / "fastembed") and call["threads"] == 2
    assert call["local_files_only"] is True


async def test_local_mode_never_downloads(fake_fastembed, caplog):
    privacy._reset_for_tests()
    privacy.freeze("local")
    e = LocalEmbedding(_NEW_INSTALL_LOCAL_MODEL)
    with caplog.at_level("WARNING"):
        assert await e.embed("hello") == []
    assert all(c.get("local_files_only") for c in fake_fastembed.calls)
    assert "paw models fetch embeddings" in caplog.text and str(emb.models_dir()) in caplog.text
    import os
    assert os.environ["HF_HUB_OFFLINE"] == "1"


async def test_normal_mode_downloads_when_nothing_is_there(fake_fastembed):
    e = LocalEmbedding(_NEW_INSTALL_LOCAL_MODEL)
    assert len(await e.embed("hello")) == 384
    assert [c.get("local_files_only") for c in fake_fastembed.calls] == [False]


async def test_a_failed_load_turns_embeddings_off_once(fake_fastembed, caplog):
    privacy._reset_for_tests()
    privacy.freeze("airgapped")
    e = LocalEmbedding(_NEW_INSTALL_LOCAL_MODEL)
    with caplog.at_level("WARNING"):
        await e.embed("one")
        await e.embed("two")
    assert caplog.text.count("Embeddings are off") == 1 and len(fake_fastembed.calls) == 1


async def test_a_model_of_another_size_is_refused_not_mixed_in(fake_fastembed):
    fake_fastembed.size = 99
    assert await LocalEmbedding(_NEW_INSTALL_LOCAL_MODEL).embed("x") == []


async def test_missing_fastembed_names_the_machine(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    monkeypatch.setattr("platform.machine", lambda: "armv7l")
    with caplog.at_level("WARNING"):
        await LocalEmbedding().embed("x")
    assert "armv7l" in caplog.text and "Ollama embeddings" in caplog.text


# --- servers on another machine ---

def _remote(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(emb.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))


async def test_openai_style_server_gets_the_key_and_the_custom_address(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    _remote(monkeypatch, handler)
    e = OpenAIEmbedding("k", "m", "http://gpu-box:1234/v1")
    assert await e.embed("x") == [0.1, 0.2]
    assert seen == {"url": "http://gpu-box:1234/v1/embeddings", "auth": "Bearer k"}
    seen.clear()
    assert await OpenAIEmbedding("", "m", "http://gpu-box:1234").embed("x")
    assert seen["auth"] is None


async def test_ollama_embedding_sends_keep_alive_to_its_own_host(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[1.0]]})

    _remote(monkeypatch, handler)
    assert await OllamaEmbedding("lan-pc:11434", "nomic-embed-text", "1m").embed("x") == [1.0]
    assert seen["url"] == "http://lan-pc:11434/api/embed" and seen["body"]["keep_alive"] == "1m"


async def test_failed_server_is_left_alone_for_a_minute(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectTimeout("no route", request=request)

    _remote(monkeypatch, handler)
    e = OllamaEmbedding("http://192.0.2.9:11434")
    start = time.monotonic()
    assert await e.embed("a") == [] and await e.embed("b") == []
    assert len(calls) == 1 and time.monotonic() - start < 5
    e._down_until = 0.0
    await e.embed("c")
    assert len(calls) == 2


async def test_clients_use_a_five_second_connect_timeout():
    assert OllamaEmbedding()._client.timeout.connect == 5.0
    assert OpenAIEmbedding("k")._client.timeout.connect == 5.0


async def test_gateway_builds_the_remote_providers_from_config(monkeypatch):
    monkeypatch.setenv("LAN_KEY", "secret")
    gw = _gateway(embedding={"provider": "openai", "base_url": "http://192.168.1.5:8080", "api_key_env": "LAN_KEY",
                             "model": "bge"})
    got = await gw._create_embedding_provider()
    assert isinstance(got, OpenAIEmbedding) and got.api_key == "secret" and got.base_url == "http://192.168.1.5:8080"
    gw = _gateway(embedding={"provider": "ollama", "base_url": "http://192.168.1.5:11434"})
    got = await gw._create_embedding_provider()
    assert got.host == "http://192.168.1.5:11434"


async def test_private_agent_refuses_a_public_embedding_address(monkeypatch):
    monkeypatch.setattr("micropaw.gateway.is_local_url", lambda url: "203.0.113.5" not in url)
    gw = _gateway(embedding={"provider": "openai", "base_url": "http://203.0.113.5:8080"})
    with pytest.raises(RuntimeError, match="not on this machine"):
        await gw._create_embedding_provider()


async def test_plain_http_to_a_public_host_is_refused_even_in_normal_mode(monkeypatch):
    gw = Gateway(Config(embedding={"provider": "openai", "base_url": "http://203.0.113.5:8080"}),
                 MagicMock(), MagicMock(), [])
    gw._runtimes = {}
    monkeypatch.setattr("micropaw.gateway.is_local_url", lambda url: False)
    with pytest.raises(RuntimeError, match="plain http"):
        await gw._create_embedding_provider()
    gw.config.embedding.base_url = "https://embeddings.example.com"
    assert isinstance(await gw._create_embedding_provider(), OpenAIEmbedding)


async def test_local_mode_turns_the_default_openai_service_off(caplog):
    privacy._reset_for_tests()
    privacy.freeze("local")
    gw = Gateway(Config(embedding={"provider": "openai"}), MagicMock(), MagicMock(), [])
    gw._runtimes = {}
    gw.config.provider_config.openai.api_key = "sk-x"
    with caplog.at_level("WARNING"):
        assert isinstance(await gw._create_embedding_provider(), NullEmbedding)
    assert "local mode" in caplog.text


async def test_openai_embedding_posts_to_the_configured_openai_base_url(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"embedding": [0.3]}]})

    _remote(monkeypatch, handler)
    gw = Gateway(Config(embedding={"provider": "openai"}), MagicMock(), MagicMock(), [])
    gw._runtimes = {}
    gw.config.provider_config.openai.api_key = "sk-x"
    gw.config.provider_config.openai.base_url = "http://localhost:4000"
    got = await gw._create_embedding_provider()
    await got.embed("x")
    assert seen["url"] == "http://localhost:4000/v1/embeddings"


def test_gateway_start_does_not_warm_the_embedding_model_up():
    import inspect
    assert "warmup" not in inspect.getsource(Gateway.start)
