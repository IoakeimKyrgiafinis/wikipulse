"""The trigger: `uv run python -m wikipulse.agent.run [--once] [--event KEY ...]`.

Every AGENT_POLL_INTERVAL_S seconds it asks the graph for significant events without
an up-to-date briefing (best spike score >= AGENT_MIN_SPIKE_SCORE, or at least
AGENT_MIN_PAGES pages), biggest first, and runs the analyst agent on each:

    trigger ── event key ──► AnalystAgent ──► MCP client ──stdio──► WikiPulse MCP server
                                  │                                   (a subprocess)
                                  ├─ local tools ──► Neo4j (READ)
                                  ├─ LLM ──► wikipulse.llm (AGENT_MODEL)
                                  ▼
               briefing ──► Event node (BriefingStore)   transcript ──► data/agent_runs/

- `--once`: one round, then exit. `--limit N`: at most N events per round.
- `--event KEY` (repeatable): just these events, whatever their strength.
- `--dry-run`: run the agent and save transcripts, but don't write briefings.
- `--force`: brief events again even if their briefing is up to date.
- `--neo4j-uri`: another graph (the MCP server subprocess gets the same one).
- `--mcp-url http://127.0.0.1:8765/mcp`: use a running HTTP server instead.

A run that ends without a briefing is not tried again for the same version of the
event until the trigger restarts.
"""

import argparse
import asyncio
import logging
import sys
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path

from mcp import Client, StdioServerParameters
from neo4j import GraphDatabase

from wikipulse.agent.loop import AgentLimits, AgentRun, AnalystAgent
from wikipulse.agent.store import BriefingMeta, BriefingStore, Candidate
from wikipulse.agent.tools import ToolBox, ToolLimits
from wikipulse.agent.transcript import save_transcript
from wikipulse.config import Settings, get_settings
from wikipulse.llm import get_client
from wikipulse.logging_setup import log_context, setup_logging
from wikipulse.qa.graph import QAGraph
from wikipulse.stream.resilience import install_stop_handlers, wait_or_stop

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="one round, then exit")
    parser.add_argument("--limit", type=int, default=50, help="most events per round")
    parser.add_argument("--event", action="append", help="brief this event (repeatable)")
    parser.add_argument("--run-id", help="only events of this grouping run")
    parser.add_argument("--dry-run", action="store_true", help="don't write briefings")
    parser.add_argument("--force", action="store_true", help="brief up-to-date events again")
    parser.add_argument("--model", help="backend:model (default: AGENT_MODEL)")
    parser.add_argument("--think", action="store_true", help="let the model think (AGENT_THINK)")
    parser.add_argument("--min-score", type=float, help="default: AGENT_MIN_SPIKE_SCORE")
    parser.add_argument("--min-pages", type=int, help="default: AGENT_MIN_PAGES")
    parser.add_argument("--neo4j-uri", help="another graph, e.g. bolt://localhost:7688")
    parser.add_argument("--mcp-url", help="a running MCP server over HTTP")
    return parser.parse_args(argv)


def mcp_client(settings: Settings, mcp_url: str | None) -> AbstractAsyncContextManager[Client]:
    """A client of the WikiPulse MCP server: a running one, or a stdio subprocess."""
    if mcp_url:
        return Client(mcp_url)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "wikipulse.mcp_server.server", "--neo4j-uri", settings.neo4j_uri],
        cwd=str(Path.cwd()),
    )
    return Client(server)


def summary(run: AgentRun) -> str:
    """One log line about a finished agent run."""
    result = (
        f"briefing with {len(run.briefing.citations)} sources"
        if run.briefing
        else f"no briefing ({run.error})"
    )
    return (
        f"{run.event_key or run.event_id}: {result}; stop {run.stop_reason}, "
        f"{run.model_turns} model turns, {run.tool_calls} tool calls, {run.tokens} tokens, "
        f"{run.seconds:.0f} s"
    )


