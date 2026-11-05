"""The locality policy: which model, voice, channel, tool and MCP settings each mode refuses, and the plain
message that says what to change."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from micropaw import egress, privacy
from micropaw.config import Config, PrivacyConfig
from micropaw.core.profile import build_runtimes, resolve_profiles
from micropaw.providers.base import Message
from micropaw.providers.failover import AllProvidersFailedError
from micropaw.providers.ollama import OllamaProvider
from micropaw.providers.registry import Registry, is_local_url

USER = [Message(role="user", content="hi")]


def set_mode(name: str, **kw) -> None:
    privacy._reset_for_tests()
    privacy.freeze(name)
    privacy.configure(PrivacyConfig(mode=name, **kw))


def config(privacy_kw: dict | None = None, **kw) -> Config:
    kw.setdefault("provider", "ollama")
    c = Config(privacy=PrivacyConfig(**(privacy_kw or {})), **kw)
    c.provider_config.claude.api_key = "sk-test"
    return c


@pytest.fixture
def no_dns(monkeypatch):
    """Any name lookup fails the test: strict modes decide a name from the owner's list, never from DNS."""
    def boom(*a, **k):
        raise AssertionError(f"getaddrinfo was called: {a}")
    monkeypatch.setattr(socket, "getaddrinfo", boom)


# --- is_local_url by mode -------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434", True), ("http://127.0.0.1:1234/v1", True), ("http://[::1]:80", True),
    ("http://192.168.1.50:11434", True), ("http://10.0.0.7:8000/v1", True), ("http://100.101.102.103:11434", True),
    ("http://[fd7a:115c:a1e0::1]:11434", True), ("http://172.20.0.5:1234", True), ("http://169.254.3.4", True),
    ("http://100.128.0.1", False), ("http://8.8.8.8", False), ("https://api.anthropic.com", False),
    ("http://gpu-box:11434", False), ("http://gpu-box.local:11434", False), ("https://gpu.tail1234.ts.net", False),
    ("http://listed-box:11434", True),
])
@pytest.mark.parametrize("name", ["local", "airgapped"])
def test_strict_modes_decide_without_dns(no_dns, name, url, expected):
    set_mode(name, lan_hosts=["listed-box"])
    assert is_local_url(url) is expected


def test_allow_lan_off_leaves_loopback_and_the_listed_hosts():
    set_mode("airgapped", allow_lan=False, lan_hosts=["192.168.7.0/24", "box"])
    assert is_local_url("http://127.0.0.1:1") and is_local_url("http://192.168.7.9") and is_local_url("http://box")
    assert not is_local_url("http://192.168.1.1") and not is_local_url("http://100.64.0.9")


def test_normal_mode_still_looks_names_up(monkeypatch):
    from micropaw.providers import registry
    registry._dns.clear()
    monkeypatch.setattr(registry.socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(2, 1, 6, "", ("192.168.1.9", 0))])
    assert is_local_url("http://gpu-box:11434")


# --- roles ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["local", "airgapped"])
@pytest.mark.parametrize("role", ["main", "fast", "vision", "fallback", "compaction"])
def test_cloud_models_are_refused_for_every_role(no_dns, name, role):
    set_mode(name)
    reg = Registry(config())
    msg = reg.refusal("claude:x", role)
    assert msg and f"privacy.mode is {name}" in msg and role in msg and "api.anthropic.com" in msg
    assert "Use a local model or switch to normal" in msg
    assert reg.get("claude:x", role) is None and msg in reg.problems


def test_check_role_uses_the_config_it_was_given(no_dns):
    set_mode("local")
    privacy.bind_config(config())
    assert privacy.check_role("ollama:qwen3", "main") is None
    assert "openrouter.ai" in privacy.check_role("openrouter:some/model", "fast")
    assert privacy.check_role("lmstudio:m", "vision") is None
    assert privacy.check_role("", "fast") is None


def test_normal_mode_refuses_nothing():
    set_mode("normal")
    assert Registry(config()).refusal("claude:x", "main") is None


