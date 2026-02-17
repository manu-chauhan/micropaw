"""Tests for WebChat channel — origin checking, frame encoding, accept key."""

from __future__ import annotations

import base64
import hashlib

import pytest

from micropaw.channels.webchat import (
    WebChatChannel,
    _ws_accept_key,
    _ws_encode_frame,
    _ws_text,
    _WS_GUID,
)


# ---------------------------------------------------------------------------
# WebSocket utilities
# ---------------------------------------------------------------------------

class TestWsAcceptKey:
    def test_rfc6455_accept_key(self):
        """Verify the accept key computation matches RFC 6455 spec."""
        ws_key = "dGhlIHNhbXBsZSBub25jZQ=="
        expected = base64.b64encode(
            hashlib.sha1((ws_key + _WS_GUID).encode()).digest()
        ).decode()
        assert _ws_accept_key(ws_key) == expected

    def test_strips_whitespace(self):
        ws_key = "  abc123  "
        expected = base64.b64encode(
            hashlib.sha1((ws_key.strip() + _WS_GUID).encode()).digest()
        ).decode()
        assert _ws_accept_key(ws_key) == expected


class TestWsEncode:
    def test_text_frame(self):
        frame = _ws_text("hello")
        assert frame[0] == 0x81  # FIN + text opcode
        assert frame[1] == 5  # payload length
        assert frame[2:] == b"hello"

    def test_small_payload(self):
        frame = _ws_encode_frame(0x1, b"x" * 125)
        assert frame[1] == 125

    def test_medium_payload(self):
        frame = _ws_encode_frame(0x1, b"x" * 200)
        assert frame[1] == 126  # Extended 16-bit length
        import struct
        length = struct.unpack("!H", frame[2:4])[0]
        assert length == 200

    def test_large_payload(self):
        frame = _ws_encode_frame(0x1, b"x" * 70000)
        assert frame[1] == 127  # Extended 64-bit length
        import struct
        length = struct.unpack("!Q", frame[2:10])[0]
        assert length == 70000


# ---------------------------------------------------------------------------
# WebChatChannel — origin checking
# ---------------------------------------------------------------------------

class TestWebChatOrigin:
    def test_no_allowed_origins_accepts_all(self):
        ch = WebChatChannel(allowed_origins=[])
        assert ch.allowed_origins == []

    def test_allowed_origins_stored(self):
        ch = WebChatChannel(allowed_origins=["https://example.com", "https://app.local"])
        assert "https://example.com" in ch.allowed_origins
        assert "https://app.local" in ch.allowed_origins

    def test_none_defaults_to_empty(self):
        ch = WebChatChannel(allowed_origins=None)
        assert ch.allowed_origins == []


class TestWebChatTokenCheck:
    def test_valid_token(self):
        ch = WebChatChannel(auth_token="secret123")
        assert ch._check_token("/ws?token=secret123") is True

    def test_invalid_token(self):
        ch = WebChatChannel(auth_token="secret123")
        assert ch._check_token("/ws?token=wrong") is False

    def test_missing_token(self):
        ch = WebChatChannel(auth_token="secret123")
        assert ch._check_token("/ws") is False

    def test_token_with_extra_params(self):
        ch = WebChatChannel(auth_token="secret123")
        assert ch._check_token("/ws?foo=bar&token=secret123") is True

    def test_auto_generated_token(self):
        ch = WebChatChannel()
        assert len(ch.auth_token) > 0
        assert ch._check_token(f"/ws?token={ch.auth_token}") is True


class TestWsOutboundTruncation:
    def test_oversized_payload_truncated(self):
        from micropaw.channels.webchat import MAX_FRAME_SIZE
        huge = "x" * (MAX_FRAME_SIZE + 1000)
        frame = _ws_text(huge)
        # Frame should be smaller than the original payload + overhead
        assert len(frame) < MAX_FRAME_SIZE + 20
        assert frame.endswith(b"...(truncated)")


class TestWebChatChannel:
    def test_name(self):
        ch = WebChatChannel()
        assert ch.name == "webchat"

    def test_defaults(self):
        ch = WebChatChannel()
        assert ch.host == "127.0.0.1"
        assert ch.port == 8765

    def test_custom_config(self):
        ch = WebChatChannel(host="0.0.0.0", port=9000, auth_token="mytoken")
        assert ch.host == "0.0.0.0"
        assert ch.port == 9000
        assert ch.auth_token == "mytoken"
