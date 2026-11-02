"""Tests for OpenAI-compatible request building."""

from __future__ import annotations

from micropaw.providers.base import MediaItem, Message, ToolCall
from micropaw.providers.openai import OpenAIProvider

USER = [Message(role="user", content="hi")]


def test_openai_uses_max_completion_tokens():
    body = OpenAIProvider(api_key="k", max_tokens=99)._build_body(USER, None)
    assert body["max_completion_tokens"] == 99
    assert "max_tokens" not in body


def test_local_servers_keep_max_tokens():
    for url in ("http://localhost:11434", "http://localhost:8080/", "https://openrouter.ai/api",
                "https://api.openai.com.evil.test"):
        body = OpenAIProvider(api_key="k", max_tokens=99, base_url=url)._build_body(USER, None)
        assert body["max_tokens"] == 99
        assert "max_completion_tokens" not in body


def test_tool_images_become_user_message_after_tool_messages():
    img = MediaItem(type="image", data=b"abc", mime_type="image/png")
    body = OpenAIProvider(api_key="k")._build_body([
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="a", name="shot", arguments={}), ToolCall(id="b", name="web", arguments={}),
        ]),
        Message(role="tool", content="r1", tool_call_id="a", media=[img]),
        Message(role="tool", content="r2", tool_call_id="b"),
    ], None)
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["assistant", "tool", "tool", "user"]
    content = body["messages"][3]["content"]
    assert content[0]["image_url"]["url"] == "data:image/png;base64,YWJj"