def test_a_lan_name_must_be_listed_and_the_refusal_says_where(no_dns):
    c = config(providers={"gpu": {"base_url": "http://gpu-box:8000/v1"}})
    set_mode("airgapped")
    reg = Registry(c)
    assert any('Add "gpu-box" to privacy.lan_hosts' in p for p in reg.problems)
    assert reg.get("gpu:m") is None  # never built
    set_mode("airgapped", lan_hosts=["gpu-box"])
    assert Registry(c).get("gpu:m") is not None


@pytest.mark.parametrize("name", ["local", "airgapped"])
def test_ollama_cloud_models_are_refused_even_on_this_machine(no_dns, name):
    set_mode(name)
    reg = Registry(config(providers={"box": {"base_url": "http://127.0.0.1:8000/v1"}}))
    for spec in ("ollama:gpt-oss:120b-cloud", "ollama:qwen3:cloud", "box:llama:cloud"):
        msg = reg.refusal(spec, "main")
        assert msg and "runs on ollama.com" in msg, spec
        assert reg.get(spec) is None
    assert reg.refusal("ollama:qwen3:27b", "main") is None


def test_ollama_cloud_models_are_labelled_cloud_in_normal_mode():
    set_mode("normal")
    reg = Registry(config())
    assert reg.is_local("ollama:qwen3:27b") and not reg.is_local("ollama:gpt-oss:120b-cloud")
    assert privacy.is_local_provider(reg.get("ollama:qwen3:27b"))
    assert not privacy.is_local_provider(reg.get("ollama:gpt-oss:120b-cloud"))


@pytest.mark.parametrize("show", [{"remote_host": "https://ollama.com:443"}, {"remote_model": "x"}])
def test_a_show_reply_naming_a_remote_host_is_refused_in_local_mode(show):
    def handler(request):
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion"], **show})
        return httpx.Response(200, json={"message": {"content": "hi"}})
    p = OllamaProvider("http://127.0.0.1:11434", "innocent-name")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    set_mode("normal")
    assert asyncio.run(p.chat(USER)).content == "hi"  # normal mode asks nothing
    p2 = OllamaProvider("http://127.0.0.1:11434", "innocent-name")
    p2._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    set_mode("local")
    with pytest.raises(egress.EgressBlocked, match="runs on ollama.com"):
        asyncio.run(p2.chat(USER))


# --- fallbacks and helpers ------------------------------------------------------------------------

def test_local_mode_builds_no_cloud_fallback(no_dns):
    set_mode("local")
    reg = Registry(config())
    chain = reg.role("main", "chat", ["ollama:m", "claude:c"])
    assert getattr(chain, "providers", [chain])[0].name == "ollama" and not hasattr(chain, "withheld")
    assert any("claude:c" in p and "api.anthropic.com" in p for p in reg.problems)


def test_normal_mode_holds_back_a_cloud_fallback_for_a_local_main():
    set_mode("normal")
    reg = Registry(config())
    chain = reg.role("main", "chat", ["ollama:m", "claude:c"])
    assert len(chain.providers) == 1 and chain.withheld == ["claude:c"]
    both = Registry(config(privacy_kw={"allow_cloud_fallback": True})).role("main", "chat", ["ollama:m", "claude:c"])
    assert len(both.providers) == 2 and both.withheld == []


async def test_a_down_local_model_does_not_send_the_chat_to_the_cloud():
    set_mode("normal")
    reg = Registry(config())
    chain = reg.role("main", "chat", ["ollama:m", "claude:c"])

    def refuse(request):
        raise httpx.ConnectError("refused")
    local = chain.providers[0]
    inner = local
    while hasattr(inner, "_inner") or hasattr(inner, "inner"):
        inner = getattr(inner, "_inner", None) or inner.inner
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    with pytest.raises(AllProvidersFailedError, match="Local model is down. Not sending this chat to claude"):
        await chain.chat(USER)
    # a cloud first choice is unchanged: the local fallback still works as before
    assert len(reg.role("main", "chat", ["claude:c", "ollama:m"]).providers) == 2


