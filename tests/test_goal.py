"""Goal loops: argument parsing, stop conditions, the sticky mark, the check through run_tool, budgets."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest
from click.testing import CliRunner

from micropaw.core import goal as goal_mod
from micropaw.core.goal import GoalManager, parse_args
from micropaw.memory.ledger import Ledger, usage_tag
from micropaw.memory.store import MemoryStore
from micropaw.providers.base import AIProvider, AIResponse, Message, ToolCall
from micropaw.providers.metered import meter
from micropaw.tools import sandbox
from micropaw.tools.base import Tool, ToolContext, ToolRegistry

CHECK = "pytest -q"


class FakeShell(Tool):
    """Stands in for the shell tool: asks like it, answers from a script of results."""

    name = "shell"
    marks = True
    description = "shell"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, results: list[str]) -> None:
        self.results = list(results)
        self.ran: list[str] = []
        self.contexts: list[ToolContext] = []

    def needs_approval(self, args, context):
        self.contexts.append(context)
        return f"run {args['command']}"

    async def execute(self, args, context):
        self.ran.append(args["command"])
        return self.results.pop(0) if self.results else "Exit code 1:\nfailed"


class FakeAgent:
    """handle_message runs `script(n, agent, conv)` once per round and saves what it returns."""

    def __init__(self, store, workspace, script=None, shell=None, provider="ollama", profile=None, name="main"):
        self.name = name
        self.store = store
        self.profile = profile
        self.provider = SimpleNamespace(name=provider)
        self.config = SimpleNamespace(permissions=SimpleNamespace(workspace=str(workspace), allow_shell=True))
        self.registry = ToolRegistry()
        if shell:
            self.registry.register(shell)
        self.script = script or (lambda n, agent, conv: "working")
        self.prompts: list[str] = []
        self.convs: list[str] = []
        self.asked: list[tuple[str, str]] = []
        self.answer = True
        self.workspace = workspace

    async def handle_message(self, text, user_id, platform, channel_id=None, conv_id=None):
        self.prompts.append(text)
        self.convs.append(conv_id)
        result = self.script(len(self.convs), self, conv_id)
        if asyncio.iscoroutine(result):
            result = await result
        return result or "working"

    def _build_tool_context(self, conv_id, user_id, platform, channel_id):
        async def approve(tool, text):
            self.asked.append((tool, text))
            return self.answer

        return ToolContext(conversation_id=conv_id, user_id=user_id, platform=platform, allow_shell=True,
                           workspace=str(self.workspace), confirm_dangerous=False, approve=approve)


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(tmp_path / "data.db")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
def ws(tmp_path):
    path = tmp_path / "ws"
    path.mkdir()
    return path


@pytest.fixture
def no_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox, "resolve", lambda: sandbox.Sandbox("none"))


class Chat:
    """Collects what the goal says to the chat."""

    def __init__(self):
        self.said: list[str] = []

    async def say(self, platform, chat, text):
        self.said.append(text)


def manager(agent, chat, ledger=None, clock=None, **kw):
    agents = {agent.name: agent, **kw.get("more", {})}
    return GoalManager(lambda: agents, chat.say, lambda: ledger, **({"clock": clock} if clock else {}))


async def run_goal(mgr, agent, line):
    reply = await mgr.command(line, agent, "terminal", "u", None)
    goal = mgr.running.get("terminal:u")
    if goal is not None:
        await goal.task
    return reply


def touch(agent, n):
    (agent.workspace / f"f{n}.txt").write_text(str(n))  # a change in the workspace


# --- parsing ---

def test_parse_flags_and_task():
    spec = parse_args('fix the "failing" tests --check "pytest -q" --max 5 --budget $2.5 --agent Coder')
    assert (spec.task, spec.check, spec.max_rounds, spec.budget, spec.agent) == (
        'fix the "failing" tests', "pytest -q", 5, 2.5, "coder")


def test_parse_single_quotes_and_defaults():
    spec = parse_args("""don't give up --check 'python -c "print(1)"'""")
    assert spec.task == "don't give up" and spec.check == 'python -c "print(1)"'
    assert spec.max_rounds == 20 and spec.budget is None and spec.agent == ""


@pytest.mark.parametrize("text", ["", "   ", "--max 3", "x --max abc", "x --max 0", "x --max 101", "x --budget 0",
                                   "x --budget cheap", 'x --check ""'])
def test_parse_errors_are_plain_messages(text):
    assert isinstance(parse_args(text), str)


# --- starting ---

