"""Secret redaction: exact values, key formats, speed, and where it is applied."""

import asyncio
import json
import logging
import time

import pytest

from micropaw.config import Config
from micropaw.tools.base import Tool, ToolContext, ToolResult, run_tool
from micropaw.utils import audit
from micropaw.utils import redact as r
from micropaw.utils.logger import RedactFilter

SECRET = "zQ9-very-secret-value-1234"
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GH = "ghp_" + "a1B2c3D4e5" * 3 + "a1B2c3"
TG = "123456789:" + "ABCdef_ghi-JKLmno0123456789abcdefgh"[:35]
ANT = "sk-ant-api03-" + "a1_-" * 23 + "a" + "AA"
JWT = "eyJhbGciOiJIUzI1NiIsInR5" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI" + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in list(__import__("os").environ):
        if r.SECRET_NAME.search(k):
            monkeypatch.delenv(k)
    r.set_known_secrets({})
    yield
    r.set_known_secrets({})


def test_exact_values_get_their_name():
    r.set_known_secrets({"claude.api_key": SECRET})
    assert r.redact(f"key is {SECRET}!") == "key is [redacted:claude.api_key]!"


def test_short_values_are_ignored():
    r.set_known_secrets({"x": "short"})
    assert r.redact("a short thing") == "a short thing"


def test_env_values_by_name(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", SECRET)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Manu Chauhan")
    monkeypatch.setenv("PLAIN_SETTING", "plain-setting-value")
    r.set_known_secrets({})
    out = r.redact(f"{SECRET} Manu Chauhan plain-setting-value")
    assert out == "[redacted:MY_SERVICE_TOKEN] Manu Chauhan plain-setting-value"


def test_add_known_secret_at_runtime():
    r.add_known_secret("custom", SECRET)
    assert "[redacted:custom]" in r.redact(SECRET)


def test_longest_value_first():
    r.set_known_secrets({"short": "abcdefgh", "long": "abcdefgh-and-more"})
    assert r.redact("abcdefgh-and-more") == "[redacted:long]"


@pytest.mark.parametrize("secret,name", [
    (AWS, "aws-access-token"), (GH, "github-pat"), (TG, "telegram-bot-token"),
    (ANT, "anthropic-api-key"), (JWT, "jwt"),
])
def test_key_formats(secret, name):
    out = r.redact(f"token={secret} next")
    assert secret not in out
    assert f"[redacted:{name}]" in out
    assert out.endswith(" next")


def test_private_key_block():
    block = "-----BEGIN RSA PRIVATE KEY-----\n" + "MIIEowIBAAKCAQEA" * 8 + "\n-----END RSA PRIVATE KEY-----"
    out = r.redact(f"before\n{block}\nafter")
    assert "MIIEow" not in out and out.startswith("before") and out.endswith("after")


def test_normal_text_is_untouched():
    text = "Meet at 10:30 on 2026-11-05. Call 555-1234, order 12345678 shipped. sk-learn is a library."
    assert r.redact(text) == text


def test_config_secrets_walks_fields_and_mcp_env():
    c = Config.model_validate({
        "provider_config": {"claude": {"api_key": SECRET}},
        "channels": {"webchat": {"auth_token": "web-token-12345"}},
        "google": {"client_secret": "google-secret-9876"},
        "mcp_servers": {"s": {"command": "x", "env": {"API_KEY": "mcp-env-value-1", "MODE": "fast"}}},
    })
    found = r.config_secrets(c)
    assert found["provider_config.claude.api_key"] == SECRET
    assert found["channels.webchat.auth_token"] == "web-token-12345"
    assert found["google.client_secret"] == "google-secret-9876"
    assert found["mcp.s.API_KEY"] == "mcp-env-value-1"
    assert "max_tokens" not in " ".join(found)


def test_one_megabyte_is_fast():
    r.set_known_secrets({f"k{i}": f"secret-value-number-{i:04d}" for i in range(10)})
    text = ("The quick brown fox jumps over the lazy dog at 10:30, order 12345678. " * 15000)[:1_000_000]
    start = time.perf_counter()
    assert r.redact(text) == text
    assert time.perf_counter() - start < 0.2