def test_a_local_main_does_not_keep_a_cloud_helper_unless_allowed():
    c = config(models={"fast": "claude:claude-haiku-5-5", "vision": "ollama:llava"})
    reg = Registry(c)
    p = resolve_profiles(c, reg)["main"]
    assert p.fast == "" and p.vision == "ollama:llava" and p.dropped == ["claude:claude-haiku-5-5"]
    ok = config(privacy_kw={"allow_cloud_helpers": True}, models={"fast": "claude:claude-haiku-5-5"})
    assert resolve_profiles(ok, Registry(ok))["main"].fast == "claude:claude-haiku-5-5"
    cloud_main = config(provider="claude", models={"fast": "claude:claude-haiku-5-5"})
    assert resolve_profiles(cloud_main, Registry(cloud_main))["main"].fast == "claude:claude-haiku-5-5"


def test_local_mode_main_that_is_cloud_has_no_runtime(no_dns):
    set_mode("airgapped")
    c = config(provider="claude")
    with pytest.raises(Exception, match="privacy.mode is airgapped"):
        build_runtimes(c, Registry(c))


# --- the config check lists everything at once ----------------------------------------------------

def test_check_config_lists_every_refusal_at_once(no_dns, monkeypatch):
    set_mode("airgapped")
    c = config(provider="claude", models={"fast": "openrouter:x/y"}, fallback_providers=["gemini:g"],
               embedding={"provider": "openai"}, voice={"stt": "openai:whisper-1"})
    c.provider_config.openai.api_key = "k"
    c.channels.telegram.enabled = True
    c.channels.telegram.token = "t"
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.example")
    c.provider_config.ollama.host = "https://ollama.example"
    report = privacy.check_config(c)
    text = "\n".join(report.refusals)
    for needle in ("main model claude", "fast model openrouter", "fallback model gemini",
                   "embedding.provider is openai", "voice.stt", "Telegram", "OLLAMA_HOST"):
        assert needle in text, needle
    with pytest.raises(privacy.PrivacyError) as err:
        report.raise_if_any()
    assert str(err.value).startswith("privacy.mode is airgapped") and err.value.args[0].count("\n- ") >= 7


def test_normal_mode_checks_nothing():
    set_mode("normal")
    c = config(provider="claude")
    c.channels.telegram.enabled = True
    assert privacy.check_config(c).refusals == []


@pytest.mark.parametrize("name,allow,refused", [
    ("airgapped", [], True), ("airgapped", ["telegram"], True), ("local", [], True), ("local", ["telegram"], False),
])
def test_vendor_channels_by_mode(name, allow, refused):
    set_mode(name, allow=allow)
    msg = privacy.channel_refusal("telegram")
    assert bool(msg) is refused
    if refused:
        assert "api.telegram.org" in msg and "passes through" not in msg and "Telegram servers" in msg
    assert privacy.channel_refusal("terminal") is None and privacy.channel_refusal("webchat") is None


def test_an_allowed_vendor_gets_a_notice_not_a_refusal(no_dns):
    set_mode("local", allow=["telegram"])
    c = config(privacy_kw={"allow": ["telegram"]})
    c.channels.telegram.enabled = True
    c.channels.telegram.token = "t"
    report = privacy.check_config(c)
    assert report.refusals == [] and any("Telegram" in n and "plain text" in n for n in report.notices)


def test_webchat_on_the_lan_gets_a_warning_and_airgapped_needs_a_listed_address(no_dns):
    c = config()
    c.channels.webchat.enabled = True
    c.channels.webchat.host = "192.168.1.5"
    set_mode("local")
    assert any("plain http" in n for n in privacy.check_config(c).notices)
    set_mode("airgapped", allow_lan=False)
    c.privacy.allow_lan = False
    assert any("webchat.host" in r for r in privacy.check_config(c).refusals)
    c.channels.webchat.host = "127.0.0.1"
    assert privacy.check_config(c).refusals == []


def test_enforce_fails_before_anything_starts_when_the_guard_is_not_working(monkeypatch):
    set_mode("airgapped")
    monkeypatch.setattr(egress, "install", lambda g: "the audit hook did not run")
    with pytest.raises(privacy.PrivacyError, match="network guard is not working"):
        privacy.enforce(config())


# --- gateway start --------------------------------------------------------------------------------

