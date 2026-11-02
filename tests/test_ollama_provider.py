"""Tests for Ollama request building."""

from __future__ import annotations

from micropaw.providers.base import MediaItem, Message, ToolCall
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