def test_redact_obj_nested():
    r.set_known_secrets({"k": SECRET})
    assert r.redact_obj({"a": [SECRET, {"b": f"x{SECRET}"}], "n": 3}) == {
        "a": ["[redacted:k]", {"b": "x[redacted:k]"}], "n": 3}


class _Echo(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, result):
        self._result = result

    async def execute(self, args, context):
        return self._result


def _ctx():
    return ToolContext(conversation_id="c", user_id="u", platform="t")


def test_run_tool_redacts_text_and_tool_result():
    r.set_known_secrets({"k": SECRET})
    assert asyncio.run(run_tool(_Echo(f"got {SECRET}"), {}, _ctx())) == "got [redacted:k]"
    res = asyncio.run(run_tool(_Echo(ToolResult(text=f"got {SECRET}")), {}, _ctx()))
    assert res.text == "got [redacted:k]"


def test_audit_redacts_args_approval_and_result_before_cutting():
    r.set_known_secrets({"k": SECRET})
    # the secret straddles the 300 char cut
    arg = "x" * (audit._MAX_VALUE - 5) + SECRET
    audit.record("shell", {"command": arg, "nested": [SECRET]}, "ok", "c", approval=f"run {SECRET}",
                 result=f"out {SECRET}")
    line = json.loads(audit.AUDIT_FILE.read_text().splitlines()[-1])
    blob = json.dumps(line)
    assert SECRET not in blob and SECRET[:6] not in blob
    assert line["approval"] == "run [redacted:k]"
    assert line["result"] == "out [redacted:k]"


def test_audit_result_preview_is_short():
    audit.record("shell", {}, "ok", "c", result="y" * 1000)
    assert len(audit.tail(1)[0]["result"]) <= audit._MAX_RESULT + 3


def test_log_filter_redacts_message_and_traceback():
    r.set_known_secrets({"k": SECRET})
    f = RedactFilter()
    rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "failed with %s", (SECRET,), None)
    f.filter(rec)
    assert rec.getMessage() == "failed with [redacted:k]"
    try:
        raise ValueError(f"bad {SECRET}")
    except ValueError:
        import sys
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "oops", (), sys.exc_info())
    f.filter(rec)
    assert SECRET not in rec.getMessage() and rec.exc_info is None


async def test_gateway_start_hides_env_secrets_the_config_names(monkeypatch, tmp_path):
    import asyncio
    from unittest.mock import MagicMock

    from micropaw import gateway as gw_mod
    from micropaw.tools import shell

    value, bearer = "s3cr3t-llm-key-value-123456", "bearer-value-abcdef-123456"
    monkeypatch.setenv("GPUBOX_PASS", value)
    monkeypatch.setenv("MCP_BEARER", bearer)
    monkeypatch.setattr(shell, "EXTRA_SECRET_ENV", set())
    monkeypatch.setattr(gw_mod, "DB_FILE", tmp_path / "d.db")
    monkeypatch.setattr(gw_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr("micropaw.scheduler.Scheduler.run", lambda self: asyncio.sleep(0))
    c = Config.model_validate({
        "providers": {"gpubox": {"base_url": "https://llm.example.com/v1", "api_key_env": "GPUBOX_PASS"}},
        "mcp_servers": {"x": {"url": "https://m.example.com/mcp",
                              "headers": {"Authorization": "Bearer ${MCP_BEARER}"}}},
        "embedding": {"enabled": "false"},
    })
    assert shell._child_env().get("GPUBOX_PASS") == value

    class Terminal:
        name = "terminal"

        def set_handler(self, h): pass
        def set_stream_handler(self, h): pass
        def set_access(self, h): pass

        async def start(self): pass
        async def stop(self): pass

    try:
        await gw_mod.Gateway(c, MagicMock(), MagicMock(), [Terminal()]).start()
        env = shell._child_env()
        assert "GPUBOX_PASS" not in env and "MCP_BEARER" not in env and "PATH" in env
        assert r.redact(f"leak {value} and {bearer}") == "leak [redacted:GPUBOX_PASS] and [redacted:MCP_BEARER]"
    finally:
        r.set_known_secrets({})