async def test_gateway_start_refuses_cloud_model_and_telegram_listing_both(monkeypatch, capsys):
    from unittest.mock import MagicMock

    from micropaw.channels.base import Channel
    from micropaw.gateway import Gateway
    from micropaw.tools.base import ToolRegistry

    class Telegram(Channel):
        name = "telegram"

        async def start(self): ...
        async def stop(self): ...
        async def send_message(self, chat_id, text): ...

    set_mode("airgapped")
    monkeypatch.setattr(privacy, "ensure_guard", lambda config=None: None)  # a hook cannot be added inside pytest
    c = config(provider="claude")
    gw = Gateway(c, MagicMock(), ToolRegistry(), [Telegram()])
    with pytest.raises(SystemExit) as exit_:
        await gw.start()
    assert exit_.value.code == 1
    err = capsys.readouterr().err
    assert "main model claude" in err and "Telegram" in err


# --- tools and owner commands ---------------------------------------------------------------------

def test_tools_off_by_mode():
    set_mode("normal")
    assert privacy.disabled_tools() == set()
    set_mode("airgapped", allow=["web_fetch", "google"])  # the allow list means nothing in airgapped
    assert privacy.disabled_tools() == {"http_request", "web_browse", "email", "calendar"}
    set_mode("local")
    assert privacy.disabled_tools() == {"http_request", "web_browse", "email", "calendar"}
    set_mode("local", allow=["web_fetch", "google"])
    assert privacy.disabled_tools() == {"web_browse"}


async def test_google_tools_refuse_in_strict_modes():
    from micropaw.tools import google_auth
    set_mode("airgapped")
    token, err = await google_auth.ensure_token("Gmail")
    assert token is None and "Air-gapped mode is on" in err
    assert "Air-gapped" in await google_auth.authorize()
    set_mode("local")
    assert "privacy.allow" in (await google_auth.ensure_token())[1]
    set_mode("local", allow=["google"])
    assert "privacy.allow" not in (await google_auth.ensure_token())[1]


def test_update_check_by_mode(monkeypatch):
    from micropaw import net, service
    made = []

    def refuse(*a, **k):
        made.append(a)
        raise httpx.ConnectError("x")
    monkeypatch.setattr(net, "sync_client", refuse)
    set_mode("airgapped")
    assert service.latest_version() is None and made == []
    asked = []
    monkeypatch.setattr(privacy, "permit", lambda host, *a, **k: asked.append(host))
    set_mode("local")
    assert service.latest_version() is None and asked == ["pypi.org"] and len(made) == 1


def test_owner_commands_are_refused_in_airgapped_with_the_offline_route():
    set_mode("airgapped")
    msg = privacy.owner_command_gate("paw update", ("pypi.org",))
    assert "air-gapped" in msg and "--no-index" in msg
    set_mode("normal")
    assert privacy.owner_command_gate("paw update", ("pypi.org",)) is None


async def test_http_request_by_mode(monkeypatch):
    from micropaw import security
    from micropaw.tools.base import ToolContext
    from micropaw.tools.http_request import HttpRequestTool
    tool = HttpRequestTool()
    ctx = ToolContext("c", "u", "terminal")
    set_mode("airgapped")
    assert "Air-gapped" in await tool.execute({"url": "https://example.com"}, ctx)
    set_mode("local")
    assert "web fetch is off" in await tool.execute({"url": "https://example.com"}, ctx)
    set_mode("local", allow=["web_fetch"])
    assert tool.needs_approval({"url": "https://example.com"}, ctx) == "GET https://example.com"  # always asks
    assert "GET only" in await tool.execute({"url": "https://example.com", "method": "POST"}, ctx)
    order = []
    monkeypatch.setattr(privacy, "permit", lambda host, *a, **k: order.append(("permit", host)))

    async def validate(url):
        order.append(("validate", url))
        return "stop here"
    monkeypatch.setattr("micropaw.tools.http_request.validate_url", validate)
    await tool.execute({"url": "https://example.com/p"}, ctx)
    assert order[0] == ("permit", "example.com") and order[1][0] == "validate"  # the approved host opens first
    assert security  # imported for the monkeypatch target


async def test_validate_url_refuses_an_unapproved_name_before_any_lookup(no_dns):
    from micropaw.security import validate_url
    set_mode("local")
    msg = await validate_url("https://evil.example/x")
    assert msg and msg.startswith("Blocked by privacy mode") and "evil.example" in msg
    set_mode("airgapped")
    assert (await validate_url("https://evil.example")).startswith("Blocked by privacy mode")


