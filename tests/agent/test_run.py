"""The trigger: which events get the agent, and what happens to the result."""

import asyncio
from datetime import UTC, datetime

from wikipulse.agent.briefing import Briefing
from wikipulse.agent.loop import AgentRun
from wikipulse.agent.run import Trigger, parse_args, summary
from wikipulse.agent.store import Candidate
from wikipulse.config import Settings
from wikipulse.llm.types import Usage
from wikipulse.qa.types import Citation

BRIEFING = Briefing(text="A [1].", citations=[Citation(title="A", revision=1)])


class FakeStore:
    def __init__(self, found: list[Candidate]) -> None:
        self.found = found
        self.asked: list[tuple] = []
        self.saved: list[tuple] = []

    def candidates(self, min_score, min_pages, limit=50, run_id=None, force=False):
        self.asked.append((min_score, min_pages, limit, run_id, force))
        return self.found

    def save(self, key, briefing, meta) -> bool:
        self.saved.append((key, briefing, meta))
        return True


class FakeClient:
    setting = "fake:m"


class FakeAgent:
    client = FakeClient()
    prompt_version = "analyst_v1"

    def __init__(self, briefed: set[str]) -> None:
        self.briefed = briefed
        self.runs: list[str] = []

    async def run(self, key: str) -> AgentRun:
        self.runs.append(key)
        return AgentRun(
            event_id=key,
            event_key=key,
            model="fake:m",
            prompt_version="analyst_v1",
            started_at=datetime(2026, 11, 9, tzinfo=UTC),
            stop_reason="answered",
            briefing=BRIEFING if key in self.briefed else None,
            error=None if key in self.briefed else "no valid source id",
            usage=Usage(input_tokens=100, output_tokens=20),
        )


def trigger(tmp_path, found, briefed, *argv: str) -> tuple[Trigger, FakeStore, FakeAgent]:
    store, agent = FakeStore(found), FakeAgent(briefed)
    settings = Settings(_env_file=None, data_dir=tmp_path)
    return Trigger(agent, store, settings, parse_args(list(argv))), store, agent


async def test_significant_events_are_briefed_and_saved(tmp_path) -> None:
    found = [Candidate("run:1", "v1"), Candidate("run:2", "v1")]
    t, store, agent = trigger(tmp_path, found, {"run:1"})
    assert await t.round(asyncio.Event()) == 2
    assert agent.runs == ["run:1", "run:2"]
    assert store.asked == [(9.0, 3, 50, None, False)]  # AGENT_MIN_SPIKE_SCORE, AGENT_MIN_PAGES
    [(key, briefing, meta)] = store.saved
    assert key == "run:1" and briefing == BRIEFING and meta.model == "fake:m"
    assert (tmp_path / "agent_runs" / meta.transcript).exists()
    assert len(list((tmp_path / "agent_runs").glob("*.json"))) == 2


async def test_a_failed_event_is_not_retried_until_it_changes(tmp_path) -> None:
    t, store, agent = trigger(tmp_path, [Candidate("run:2", "v1")], set())
    await t.round(asyncio.Event())
    await t.round(asyncio.Event())
    assert agent.runs == ["run:2"]
    store.found = [Candidate("run:2", "v2")]  # extracted again
    await t.round(asyncio.Event())
    assert agent.runs == ["run:2", "run:2"]


async def test_options(tmp_path) -> None:
    t, store, agent = trigger(
        tmp_path,
        [],
        {"run:5"},
        "--event",
        "run:5",
        "--dry-run",
    )
    await t.round(asyncio.Event())
    assert agent.runs == ["run:5"] and store.asked == [] and store.saved == []
    t, store, _ = trigger(
        tmp_path, [], set(), "--min-score", "2", "--min-pages", "5", "--limit", "3", "--force"
    )
    await t.round(asyncio.Event())
    assert store.asked == [(2.0, 5, 3, None, True)]


async def test_a_stop_ends_the_round(tmp_path) -> None:
    t, _, agent = trigger(tmp_path, [Candidate("run:1", "v1")], set())
    stop = asyncio.Event()
    stop.set()
    await t.round(stop)
    assert agent.runs == []


def test_summary_line() -> None:
    run = AgentRun(
        event_id="run:1",
        model="m",
        prompt_version="v",
        started_at=datetime(2026, 11, 9, tzinfo=UTC),
        stop_reason="answered",
        briefing=BRIEFING,
        model_turns=3,
        tool_calls=4,
        seconds=12.4,
        usage=Usage(input_tokens=1000, output_tokens=50),
    )
    assert summary(run) == (
        "run:1: briefing with 1 sources; stop answered, 3 model turns, 4 tool calls, "
        "1050 tokens, 12 s"
    )