class Trigger:
    """Finds significant events and has the agent brief them."""

    def __init__(
        self,
        agent: AnalystAgent,
        store: BriefingStore,
        settings: Settings,
        args: argparse.Namespace,
    ) -> None:
        self.agent = agent
        self.store = store
        self.settings = settings
        self.args = args
        self.tried: dict[str, str | None] = {}  # key -> event version that got no briefing
        self.runs: list[AgentRun] = []

    async def candidates(self) -> list[Candidate]:
        """The events of this round: `--event`, or significant events not yet briefed."""
        if self.args.event:
            return [
                Candidate(key, None, await asyncio.to_thread(self.store.correlation_id, key))
                for key in self.args.event
            ]
        args, settings = self.args, self.settings
        found = await asyncio.to_thread(
            self.store.candidates,
            args.min_score if args.min_score is not None else settings.agent_min_spike_score,
            args.min_pages if args.min_pages is not None else settings.agent_min_pages,
            args.limit + len(self.tried),
            args.run_id,
            args.force,
        )
        fresh = [c for c in found if c.key not in self.tried or self.tried[c.key] != c.version]
        return fresh[: args.limit]

    async def brief(self, candidate: Candidate) -> AgentRun:
        """One agent run; its logs and trace carry the event's correlation id (spec 09b)."""
        with log_context(correlation_id=candidate.correlation_id, event_key=candidate.key):
            return await self._brief(candidate)

    async def _brief(self, candidate: Candidate) -> AgentRun:
        logger.info("Briefing event %s", candidate.key)
        run = await self.agent.run(candidate.key)
        path = save_transcript(run, self.settings.data_dir / "agent_runs")
        if run.briefing is not None and run.event_key and not self.args.dry_run:
            meta = BriefingMeta(
                self.agent.client.setting, self.agent.prompt_version, datetime.now(UTC), path.name
            )
            await asyncio.to_thread(self.store.save, run.event_key, run.briefing, meta)
        elif run.briefing is None:
            self.tried[candidate.key] = candidate.version
        logger.info("%s (transcript %s)", summary(run), path)
        self.runs.append(run)
        return run

    async def round(self, stop: asyncio.Event) -> int:
        found = await self.candidates()
        for candidate in found:
            if stop.is_set():
                break
            await self.brief(candidate)
        return len(found)


async def run(args: argparse.Namespace, settings: Settings, stop: asyncio.Event) -> str:
    """Run the trigger with the command-line options; returns the last summary."""
    if args.neo4j_uri:
        settings = settings.model_copy(update={"neo4j_uri": args.neo4j_uri})
    if args.model:
        settings = settings.model_copy(update={"agent_model": args.model})
    if args.think:
        settings = settings.model_copy(update={"agent_think": True})
    auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=auth)
    limits = ToolLimits(
        settings.agent_tool_timeout_s, settings.agent_ask_timeout_s, settings.agent_max_result_chars
    )
    try:
        async with mcp_client(settings, args.mcp_url) as mcp:
            with get_client(settings.agent_model, settings) as llm:
                tools = ToolBox(mcp, QAGraph(driver), limits)
                await tools.load()
                agent = AnalystAgent(
                    llm,
                    tools,
                    AgentLimits.from_settings(settings),
                    think=settings.agent_think,
                    verify=settings.agent_verify,
                )
                trigger = Trigger(agent, BriefingStore(driver), settings, args)
                while not stop.is_set():
                    count = await trigger.round(stop)
                    logger.info("Round done: %d events", count)
                    if args.once or args.event:
                        break
                    if await wait_or_stop(stop, settings.agent_poll_interval_s):
                        break
    finally:
        driver.close()
    briefed = sum(1 for r in trigger.runs if r.briefing)
    lines = [summary(r) for r in trigger.runs]
    lines.append(f"{briefed} of {len(trigger.runs)} runs produced a briefing.")
    return "\n".join(lines)


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    stop = asyncio.Event()
    install_stop_handlers(stop)
    print(await run(args, settings, stop))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())