def test_a_permit_lets_one_host_through_validate_url_to_its_lookup(monkeypatch):
    set_mode("local")
    seen = []

    def resolve(host, *a, **k):
        seen.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert privacy.permit("search.example") is None
    g = privacy.guard()
    assert g.name_ok("search.example") and not g.name_ok("other.example")
    assert g.address_ok("93.184.216.34") and not g.address_ok("93.184.216.35")
    assert seen == ["search.example"]
    set_mode("airgapped")
    assert privacy.permit("search.example") == "Air-gapped mode allows no outside hosts."


def test_a_proxy_permit_never_resolves_the_target(no_dns):
    set_mode("local")
    assert privacy.permit("target.example", ips=("127.0.0.1",)) is None
    assert privacy.guard().name_ok("target.example")


# --- what a cloud model is told about the owner ---------------------------------------------------------

def _prompt(provider, **privacy_kw) -> str:
    from unittest.mock import AsyncMock, MagicMock

    from micropaw.core.agent import Agent
    from micropaw.tools.base import ToolRegistry
    c = Config(privacy=PrivacyConfig(**privacy_kw))
    c.persona.location = "Lisbon, Portugal"
    store = MagicMock()
    store.get_recent_memories = AsyncMock(return_value=[])
    return Agent(c, provider, ToolRegistry(), store)._build_system_prompt("terminal")


def test_location_and_user_text_stay_home_when_sharing_is_off(tmp_path, monkeypatch):
    import micropaw.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    (tmp_path / "USER.md").write_text("Likes tea. Lives alone.")
    cloud = Registry(config(provider="claude")).get("claude:x")
    local = Registry(config()).get("ollama:m")
    assert "Lisbon" in _prompt(cloud) and "Likes tea" in _prompt(cloud)  # today's behaviour is the default
    off = _prompt(cloud, share_with_cloud=False)
    assert "Lisbon" not in off and "Likes tea" not in off and "MicroPaw" in off
    on_device = _prompt(local, share_with_cloud=False)
    assert "Lisbon" in on_device and "Likes tea" in on_device  # a local model gets it either way
    # a chain that can reach a cloud model counts as cloud; without the fallback it is local only
    privacy_kw = {"share_with_cloud": False, "allow_cloud_fallback": True}
    both = Registry(config(privacy_kw=privacy_kw)).role("main", "chat", ["ollama:m", "claude:c"])
    assert "Lisbon" not in _prompt(both, **privacy_kw)
    held = Registry(config()).role("main", "chat", ["ollama:m", "claude:c"])
    assert "Lisbon" in _prompt(held, share_with_cloud=False)


# --- the smaller rules ------------------------------------------------------------------------------------------------

async def test_a_blocked_connection_is_not_a_reason_to_try_the_next_provider():
    from micropaw.providers.base import AIProvider, AIResponse
    from micropaw.providers.failover import FallbackProvider, classify

    class Blocked(AIProvider):
        name = "a"

        async def chat(self, messages, tools=None):
            raise httpx.ConnectError("x") from egress.EgressBlocked("air-gapped mode blocked a connection to x")

    class Second(AIProvider):
        name = "b"
        called = False

        async def chat(self, messages, tools=None):
            Second.called = True
            return AIResponse(content="b", tool_calls=[], usage={})

    err = httpx.ConnectError("x")
    err.__cause__ = egress.EgressBlocked("air-gapped mode blocked a connection to x")
    assert classify(err) is None and classify(httpx.ConnectError("plain")) == "could not be reached"
    with pytest.raises(httpx.ConnectError):
        await FallbackProvider([Blocked(), Second()]).chat(USER)
    assert Second.called is False


async def test_a_blocked_call_to_a_local_model_gets_a_plain_message():
    import asyncio as aio

    from micropaw.providers.registry import _Gate

    class Inner:
        name, model, host = "ollama", "m", "http://127.0.0.1:11434"

        async def chat(self, messages, tools=None):
            raise httpx.ConnectError("x") from egress.EgressBlocked("local mode blocked a connection to evil.example")

    gate = _Gate(Inner(), aio.Semaphore(1), "ollama")
    with pytest.raises(Exception, match="Blocked by privacy mode: local mode blocked a connection to evil.example"):
        await gate.chat(USER)


