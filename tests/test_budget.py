"""Budgets: prices, ledger, the metered provider, /usage and paw usage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import aiosqlite
import pytest
from click.testing import CliRunner

from micropaw.config import BudgetConfig, Config
from micropaw.memory.ledger import Ledger
from micropaw.providers.base import AIProvider, AIResponse, Message, ToolDef
from micropaw.providers.metered import BudgetExceeded, meter
from micropaw.utils.prices import cost, is_local


class FakeProvider(AIProvider):
    def __init__(self, name="claude", model="claude-sonnet-5-5", usage=None, delay=0.0, max_tokens=4000):
        self.name = name
        self.model = model
        self.max_tokens = max_tokens
        self.usage = usage if usage is not None else {"input": 1000, "output": 500}
        self.delay = delay
        self.calls = 0

    async def chat(self, messages, tools=None):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return AIResponse(content="hello there", tool_calls=[], usage=self.usage)

    async def chat_stream(self, messages, tools: list[ToolDef] | None = None) -> AsyncGenerator[str | AIResponse, None]:
        self.calls += 1
        for _ in range(10):
            yield "abcdefgh"
        yield AIResponse(content="x" * 80, tool_calls=[], usage=self.usage)


@pytest.fixture
async def ledger():
    db = await aiosqlite.connect(":memory:")
    led = Ledger(db)
    await led.init()
    yield led
    await db.close()


MSG = [Message(role="user", content="hi " * 100)]  # 75 tokens by chars/4


class TestCost:
    def test_sonnet_with_cache(self):
        # 100k in of which 60k cache reads and 10k cache writes, 2k out
        usage = {"input": 100_000, "cache_read": 60_000, "cache_write": 10_000, "output": 2_000}
        expected = (30_000 * 2 + 60_000 * 0.20 + 10_000 * 2.5 + 2_000 * 10) / 1e6
        assert cost("claude", "claude-sonnet-5-5", usage) == pytest.approx(expected)

    def test_haiku_tiers(self):
        low = cost("claude", "claude-haiku-5-5", {"input": 100_000, "output": 1000})
        assert low == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / 1e6)
        high = cost("claude", "claude-haiku-5-5", {"input": 100_001, "output": 1000})
        assert high == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)
        cached = cost("claude", "claude-haiku-5-5", {"input": 1000, "cache_read": 1000, "output": 0})
        assert cached == pytest.approx(1000 * 0.01 / 1e6)

    def test_other_models_use_research_prices(self):
        usage = {"input": 200_000, "cache_read": 100_000, "output": 100_000}
        assert cost("openai", "gpt-5.4-mini", usage) == pytest.approx(0.1 * 0.75 + 0.1 * 0.075 + 0.1 * 4.50)

    def test_above_unknown_tier_is_unknown(self):
        assert cost("openai", "gpt-5.4", {"input": 300_000, "output": 10}) is None

    @pytest.mark.parametrize("where", [
        "ollama", "lmstudio", "llamacpp", "vllm", "jan", "koboldcpp", "localai",
        "http://localhost:11434", "127.0.0.1:8080", "http://192.168.1.20:1234", "http://10.0.0.5",
        "http://172.16.3.4:8000", "http://[::1]:8080",
    ])
    def test_local_is_free(self, where):
        assert is_local(where)
        assert cost(where, "whatever", {"input": 10**6, "output": 10**6}) == 0.0

    @pytest.mark.parametrize("where", ["claude", "openai", "https://api.openai.com", "http://8.8.8.8:80", "172.32.0.1"])
    def test_not_local(self, where):
        assert not is_local(where)

    def test_unknown_is_none(self):
        assert cost("claude", "claude-made-up-9", {"input": 5, "output": 5}) is None
        assert cost("groq", "llama-3.3-70b-versatile", {"input": 5, "output": 5}) is None

    def test_claude_usage_reports_cache_write(self):
        from micropaw.providers.claude import ClaudeProvider
        u = ClaudeProvider._usage({"input_tokens": 10, "cache_read_input_tokens": 20,
                                   "cache_creation_input_tokens": 30, "output_tokens": 5})
        assert u == {"input": 60, "output": 5, "cache_read": 20, "cache_write": 30}


class TestLedger:
    async def test_totals_by_agent_and_breakdown(self, ledger):
        for agent, usd in (("main", 0.5), ("main", 0.25), ("work", 1.0)):
            await ledger.record(agent=agent, chat="c", role="main", provider="claude", model="m",
                                input=100, cached=0, output=10, usd=usd)
        await ledger.record(agent="work", chat="c", role="main", provider="x", model="y",
                            input=100, cached=0, output=10, usd=None)
        today = await ledger.today()
        assert today["main"].usd == pytest.approx(0.75) and today["main"].calls == 2
        assert today["work"].usd == pytest.approx(1.0) and today["work"].unpriced_tokens == 110
        rows = await ledger.breakdown(1)
        assert {(a, m) for _, a, m, _ in rows} == {("main", "m"), ("work", "m"), ("work", "y")}

    async def test_yesterday_is_not_today(self, ledger):
        await ledger.db.execute(
            "INSERT INTO usage (ts, agent, provider, model, usd) VALUES (?, 'main', 'p', 'm', 9.0)", (1.0,))
        assert await ledger.today() == {}


class TestMeter:
    async def test_records_a_call_with_cached_tokens(self, ledger):
        p = meter(FakeProvider(usage={"input": 1000, "cache_read": 400, "output": 100}),
                  agent="main", role="main", ledger=ledger, budget=BudgetConfig())
        assert (await p.chat(MSG)).content == "hello there"
        async with ledger.db.execute("SELECT agent, role, provider, model, input, cached, output, usd FROM usage") as c:
            row = await c.fetchone()
        assert row[:7] == ("main", "main", "claude", "claude-sonnet-5-5", 1000, 400, 100)
        assert row[7] == pytest.approx((600 * 2 + 400 * 0.2 + 100 * 10) / 1e6)

    async def test_passthrough_attributes(self, ledger):
        inner = FakeProvider()
        inner.host = "http://localhost:11434"
        p = meter(inner, agent="main", role="main", ledger=ledger, budget=BudgetConfig())
        assert p.name == "claude" and p.model == "claude-sonnet-5-5" and p.host == inner.host

    async def test_cap_refuses_before_the_call(self, ledger):
        await ledger.record(agent="main", chat="", role="main", provider="claude", model="m",
                            input=1, cached=0, output=1, usd=2.0)
        inner = FakeProvider()
        p = meter(inner, agent="main", role="main", ledger=ledger, budget=BudgetConfig(daily_usd=2.0))
        with pytest.raises(BudgetExceeded) as e:
            await p.chat(MSG)
        assert str(e.value) == ("Daily budget of $2.00 reached; it resets at midnight. "
                                "Change budget.daily_usd to raise it.")
        assert inner.calls == 0
        with pytest.raises(BudgetExceeded):
            async for _ in p.chat_stream(MSG):
                pass
        assert inner.calls == 0

    async def test_no_cap_never_refuses(self, ledger):
        await ledger.record(agent="main", chat="", role="main", provider="claude", model="m",
                            input=1, cached=0, output=1, usd=500.0)
        p = meter(FakeProvider(), agent="main", role="main", ledger=ledger, budget=BudgetConfig())
        assert (await p.chat(MSG)).content

    async def test_per_agent_cap(self, ledger):
        await ledger.record(agent="kid", chat="", role="main", provider="claude", model="m",
                            input=1, cached=0, output=1, usd=1.0)
        budget = BudgetConfig(daily_usd=100)
        caps = {"kid": 1.0}
        kid = meter(FakeProvider(), agent="kid", role="main", ledger=ledger, budget=budget, agent_caps=caps)
        main = meter(FakeProvider(), agent="main", role="main", ledger=ledger, budget=budget, agent_caps=caps)
        with pytest.raises(BudgetExceeded, match="agent kid"):
            await kid.chat(MSG)
        assert (await main.chat(MSG)).content

    async def test_local_model_not_blocked_over_the_cap(self, ledger):
        await ledger.record(agent="main", chat="", role="main", provider="claude", model="m",
                            input=1, cached=0, output=1, usd=5.0)
        p = meter(FakeProvider(name="ollama", model="llama3"), agent="main", role="main", ledger=ledger,
                  budget=BudgetConfig(daily_usd=1.0))
        assert (await p.chat(MSG)).content
        async with ledger.db.execute("SELECT usd FROM usage WHERE provider = 'ollama'") as c:
            assert (await c.fetchone())[0] == 0.0

    async def test_reservation_blocks_concurrent_overspend(self, ledger):
        # One call is estimated at 75 in + 1000 out tokens = about $0.01. A cap of $0.015 fits one, not two.
        inner = FakeProvider(delay=0.05)
        p = meter(inner, agent="main", role="main", ledger=ledger, budget=BudgetConfig(daily_usd=0.015))
        results = await asyncio.gather(p.chat(MSG), p.chat(MSG), return_exceptions=True)
        assert sum(isinstance(r, BudgetExceeded) for r in results) == 1
        assert inner.calls == 1
        assert ledger.held() == (0.0, 0)  # released

    async def test_aborted_stream_settles_in_finally(self, ledger):
        inner = FakeProvider()
        p = meter(inner, agent="main", role="main", ledger=ledger, budget=BudgetConfig(daily_usd=50))
        stream = p.chat_stream(MSG)
        got = 0
        async for item in stream:
            got += len(item)
            if got >= 24:  # three chunks, then the reader walks away
                break
        await stream.aclose()
        async with ledger.db.execute("SELECT input, output, usd FROM usage") as c:
            inp, out, usd = await c.fetchone()
        assert inp == 75 and out == 24 // 4 and usd is not None
        assert ledger.held() == (0.0, 0)

    async def test_failed_call_records_nothing_and_releases(self, ledger):
        class Boom(FakeProvider):
            async def chat(self, messages, tools=None):
                raise RuntimeError("down")
        p = meter(Boom(), agent="main", role="main", ledger=ledger, budget=BudgetConfig(daily_usd=5))
        with pytest.raises(RuntimeError):
            await p.chat(MSG)
        assert await ledger.today() == {}
        assert ledger.held() == (0.0, 0)

    async def test_unpriced_cloud_model_uses_token_cap(self, ledger):
        budget = BudgetConfig(daily_usd=5, daily_tokens=1500)
        inner = FakeProvider(name="groq", model="some-new-model", usage={"input": 1000, "output": 400})
        p = meter(inner, agent="main", role="main", ledger=ledger, budget=budget)
        # estimate: 75 in + 1000 out = 1075 fits
        assert (await p.chat(MSG)).content
        # 1400 tokens used + a new 1075 estimate is over 1500
        with pytest.raises(BudgetExceeded, match="1,500 tokens"):
            await p.chat(MSG)
        assert inner.calls == 1
        # without a USD cap the token cap is not used
        free = meter(inner, agent="main", role="main", ledger=ledger, budget=BudgetConfig(daily_tokens=1))
        assert (await free.chat(MSG)).content

    async def test_warn_note_once_per_day(self, ledger):
        budget = BudgetConfig(daily_usd=1.0, warn_at=0.5)
        # Each call costs (1000 * 2 + 500 * 10) / 1e6 = $0.007; make it about $0.3 with big usage
        inner = FakeProvider(usage={"input": 100_000, "output": 10_000})  # $0.30
        p = meter(inner, agent="main", role="main", ledger=ledger, budget=budget)
        await p.chat(MSG)
        assert ledger.take_warning() is None  # $0.30 of $1.00
        await p.chat(MSG)  # $0.60, crosses 50%
        note = ledger.take_warning()
        assert note and "$0.60 of the $1.00" in note and "\n" not in note
        await p.chat(MSG)  # $0.90, already warned today
        assert ledger.take_warning() is None


class TestUsageCommand:
    async def test_gateway_usage_text(self, ledger, tmp_path):
        from unittest.mock import MagicMock

        from micropaw.gateway import Gateway
        gw = Gateway(Config(budget=BudgetConfig(daily_usd=2.0)), MagicMock(), MagicMock(), [])
        gw.store = MagicMock(db=ledger.db)
        assert await gw._owner_command("/usage") == "No model calls recorded today."
        await ledger.record(agent="main", chat="", role="main", provider="claude", model="claude-sonnet-5-5",
                            input=1000, cached=0, output=100, usd=0.5)
        text = await gw._owner_command("/usage")
        assert text.startswith("Today: $0.50 of $2.00.")
        assert "- main, claude-sonnet-5-5: $0.50, 1,100 tokens, 1 calls" in text

    def test_paw_usage_empty(self, tmp_path, monkeypatch):
        import micropaw.cli as cli
        monkeypatch.setattr(cli, "DB_FILE", tmp_path / "data.db")
        result = CliRunner().invoke(cli.main, ["usage", "--days", "3"])
        assert result.exit_code == 0 and "No model calls" in result.output

    def test_config_budget_defaults_and_old_configs(self):
        c = Config()
        assert (c.budget.daily_usd, c.budget.daily_tokens, c.budget.warn_at) == (0, 2_000_000, 0.8)
        assert Config.model_validate({"provider": "ollama"}).budget.daily_usd == 0