async def test_one_goal_at_a_time_per_chat(store, ws, no_sandbox):
    agent = FakeAgent(store, ws, script=lambda n, a, c: asyncio.sleep(0.05))
    mgr = manager(agent, Chat())
    first = await mgr.command("one", agent, "terminal", "u", None)
    assert "started" in first
    assert "already running" in await mgr.command("two", agent, "terminal", "u", None)
    other = await mgr.command("three", agent, "terminal", "other", None)  # a different chat is free
    assert "started" in other
    await mgr.close()


async def test_unknown_agent_and_missing_shell(store, ws):
    agent = FakeAgent(store, ws)
    mgr = manager(agent, Chat())
    assert "No agent called 'zed'" in await mgr.command("x --agent zed", agent, "terminal", "u", None)
    assert "shell is off" in await mgr.command('x --check "true"', agent, "terminal", "u", None)
    assert not mgr.running


async def test_cloud_models_need_a_budget(store, ws, no_sandbox):
    cloud = FakeAgent(store, ws, provider="claude")
    mgr = manager(cloud, Chat())
    assert "--budget" in await mgr.command("task", cloud, "terminal", "u", None)
    assert not mgr.running
    assert "started" in await mgr.command("task --budget 1", cloud, "terminal", "u", None)
    await mgr.close()


async def test_budget_rule_uses_the_private_flag_when_there_is_a_profile(store, ws, no_sandbox):
    mixed = FakeAgent(store, ws, provider="ollama", profile=SimpleNamespace(private=False, prefix=""))
    private = FakeAgent(store, ws, provider="claude", profile=SimpleNamespace(private=True, prefix=""), name="p")
    assert goal_mod.needs_budget(mixed) is True
    assert goal_mod.needs_budget(private) is False
    assert goal_mod.needs_budget(FakeAgent(store, ws, provider="ollama")) is False
    assert goal_mod.needs_budget(FakeAgent(store, ws, provider="openai")) is True


async def test_agent_flag_picks_another_agent(store, ws, no_sandbox):
    main = FakeAgent(store, ws)
    coder = FakeAgent(store, ws, name="coder")
    mgr = manager(main, Chat(), more={"coder": coder})
    await run_goal(mgr, main, "do it --agent coder --max 1")
    assert coder.prompts and not main.prompts


# --- rounds ---

async def test_each_round_starts_fresh_with_goal_progress_and_check_output(store, ws, no_sandbox):
    shell = FakeShell(["Exit code 1:\n1 failed", "all good"])

    def script(n, agent, conv):
        if n == 1:
            progress = next((ws / ".goals").glob("*/progress.md"), None)
            assert progress is None
            gid = conv.split(":")[1]
            (ws / ".goals" / gid / "progress.md").write_text("tried X, next try Y")
        touch(agent, n)

    agent = FakeAgent(store, ws, script, shell)
    chat = Chat()
    mgr = manager(agent, chat)
    await run_goal(mgr, agent, f'make tests pass --check "{CHECK}"')
    assert len(agent.convs) == 2 and len(set(agent.convs)) == 2
    assert agent.convs[0].startswith("goal:") and agent.convs[0].endswith(":1")
    assert "Goal: make tests pass" in agent.prompts[1] and "round 2 of 20" in agent.prompts[1]
    assert "tried X, next try Y" in agent.prompts[1] and "1 failed" in agent.prompts[1]
    assert "<outside_content" in agent.prompts[1]
    assert "1 failed" not in agent.prompts[0]
    assert "Done: the check passed" in chat.said[0] and "2 round" in chat.said[0] and "Cost:" in chat.said[0]
    assert not mgr.running and "Done" in mgr.status("terminal:u")


async def test_private_agent_rounds_keep_the_agent_prefix(store, ws, no_sandbox):
    agent = FakeAgent(store, ws, profile=SimpleNamespace(private=True, prefix="agent:p:"), name="p")
    mgr = manager(agent, Chat())
    await run_goal(mgr, agent, "x --max 1")
    assert agent.convs[0].startswith("agent:p:goal:")


async def test_stops_at_max_rounds(store, ws, no_sandbox):
    agent = FakeAgent(store, ws, lambda n, a, c: touch(a, n), FakeShell([]))
    chat = Chat()
    await run_goal(manager(agent, chat), agent, f'x --check "{CHECK}" --max 3')
    assert len(agent.convs) == 3 and "reached 3 rounds" in chat.said[0]