def test_lan_hosts_accepts_names_ranges_and_name_equals_address():
    set_mode("airgapped", lan_hosts=["Gpu-Box", "192.168.7.0/24", "nas=10.9.8.7", "  "])
    s = privacy.settings()
    assert s.names == {"gpu-box", "nas"} and any(str(n) == "10.9.8.7/32" for n in s.nets)
    assert privacy.local_host("GPU-BOX") and privacy.local_host("nas") and privacy.local_host("192.168.7.99")
    assert not privacy.local_host("other") and not privacy.local_host("")


def test_allowed_vendors_resolve_and_their_addresses_are_learned(monkeypatch):
    set_mode("local", allow=["telegram", "google"])
    g = privacy.build_guard()
    assert g.name_ok("api.telegram.org") and g.name_ok("gmail.googleapis.com") and g.name_ok("accounts.google.com")
    assert not g.name_ok("telegram.evil.example") and not g.name_ok("discord.com")
    assert not g.address_ok("149.154.167.220")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("149.154.167.220", 443))])
    privacy._learn_vendor_addresses(g)
    socket.getaddrinfo("api.telegram.org", 443)
    assert g.address_ok("149.154.167.220") and not g.address_ok("149.154.167.221")
    set_mode("airgapped", allow=["telegram"])
    assert not privacy.build_guard().name_ok("api.telegram.org")  # an airgapped install has no vendor exceptions


def test_unknown_allow_entries_are_reported():
    set_mode("local", allow=["telegramm"])
    c = config(privacy_kw={"allow": ["telegramm"]})
    assert any('"telegramm"' in n for n in privacy.check_config(c).notices)


def test_describe_and_child_isolation_for_status_screens(monkeypatch):
    set_mode("normal")
    assert privacy.describe() == "Privacy: normal"
    set_mode("local")
    assert privacy.describe() == "Privacy: local (LAN allowed)"
    set_mode("airgapped", allow_lan=False)
    assert privacy.describe() == "Privacy: airgapped (this machine only)"
    from micropaw.tools import sandbox
    monkeypatch.setattr(sandbox, "isolation_ok", lambda: True)
    assert privacy.child_isolation() == "bwrap"
    monkeypatch.setattr(sandbox, "isolation_ok", lambda: False)
    assert privacy.child_isolation() == "none"


async def test_scan_goes_around_environment_proxies(monkeypatch):
    from micropaw import net
    from micropaw.providers import registry
    seen = []
    real = net.client
    monkeypatch.setattr(registry.net, "client", lambda *a, **k: seen.append(k.get("local")) or real(*a, **k))
    assert await registry.scan_local({"x": ("ollama", "http://127.0.0.1:1")}) == []
    assert seen == [True]


@pytest.mark.parametrize("entry", ["0.0.0.0/0", "::/0", "8.8.8.8", "8.8.8.0/24", "192.168.0.0/15", "100.128.0.0/10",
                                   "::ffff:0.0.0.0/96", "name=1.1.1.1", "2001:db8::/32"])
def test_lan_hosts_cannot_list_public_or_too_wide_ranges(entry):
    set_mode("airgapped", lan_hosts=[entry, "192.168.7.0/24"])
    s = privacy.settings()
    assert s.public == (entry,) and [str(n) for n in s.nets] == ["192.168.7.0/24"]
    g = privacy.build_guard()
    assert not g.address_ok("8.8.8.8") and not g.address_ok("2606:4700::1") and g.address_ok("192.168.7.5")
    c = config(privacy_kw={"mode": "airgapped", "lan_hosts": [entry]})
    assert any(f'"{entry}"' in r and "private" in r for r in privacy.check_config(c).refusals)


def test_private_lan_hosts_entries_are_not_refused():
    set_mode("airgapped", lan_hosts=["10.0.0.0/8", "192.168.1.0/24", "100.64.0.0/10", "fd00::/8", "127.0.0.0/8",
                                     "fe80::/10", "box", "::ffff:192.168.1.5"])
    assert privacy.settings().public == ()
    assert privacy.check_config(config(privacy_kw={"mode": "airgapped", "lan_hosts": ["10.0.0.0/8"]})).refusals == []
