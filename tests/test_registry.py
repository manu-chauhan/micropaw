"""Registry: specs, presets, custom servers, local detection, caching, quirks."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from micropaw.config import Config
from micropaw.providers.base import AIResponse, Message, ToolCall
from micropaw.providers.openai import OpenAIProvider
from micropaw.providers.registry import PRESETS, Registry, is_local_url, parse_spec


def cfg(**kw) -> Config:
    c = Config(**kw)
    c.provider_config.claude.api_key = "sk-test"
    return c


def test_parse_spec_keeps_colons_in_model():
    assert parse_spec("Ollama:qwen3.6:27b") == ("ollama", "qwen3.6:27b")
    assert parse_spec("claude") == ("claude", "")


@pytest.mark.parametrize("url,local", [
    ("http://localhost:11434", True), ("localhost:11434", True), ("http://127.0.0.1:1234/v1", True),
    ("http://[::1]:8000", True), ("http://192.168.1.20:8000", True), ("http://10.1.2.3", True),
    ("http://172.20.0.5", True), ("http://[fd12::1]:80", True),
    ("http://8.8.8.8", False), ("http://172.32.0.1", False), ("https://api.openai.com", False),
    ("https://gpu-box.example.com", False), ("", False),
])
def test_local_is_derived_from_the_host(url, local):
    assert is_local_url(url) is local


def test_registry_shares_one_provider_per_spec():
    reg = Registry(cfg())
    a = reg.get("claude")
    b = reg.get("claude:claude-sonnet-5-5")  # a bare name means the configured default model
    assert a is b and a is not None
    assert reg.get("claude:claude-haiku-5-5") is not a
    assert reg.chain(["claude", "claude"]) is a  # duplicates collapse


def test_chain_of_several_is_a_fallback_provider():
    reg = Registry(cfg())
    chain = reg.chain(["claude", "ollama:qwen3.6"])
    assert chain.name.startswith("failover(") and len(chain.providers) == 2
    assert reg.chain(["nope:x"]) is None


def test_missing_key_is_reported_not_raised(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    reg = Registry(Config())
    assert reg.get("groq:llama") is None
    assert "GROQ_API_KEY" in reg.problems[-1]


def test_preset_urls_and_local_servers_need_no_key(monkeypatch):
    for var in ("XAI_API_KEY", "ZAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(var, "k")
    reg = Registry(Config())
    assert reg.get("xai:grok-4.3")._api_url == "https://api.x.ai/v1/chat/completions"
    assert reg.get("zai:glm-5")._api_url == "https://api.z.ai/api/paas/v4/chat/completions"
    assert reg.get("deepseek:deepseek-flash")._api_url == "https://api.deepseek.com/v1/chat/completions"
    assert reg.get("lmstudio:qwen")._api_url == "http://localhost:1234/v1/chat/completions"
    assert reg.get("jan:m")._api_url == "http://127.0.0.1:1337/v1/chat/completions"


def test_local_presets_get_the_long_read_timeout(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    reg = Registry(Config())
    assert reg.get("lmstudio:q")._inner._client.timeout.read == 300.0
    assert reg.get("groq:m")._inner._client.timeout.read == 120.0
    assert reg.get("groq:m")._inner._client.timeout.connect == 5.0


def test_custom_provider_in_config(monkeypatch):
    monkeypatch.setenv("MYKEY", "k")
    c = Config(providers={"myvllm": {"base_url": "http://192.168.1.9:8000/v1", "api_key_env": "MYKEY"}})
    reg = Registry(c)
    p = reg.get("myvllm:llama")
    assert p._api_url == "http://192.168.1.9:8000/v1/chat/completions"
    assert reg.is_local("myvllm:llama")


def test_custom_provider_over_plain_http_to_the_internet_is_refused():
    c = Config(providers={"evil": {"base_url": "http://8.8.8.8:8000", "api_key_env": "K"}})
    reg = Registry(c)
    assert reg.get("evil:m") is None
    assert any("plain http" in p for p in reg.problems)


def test_custom_provider_cannot_take_a_builtin_name():
    reg = Registry(Config(providers={"claude": {"base_url": "https://x.example"}}))
    assert any("built in" in p for p in reg.problems)


def test_https_custom_provider_is_cloud(monkeypatch):
    monkeypatch.setenv("K", "k")
    reg = Registry(Config(providers={"far": {"base_url": "https://llm.example.com/v1", "api_key_env": "K"}}))
    assert reg.get("far:m") is not None and not reg.is_local("far:m")


def test_every_shipped_preset_has_a_url_with_scheme():
    for name, p in PRESETS.items():
        assert p.base_url.startswith(("http://", "https://")), name


# --- quirks ---

def test_deepseek_reasoning_content_is_sent_back_on_tool_turns():
    p = OpenAIProvider("k", "m", base_url="https://api.deepseek.com", quirks=PRESETS["deepseek"].quirks)
    data = {"choices": [{"message": {"content": "", "reasoning_content": "thinking...", "tool_calls": [
        {"id": "1", "function": {"name": "t", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}
    resp = p._parse_response(data)
    assert resp.thinking == [{"reasoning_content": "thinking..."}]
    body = p._build_body([
        Message(role="user", content="q"),
        Message(role="assistant", content="", tool_calls=resp.tool_calls, thinking=resp.thinking),
        Message(role="tool", content="r", tool_call_id="1"),
    ], None)
    assert body["messages"][1]["reasoning_content"] == "thinking..."


def test_other_presets_do_not_send_reasoning_content():
    p = OpenAIProvider("k", "m", base_url="https://api.groq.com/openai/v1")
    msg = Message(role="assistant", content="", tool_calls=[ToolCall("1", "t", {})],
                  thinking=[{"reasoning_content": "x"}])
    assert "reasoning_content" not in p._build_body([msg], None)["messages"][0]


async def test_qwen_tools_turn_is_not_streamed():
    p = OpenAIProvider("k", "m", base_url="https://x/v1", quirks=PRESETS["dashscope"].quirks)
    seen = []

    async def fake_chat(messages, tools=None):
        seen.append("chat")
        return AIResponse(content="hi", tool_calls=[], usage={})

    p.chat = fake_chat  # type: ignore[method-assign]
    from micropaw.providers.base import ToolDef
    items = [i async for i in p.chat_stream([Message(role="user", content="x")], [ToolDef("t", "d", {})])]
    assert seen == ["chat"] and items[0] == "hi"


def test_role_providers_are_metered_around_the_shared_provider():
    from micropaw.providers.metered import MeteredProvider
    reg = Registry(cfg())
    p = reg.get("claude")
    role = reg.role("main", "chat", ["claude"])
    assert isinstance(role, MeteredProvider) and role.inner is p


def test_registry_dump_roundtrip():
    assert json.loads(json.dumps(asdict(Config())))["providers"] == {}


@pytest.mark.parametrize("url,local", [("http://100.101.102.103:11434", True), ("http://100.128.0.1:11434", False)])
def test_tailscale_range_is_local_and_the_edge_of_it_is_not(url, local):
    assert is_local_url(url) is local


def test_one_server_written_two_ways_shares_one_gate():
    c = Config(provider_config={"ollama": {"host": "192.168.1.50"}},
               providers={"box": {"base_url": "http://192.168.1.50:11434/v1"}})
    reg = Registry(c)
    a, b = reg.resolve("ollama:m"), reg.resolve("box:m")
    assert reg._sem(a) is reg._sem(b)
    assert reg._sem(a) is not reg._sem(reg.resolve("claude:x"))