async def test_same_tool_call_three_times_in_a_row_stops(store, ws, no_sandbox):
    call = ToolCall(id="1", name="web_search", arguments={"query": "same"})

    async def script(n, agent, conv):
        await store.get_or_create_conversation(conv, "terminal", "u")
        await store.save_messages(conv, [Message(role="assistant", content="", tool_calls=[call])])
        touch(agent, n)

    agent = FakeAgent(store, ws, script)
    chat = Chat()
    await run_goal(manager(agent, chat), agent, "x --max 10")
    assert len(agent.convs) == 3 and "same tool call" in chat.said[0]


async def test_different_calls_do_not_trip_the_loop_rule(store, ws, no_sandbox):
    async def script(n, agent, conv):
        await store.get_or_create_conversation(conv, "terminal", "u")
        call = ToolCall(id="1", name="web_search", arguments={"query": f"q{n % 2}"})
        await store.save_messages(conv, [Message(role="assistant", content="", tool_calls=[call])])
        touch(agent, n)

    agent = FakeAgent(store, ws, script)
    await run_goal(manager(agent, Chat()), agent, "x --max 5")
    assert len(agent.convs) == 5


async def test_three_rounds_with_no_change_stop(store, ws, no_sandbox):
    agent = FakeAgent(store, ws)
    chat = Chat()
    await run_goal(manager(agent, chat), agent, "x --max 10")
    assert len(agent.convs) == 3 and "changed no files" in chat.said[0]


async def test_a_change_resets_the_idle_count(store, ws, no_sandbox):
    def script(n, agent, conv):
        if n in (3, 5):
            touch(agent, n)

    agent = FakeAgent(store, ws, script)
    await run_goal(manager(agent, Chat()), agent, "x --max 10")
    assert len(agent.convs) == 8  # idle 1-2, change 3, idle 4, change 5, then idle 6-8 ends it


async def test_progress_file_change_counts_as_change(store, ws, no_sandbox):
    def script(n, agent, conv):
        gid = conv.split(":")[1]
        (ws / ".goals" / gid / "progress.md").write_text(f"round {n}")

    agent = FakeAgent(store, ws, script)
    await run_goal(manager(agent, Chat()), agent, "x --max 6")
    assert len(agent.convs) == 6


async def test_without_a_check_the_done_line_ends_it_unverified(store, ws, no_sandbox):
    agent = FakeAgent(store, ws, lambda n, a, c: "All finished.\nGOAL COMPLETE" if n == 2 else touch(a, n))
    chat = Chat()
    await run_goal(manager(agent, chat), agent, "x")
    assert len(agent.convs) == 2 and "Nothing verified it" in chat.said[0]


async def test_the_done_line_is_ignored_when_there_is_a_check(store, ws, no_sandbox):
    agent = FakeAgent(store, ws, lambda n, a, c: "GOAL COMPLETE", FakeShell(["Exit code 1:\nno", "ok"]))
    chat = Chat()
    await run_goal(manager(agent, chat), agent, f'x --check "{CHECK}"')
    assert len(agent.convs) == 2 and "check passed" in chat.said[0]


async def test_wall_clock_limit(store, ws, no_sandbox):
    now = [0.0]

    def script(n, agent, conv):
        touch(agent, n)
        now[0] += 3 * 3600  # the round "took" three hours

    agent = FakeAgent(store, ws, script)
    chat = Chat()
    await run_goal(manager(agent, chat, clock=lambda: now[0]), agent, "x")
    assert len(agent.convs) == 1 and "2 hour limit" in chat.said[0]


async def test_stop_command_cancels_and_reports(store, ws, no_sandbox):
    started = asyncio.Event()

    async def script(n, agent, conv):
        started.set()
        await asyncio.sleep(30)

    agent = FakeAgent(store, ws, script)
    chat = Chat()
    mgr = manager(agent, chat)
    await mgr.command("long task", agent, "terminal", "u", None)
    await started.wait()
    assert "round 1 of 20" in mgr.status("terminal:u")
    goal = mgr.running["terminal:u"]
    assert "Stopping" in await mgr.command("stop", agent, "terminal", "u", None)
    await goal.task
    assert "Stopped on request" in chat.said[0] and not mgr.running
    assert "No goal is running" in await mgr.command("stop", agent, "terminal", "u", None)


async def test_an_error_in_a_round_is_reported(store, ws, no_sandbox):
    def script(n, agent, conv):
        raise RuntimeError("boom")

    agent = FakeAgent(store, ws, script)
    chat = Chat()
    await run_goal(manager(agent, chat), agent, "x")
    assert "Stopped by an error: boom" in chat.said[0]


