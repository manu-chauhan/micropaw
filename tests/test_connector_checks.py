"""The token checks and the shared 'save anyway' step of the channel connector."""

from __future__ import annotations

import httpx

from micropaw import connector


def _send_with(handler):
    def send(method, url, timeout, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            return c.request(method, url, **kwargs)
    return send


def test_token_checks_report_network_errors_as_text(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(connector, "_send", _send_with(boom))
    assert connector.validate_telegram_token("t") == (False, "connection failed (network issue)")
    assert connector.validate_slack_token("t") == (False, "connection failed (network issue)")
    assert connector._discord_me("t") == (False, "connection failed (network issue)", "")


def test_telegram_and_slack_checks_read_the_reply(monkeypatch):
    def reply(request):
        if "telegram" in request.url.host:
            return httpx.Response(200, json={"ok": True, "result": {"username": "paw_bot"}})
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
    monkeypatch.setattr(connector, "_send", _send_with(reply))
    assert connector.validate_telegram_token("t") == (True, "@paw_bot")
    assert connector.validate_slack_token("t") == (False, "invalid_auth")


def test_discord_check_returns_the_application_id_from_one_request(monkeypatch):
    seen = []

    def reply(request):
        seen.append(request.url.path)
        if request.headers["authorization"] == "Bot good":
            return httpx.Response(200, json={"username": "paw", "id": "42"})
        return httpx.Response(401)
    monkeypatch.setattr(connector, "_send", _send_with(reply))
    assert connector._discord_me("good") == (True, "paw (id: 42)", "42")
    assert connector._discord_me("bad") == (False, "invalid token (401 Unauthorized)", "")
    assert len(seen) == 2


def test_accept_token_asks_only_after_a_failed_check(monkeypatch):
    import questionary

    class Answer:
        def __init__(self, value):
            self.value = value

        def ask(self):
            return self.value

    asked = []
    monkeypatch.setattr(questionary, "confirm", lambda *a, **k: asked.append(a) or Answer(False))
    assert connector._accept_token(True, "x", "Connected") is True
    assert asked == []
    assert connector._accept_token(False, "bad", "Connected") is False
    assert asked == [("Save anyway?",)]
