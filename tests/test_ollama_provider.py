"""Tests for Ollama request building."""

from __future__ import annotations

from micropaw.providers.base import AIResponse, MediaItem, Message, ToolCall
from micropaw.providers.ollama import OllamaProvider


def test_assistant_tool_calls_and_results_kept():
    p = OllamaProvider()
    body = p._build_body([
        Message(role="user", content="hi"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="a", name="web", arguments={"q": "x"})]),
        Message(role="tool", content="result", tool_call_id="a"),
    ])
    msgs = body["messages"]
    assert msgs[1]["tool_calls"] == [{"function": {"name": "web", "arguments": {"q": "x"}}}]
    assert msgs[2] == {"role": "tool", "content": "result", "tool_name": "web"}


def test_tool_images_follow_tool_results_as_user_message():
    p = OllamaProvider()
    img = MediaItem(type="image", data=b"abc", mime_type="image/png")
    body = p._build_body([
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="a", name="shot", arguments={}), ToolCall(id="b", name="web", arguments={}),
        ]),
        Message(role="tool", content="r1", tool_call_id="a", media=[img]),
        Message(role="tool", content="r2", tool_call_id="b"),
    ])
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["assistant", "tool", "tool", "user"]
    assert body["messages"][3]["images"] == ["YWJj"]


def test_keep_alive_goes_as_a_number_when_it_is_one():
    msgs = [Message(role="user", content="x")]
    for given, sent in (("-1", -1), ("0", 0), ("30m", "30m"), ("1h", "1h"), ("300", 300)):
        assert OllamaProvider(keep_alive=given)._build_body(msgs)["keep_alive"] == sent


async def test_stream_error_line_raises_so_a_fallback_can_answer():
    import httpx
    import pytest

    from micropaw.providers.failover import FallbackProvider
    from micropaw.providers.ollama import OllamaProvider as Ollama

    def handler(request):
        return httpx.Response(200, content=b'{"message": {"content": "partial"}}\n{"error": "model runner crashed"}\n')

    def handler_early(request):
        return httpx.Response(200, content=b'{"error": "model runner crashed"}\n')

    p = Ollama()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="model runner crashed"):
        [x async for x in p.chat_stream([Message(role="user", content="hi")])]
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler_early))

    class Good(Ollama):
        async def chat_stream(self, messages, tools=None):
            yield "from the backup"
            yield AIResponse(content="from the backup", tool_calls=[], usage={})

    items = [x async for x in FallbackProvider([p, Good()]).chat_stream([Message(role="user", content="hi")])]
    assert any("backup" in x for x in items if isinstance(x, str))