# --- the sticky mark ---

async def test_mark_is_sticky_for_every_later_round(store, ws, no_sandbox):
    seen: dict[int, bool] = {}

    async def script(n, agent, conv):
        seen[n] = await store.is_marked(conv)  # what the round starts with
        if n == 2:
            await store.set_marked(conv)  # round 2 reads a web page
        touch(agent, n)

    agent = FakeAgent(store, ws, script)
    mgr = manager(agent, Chat())
    await run_goal(mgr, agent, "x --max 5")
    assert seen == {1: False, 2: False, 3: True, 4: True, 5: True}


async def test_status_says_when_the_goal_is_marked(store, ws, no_sandbox):
    gate = asyncio.Event()

    async def script(n, agent, conv):
        if n == 1:
            await store.set_marked(conv)
            touch(agent, n)
        else:
            await gate.wait()

    agent = FakeAgent(store, ws, script)
    mgr = manager(agent, Chat())
    await mgr.command("x", agent, "terminal", "u", None)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if mgr.running["terminal:u"].round == 2:
            break
    assert "asks first" in mgr.status("terminal:u")
    await mgr.close()


async def test_running_the_check_marks_the_goal(store, ws, no_sandbox):
    shell = FakeShell(["Exit code 1:\nfail", "Exit code 1:\nfail"])
    seen: dict[int, bool] = {}

    async def script(n, agent, conv):
        seen[n] = await store.is_marked(conv)
        touch(agent, n)

    agent = FakeAgent(store, ws, script, shell)
    await run_goal(manager(agent, Chat()), agent, f'x --check "{CHECK}" --max 2')
    assert seen == {1: False, 2: True}  # command output is outside content


# --- the check goes through run_tool ---

async def test_check_asks_the_owner_each_round_without_a_sandbox(store, ws, no_sandbox):
    shell = FakeShell(["Exit code 1:\nno", "ok"])
    agent = FakeAgent(store, ws, lambda n, a, c: touch(a, n), shell)
    await run_goal(manager(agent, Chat()), agent, f'x --check "{CHECK}"')
    assert agent.asked == [("shell", f"run {CHECK}")] * 2
    assert shell.ran == [CHECK, CHECK]
    assert all(c.confirm_dangerous for c in shell.contexts)  # a bare host always asks


async def test_declined_check_stops_the_goal(store, ws, no_sandbox):
    shell = FakeShell(["ok"])
    agent = FakeAgent(store, ws, lambda n, a, c: touch(a, n), shell)
    agent.answer = False
    chat = Chat()
    await run_goal(manager(agent, chat), agent, f'x --check "{CHECK}"')
    assert shell.ran == [] and "check could not run" in chat.said[0] and len(agent.convs) == 1


@pytest.mark.parametrize("text", [
    "Error: This command has been blocked for safety reasons.", "Sandbox failed: bwrap: nope. Not run.",
    "Error: Command timed out after 30 seconds.",
])
async def test_a_check_that_did_not_run_stops_instead_of_looping(store, ws, no_sandbox, text):
    agent = FakeAgent(store, ws, lambda n, a, c: touch(a, n), FakeShell([text]))
    chat = Chat()
    await run_goal(manager(agent, chat), agent, f'x --check "{CHECK}"')
    assert "check could not run" in chat.said[0] and len(agent.convs) == 1


async def test_only_exit_code_zero_passes(store, ws, no_sandbox):
    # output that says "passed" but exit code is not 0 never counts
    shell = FakeShell(["Exit code 2:\n5 passed, all good"] * 3)
    agent = FakeAgent(store, ws, lambda n, a, c: touch(a, n), shell)
    chat = Chat()
    await run_goal(manager(agent, chat), agent, f'x --check "{CHECK}" --max 3')
    assert "reached 3 rounds" in chat.said[0] and "passed" not in chat.said[0].split("Cost")[0].lower()


async def test_no_network_sandbox_preapproves_only_the_exact_check(store, ws, monkeypatch):
    monkeypatch.setattr(sandbox, "resolve", lambda: sandbox.Sandbox("bwrap", network=False))
    shell = FakeShell(["ok"])
    agent = FakeAgent(store, ws, lambda n, a, c: touch(a, n), shell)
    mgr = manager(agent, Chat())
    assert "without asking" in await mgr.command(f'x --check "{CHECK}"', agent, "terminal", "u", None)
    await mgr.running["terminal:u"].task
    assert agent.asked == [] and shell.ran == [CHECK]


