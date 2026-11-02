"""Tests for Gemini request building and parsing."""

from __future__ import annotations

from micropaw.providers.base import MediaItem, Message, ToolCall
from micropaw.providers.gemini import GeminiProvider

IMG = MediaItem(type="image", data=b"abc", mime_type="image/png")


def _p() -> GeminiProvider:
    return GeminiProvider(api_key="k")


def test_user_image_sent_inline():
    body = _p()._build_body([Message(role="user", content="look", media=[IMG])], None)
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"inline_data": {"mime_type": "image/png", "data": "YWJj"}}
    assert parts[1] == {"text": "look"}


def test_parallel_responses_share_one_turn():
    body = _p()._build_body([
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="a", name="f", arguments={}), ToolCall(id="b", name="g", arguments={}),
        ]),
        Message(role="tool", content="1", tool_call_id="a", media=[IMG]),
        Message(role="tool", content="2", tool_call_id="b"),
    ], None)
    assert len(body["contents"]) == 2
    parts = body["contents"][1]["parts"]
    assert [next(iter(x)) for x in parts] == ["functionResponse", "functionResponse", "inline_data"]
    assert parts[1]["functionResponse"]["name"] == "g"


def test_multiple_system_messages_concatenated():
    body = _p()._build_body([
        Message(role="system", content="base prompt"),
        Message(role="user", content="hi"),
        Message(role="system", content="summary"),
    ], None)
    assert body["systemInstruction"]["parts"][0]["text"] == "base prompt\n\nsummary"


def test_thought_signature_round_trip():
    p = _p()
    calls = p._parse_candidates({"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "f", "args": {}}, "thoughtSignature": "sig"},
    ]}}]})[1]
    assert calls[0].thought_signature == "sig"
    body = p._build_body([Message(role="assistant", content="", tool_calls=calls)], None)
    assert body["contents"][0]["parts"][0]["thoughtSignature"] == "sig"
    # absent signature stays absent
    plain = [ToolCall(id="x", name="f", arguments={})]
    body = p._build_body([Message(role="assistant", content="", tool_calls=plain)], None)
    assert "thoughtSignature" not in body["contents"][0]["parts"][0]
