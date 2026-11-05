"""Fallback chain rules: what falls through, cooldown, notes, streaming, context-length retry."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from micropaw.providers import failover
from micropaw.providers.base import NO_RETRY, AIProvider, AIResponse, Message, ToolCall, retry_on_transient
from micropaw.providers.failover import AllProvidersFailedError, FallbackProvider, classify, trim_messages
from micropaw.providers.registry import Registry, _Gate

REQ = httpx.Request("POST", "http://x/v1/chat/completions")
USER = [Message(role="user", content="hi")]


def status(code: int, text: str = "") -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError("err", request=REQ, response=httpx.Response(code, text=text, request=REQ))


class Fake(AIProvider):
    def __init__(
        self, name: str, error: Exception | None = None, text: str = "ok", stream_error_after: int | None = None,
    ):
        self.name = name
        self.model = "m"
        self.error = error
        self.text = text
        self.stream_error_after = stream_error_after
        self.calls = 0
        self.no_retry_seen: list[bool] = []

    async def chat(self, messages, tools=None):
        self.calls += 1
        self.no_retry_seen.append(NO_RETRY.get())
        if self.error:
            raise self.error
        return AIResponse(content=self.text, tool_calls=[], usage={"input": 1, "output": 1})

    async def chat_stream(self, messages, tools=None):
        self.calls += 1
        if self.error and self.stream_error_after is None:
            raise self.error
        for i, word in enumerate(self.text.split()):
            if self.stream_error_after is not None and i == self.stream_error_after:
                raise self.error
            yield word + " "
        yield AIResponse(content=self.text, tool_calls=[], usage={})


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(failover, "_now", lambda: t[0])
    return t


@pytest.mark.parametrize("error", [
    status(429), status(500), status(503), status(401), status(402), status(403), status(404), status(408),
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"),
])
async def test_retryable_errors_fall_through(error):
    a, b = Fake("a", error), Fake("b", text="from b")
    r = await FallbackProvider([a, b]).chat(USER)
    assert r.content.startswith("from b")
    assert b.calls == 1


@pytest.mark.parametrize("code", [400, 413, 422])
async def test_request_errors_stop_at_once(code):
    a, b = Fake("a", status(code)), Fake("b")
    with pytest.raises(httpx.HTTPStatusError):
        await FallbackProvider([a, b]).chat(USER)
    assert b.calls == 0


async def test_all_fail_raises_with_last_error_as_cause():
    err = status(500)
    with pytest.raises(AllProvidersFailedError) as ei:
        await FallbackProvider([Fake("a", status(429)), Fake("b", err)]).chat(USER)
    assert ei.value.__cause__ is err


async def test_cooldown_skips_a_failed_provider_for_60s(clock):
    a, b = Fake("a", httpx.ReadTimeout("slow")), Fake("b")
    chain = FallbackProvider([a, b])
    await chain.chat(USER)
    await chain.chat(USER)
    assert a.calls == 1  # second call skipped it
    clock[0] += 61
    a.error = None
    r = await chain.chat(USER)
    assert a.calls == 2 and not r.content.endswith("]")


async def test_connect_error_enters_cooldown_too(clock):
    a, b = Fake("a", httpx.ConnectError("refused")), Fake("b")
    chain = FallbackProvider([a, b])
    await chain.chat(USER)
    await chain.chat(USER)
    assert a.calls == 1


async def test_cooldown_is_flat(clock):
    a, b = Fake("a", status(429)), Fake("b")
    chain = FallbackProvider([a, b])
    await chain.chat(USER)
    clock[0] += 61
    await chain.chat(USER)  # fails again
    clock[0] += 61
    await chain.chat(USER)
    assert a.calls == 3  # tried again after 61 s each time, no doubling


async def test_all_cooling_still_tries_in_order(clock):
    a, b = Fake("a", status(429)), Fake("b", status(429))
    chain = FallbackProvider([a, b])
    with pytest.raises(AllProvidersFailedError):
        await chain.chat(USER)
    with pytest.raises(AllProvidersFailedError):
        await chain.chat(USER)
    assert a.calls == 2


async def test_note_names_the_fallback_and_the_reason():
    r = await FallbackProvider([Fake("a", httpx.ReadTimeout("x")), Fake("b", text="hello")]).chat(USER)
    assert r.content == "hello\n\n[answered by b:m because a:m timed out]"


async def test_no_note_when_primary_answers_or_tool_calls_pending():
    r = await FallbackProvider([Fake("a", text="hello"), Fake("b")]).chat(USER)
    assert r.content == "hello"

    class Tooling(Fake):
        async def chat(self, messages, tools=None):
            return AIResponse(content="calling", tool_calls=[ToolCall("1", "t", {})], usage={})

    r = await FallbackProvider([Fake("a", status(500)), Tooling("b")]).chat(USER)
    assert r.content == "calling"


async def test_retry_sleeps_skipped_when_a_fallback_exists():
    a, b = Fake("a", status(429)), Fake("b")
    await FallbackProvider([a, b]).chat(USER)
    assert a.no_retry_seen == [True]
    assert NO_RETRY.get() is False  # restored
    assert b.no_retry_seen == [False]  # the last one may still retry


async def test_retry_on_transient_honours_no_retry():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        raise status(429)

    token = NO_RETRY.set(True)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await retry_on_transient(flaky)
    finally:
        NO_RETRY.reset(token)
    assert calls == 1


async def collect(agen):
    return [x async for x in agen]


async def test_stream_falls_through_before_any_text():
    chain = FallbackProvider([Fake("a", httpx.ConnectError("x")), Fake("b", text="two words")])
    items = await collect(chain.chat_stream(USER))
    assert items[0] == "two " and isinstance(items[-1], AIResponse)
    assert "[answered by b:m because a:m could not be reached]" in items[-1].content


async def test_stream_error_after_text_does_not_fall_through():
    a = Fake("a", httpx.ReadTimeout("x"), text="one two three", stream_error_after=2)
    b = Fake("b")
    items = await collect(FallbackProvider([a, b]).chat_stream(USER))
    assert b.calls == 0
    final = items[-1]
    assert final.content.startswith("one two ") and "[reply cut short: a:m timed out]" in final.content


async def test_stream_400_raises():
    with pytest.raises(httpx.HTTPStatusError):
        await collect(FallbackProvider([Fake("a", status(400)), Fake("b")]).chat_stream(USER))


# --- context-length retry (in the per-provider wrapper) ---

LONG = [Message(role="system", content="s")] + [
    Message(role="user" if i % 2 == 0 else "assistant", content=f"m{i}") for i in range(8)
] + [Message(role="user", content="last")]


class Overflow(Fake):
    """Rejects a long conversation with a context-length 400, accepts a short one."""

    async def chat(self, messages, tools=None):
        self.calls += 1
        self.seen = messages
        if len(messages) > 6:
            raise status(400, '{"error": "maximum context length exceeded"}')
        return AIResponse(content="fits", tool_calls=[], usage={})


async def test_context_length_400_trims_and_retries_once():
    inner = Overflow("o")
    gate = _Gate(inner, asyncio.Semaphore(1), None)
    r = await gate.chat(LONG)
    assert r.content == "fits" and inner.calls == 2
    assert inner.seen[0].role == "system" and inner.seen[-1].content == "last"


async def test_context_retry_happens_once_only():
    class Never(Fake):
        async def chat(self, messages, tools=None):
            self.calls += 1
            raise status(400, "context length exceeded")

    inner = Never("n")
    with pytest.raises(httpx.HTTPStatusError):
        await _Gate(inner, asyncio.Semaphore(1), None).chat(LONG)
    assert inner.calls == 2


async def test_other_400_is_not_retried():
    inner = Fake("n", status(400, "bad tool schema"))
    with pytest.raises(httpx.HTTPStatusError):
        await _Gate(inner, asyncio.Semaphore(1), None).chat(LONG)
    assert inner.calls == 1


def test_trim_keeps_system_and_newest_and_starts_on_a_user_turn():
    out = trim_messages(LONG)
    assert out[0].role == "system" and out[-1].content == "last" and out[1].role == "user"
    assert len(out) < len(LONG)
    assert trim_messages(USER) is None


def test_classify():
    assert classify(status(400)) is None
    assert classify(status(429)) == "returned HTTP 429"
    assert classify(RuntimeError("x")) == "failed"


# --- local servers: friendly errors, one call at a time ---

async def test_local_connect_error_gets_a_plain_message():
    from micropaw.config import Config
    reg = Registry(Config())
    inner = Fake("lmstudio", httpx.ConnectError("refused"))
    gate = _Gate(inner, asyncio.Semaphore(1), "lmstudio")
    with pytest.raises(Exception) as ei:
        await gate.chat(USER)
    assert str(ei.value) == "LM Studio server is not running; start it in the Developer tab."
    assert reg  # registry builds without a key or network


async def test_cloud_errors_are_left_alone():
    gate = _Gate(Fake("x", httpx.ConnectError("refused")), asyncio.Semaphore(1), None)
    with pytest.raises(httpx.ConnectError):
        await gate.chat(USER)


async def test_fallback_still_sees_the_original_error_through_the_friendly_one():
    gate = _Gate(Fake("lmstudio", httpx.ConnectError("refused")), asyncio.Semaphore(1), "lmstudio")
    r = await FallbackProvider([gate, Fake("b", text="cloud")]).chat(USER)
    assert r.content.startswith("cloud")


async def test_local_gate_runs_one_call_at_a_time():
    active = peak = 0

    class Slow(Fake):
        async def chat(self, messages, tools=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return AIResponse(content="x", tool_calls=[], usage={})

    gate = _Gate(Slow("s"), asyncio.Semaphore(1), "ollama")
    await asyncio.gather(*(gate.chat(USER) for _ in range(4)))
    assert peak == 1