async def test_preapproval_matches_the_exact_text_never_a_prefix(store, ws, monkeypatch):
    monkeypatch.setattr(sandbox, "resolve", lambda: sandbox.Sandbox("bwrap", network=False))

    class Drifting(FakeShell):
        def needs_approval(self, args, context):  # the second ask extends the first one
            super().needs_approval(args, context)
            return f"run {args['command']}" + (" && more" if len(self.contexts) > 1 else "")

    shell = Drifting(["ok"])
    agent = FakeAgent(store, ws, lambda n, a, c: touch(a, n), shell)
    mgr = manager(agent, Chat())
    await mgr.command(f'x --check "{CHECK}"', agent, "terminal", "u", None)
    await mgr.running["terminal:u"].task
    assert agent.asked == [("shell", f"run {CHECK} && more")]  # not waved through: it went to the owner


@pytest.mark.parametrize("box", [sandbox.Sandbox("bwrap", network=True), sandbox.Sandbox("seatbelt"),
                                 sandbox.Sandbox("none"), sandbox.Sandbox("none", error="broken")])
async def test_preapproval_is_not_offered_without_a_no_network_sandbox(store, ws, monkeypatch, box):
    monkeypatch.setattr(sandbox, "resolve", lambda: box)
    shell = FakeShell(["ok"])
    agent = FakeAgent(store, ws, lambda n, a, c: touch(a, n), shell)
    mgr = manager(agent, Chat())
    assert "asks you each round" in await mgr.command(f'x --check "{CHECK}"', agent, "terminal", "u", None)
    await mgr.running["terminal:u"].task
    assert agent.asked == [("shell", f"run {CHECK}")]


async def test_docker_no_network_counts_as_a_sandbox(store, ws, monkeypatch):
    monkeypatch.setattr(sandbox, "resolve", lambda: sandbox.Sandbox("docker", network=False, image="x"))
    agent = FakeAgent(store, ws, lambda n, a, c: touch(a, n), FakeShell(["ok"]))
    mgr = manager(agent, Chat())
    assert "without asking" in await mgr.command(f'x --check "{CHECK}"', agent, "terminal", "u", None)
    await mgr.close()


# --- budgets ---

@pytest.fixture
async def ledger():
    db = await aiosqlite.connect(":memory:")
    led = Ledger(db)
    await led.init()
    yield led
    await db.close()


async def spend(ledger, usd, model="claude-sonnet-5-5"):
    tag = usage_tag.get() or ("", "main")
    await ledger.record(agent="main", chat=tag[0], role=tag[1], provider="claude", model=model,
                        input=100, cached=0, output=10, usd=usd)


async def test_budget_stops_and_reports_the_cost(store, ws, no_sandbox, ledger):
    async def script(n, agent, conv):
        await spend(ledger, 0.6)
        touch(agent, n)

    agent = FakeAgent(store, ws, script, provider="claude")
    chat = Chat()
    await run_goal(manager(agent, chat, ledger), agent, "x --budget 1.0 --max 10")
    assert len(agent.convs) == 2
    assert "$1.00 budget is used" in chat.said[0] and "Cost: $1.20" in chat.said[0]


async def test_goal_spend_is_summed_per_goal_id(store, ws, no_sandbox, ledger):
    await ledger.record(agent="main", chat="goal:other", role="goal", provider="claude", model="m",
                        input=1, cached=0, output=1, usd=5.0)
    await ledger.record(agent="main", chat="", role="main", provider="claude", model="m",
                        input=1, cached=0, output=1, usd=7.0)

    async def script(n, agent, conv):
        await spend(ledger, 0.1)
        touch(agent, n)

    agent = FakeAgent(store, ws, script, provider="claude")
    chat = Chat()
    await run_goal(manager(agent, chat, ledger), agent, "x --budget 1.0 --max 2")
    assert "Cost: $0.20" in chat.said[0]  # not the 5.00 of another goal, not the 7.00 of ordinary chat


async def test_unpriced_model_cannot_keep_a_budget(store, ws, no_sandbox, ledger):
    async def script(n, agent, conv):
        await spend(ledger, None, model="mystery-1")
        touch(agent, n)

    agent = FakeAgent(store, ws, script, provider="claude")
    chat = Chat()
    await run_goal(manager(agent, chat, ledger), agent, "x --budget 1.0 --max 5")
    assert len(agent.convs) == 1 and "no known price" in chat.said[0]


async def test_local_goal_needs_no_budget_and_costs_nothing(store, ws, no_sandbox, ledger):
    agent = FakeAgent(store, ws, lambda n, a, c: touch(a, n))
    chat = Chat()
    await run_goal(manager(agent, chat, ledger), agent, "x --max 2")
    assert "Cost: $0.00" in chat.said[0]


class _Provider(AIProvider):
    name = "claude"
    model = "claude-sonnet-5-5"

    async def chat(self, messages, tools=None):
        return AIResponse(content="hi", tool_calls=[], usage={"input": 100, "output": 10})

    async def chat_stream(self, messages, tools=None):
        yield AIResponse(content="hi", tool_calls=[], usage={"input": 100, "output": 10})


async def test_metered_calls_inside_a_goal_are_recorded_under_the_goal(ledger):
    provider = meter(_Provider(), agent="coder", role="main", ledger=ledger, budget=SimpleNamespace(
        daily_usd=0, daily_tokens=10**9, warn_at=0))
    await provider.chat([Message(role="user", content="a")])
    token = usage_tag.set(("goal:abc", "goal"))
    try:
        await provider.chat([Message(role="user", content="b")])
    finally:
        usage_tag.reset(token)
    usd, _ = await ledger.spent_on("goal:abc", "goal")
    assert usd > 0
    async with ledger.db.execute("SELECT agent, chat, role FROM usage ORDER BY ts") as cur:
        rows = await cur.fetchall()
    assert rows == [("coder", "", "main"), ("coder", "goal:abc", "goal")]


# --- the gateway commands and paw goal ---

async def test_gateway_goal_commands(store, ws, no_sandbox):
    from micropaw.gateway import Gateway
    agent = FakeAgent(store, ws, script=lambda n, a, c: asyncio.sleep(30))
    gw = Gateway.__new__(Gateway)
    gw.store = store
    gw.goals = manager(agent, Chat())
    gw._route = lambda platform, user_id, channel_id: agent
    assert "Usage: /goal" in await gw._owner_command("/goal", "terminal", "u", None)
    assert "No goal is running" in await gw._owner_command("/goal status", "terminal", "u", None)
    assert "shell is off" in await gw._owner_command('/goal build it --check "true"', "terminal", "u", None)
    assert "started" in await gw._owner_command("/goal build it --max 3", "terminal", "u", None)
    assert "round" in await gw._owner_command("/goal status", "terminal", "u", None)
    await gw.goals.close()


async def test_gateway_skills_command(store):
    from micropaw.gateway import Gateway
    gw = Gateway.__new__(Gateway)
    gw.store = store
    gw.goals = None
    out = await gw._owner_command("/skills", "terminal", "u", None)
    assert "game-dev-html5" in out and "re-triage-binary" in out


def test_paw_goal_rejects_bad_options_before_starting_anything():
    from micropaw.cli import main
    result = CliRunner().invoke(main, ["goal", "do it", "--max", "500"])
    assert result.exit_code != 0 and "--max" in result.output


# --- the real agent in a goal round ---

class _Peek(Tool):
    name = "peek"
    description = "reports the mark"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    seen: list[bool] = []

    async def execute(self, args, context):
        self.seen.append(context.marked)
        return "peeked"


class _Script(AIProvider):
    name = "ollama"
    model = "m"

    def __init__(self):
        self.n = 0

    async def chat(self, messages, tools=None):
        self.n += 1
        if self.n == 1:
            return AIResponse(content="", tool_calls=[ToolCall(id="t1", name="peek", arguments={})], usage={})
        return AIResponse(content="done", tool_calls=[], usage={})

    async def chat_stream(self, messages, tools=None):
        yield await self.chat(messages, tools)


async def test_a_real_agent_round_runs_in_its_own_conversation_and_starts_marked(store, ws):
    from micropaw.config import Config
    from micropaw.core.agent import Agent

    cfg = Config()
    cfg.permissions.workspace = str(ws)
    registry = ToolRegistry()
    peek = _Peek()
    peek.seen = []
    registry.register(peek)
    agent = Agent(cfg, _Script(), registry, store)
    await store.get_or_create_conversation("goal:abc:2", "terminal", "u")
    await store.set_marked("goal:abc:2")
    reply = await agent.handle_message("round prompt", "u", "terminal", None, conv_id="goal:abc:2")
    assert reply == "done" and peek.seen == [True]
    saved = await store.get_messages("goal:abc:2")
    assert [m.role for m in saved] == ["user", "assistant", "tool", "assistant"]
    assert await store.get_messages("dm:owner") == []  # the chat's own history is untouched
