"""Event grouping: `uv run python -m wikipulse.events.run [--spike-run live] [--once]`.

Reads the spikes of a detector run (with their enriched pages) from Postgres, groups
them into events with `Grouper`, and writes the `events` and `event_pages` tables
(apply migrations first: `uv run python -m wikipulse.db.migrate`).

- Live: run it next to the detector and the enricher. Every EVENTS_POLL_INTERVAL_S
  seconds it groups new spikes, oldest first. A spike whose page is not enriched yet
  waits up to EVENTS_ENRICH_WAIT_S seconds; Ctrl+C stops it. A restart continues where
  it stopped (the active events are rebuilt from Postgres).
- Replay a recorded day: `--spike-run 02b-loose --run-id 03b-day --since 2026-11-05
  --until 2026-11-06 --once --reset` groups that day's spikes in order and exits.

`--run-id` names the grouping run (default: the spike run's name), so different
weights can be tried on the same spikes side by side.
"""

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import psycopg

from wikipulse.config import EventsConfig, get_settings
from wikipulse.db.events import (
    SpikeRow,
    active_members,
    delete_grouping_run,
    run_info,
    save_events,
    spike_ends,
    unassigned_spikes,
)
from wikipulse.db.spikes import latest_edit_time
from wikipulse.enrich.run import parse_day_or_time
from wikipulse.events.embed import Embedder, page_text
from wikipulse.events.grouper import Event, Grouper, Member
from wikipulse.events.similarity import SpikePage
from wikipulse.health import Heartbeat
from wikipulse.logging_setup import setup_logging
from wikipulse.metrics.catalog import GROUPER_ACTIVE_EVENTS, GROUPER_SPIKES, start_at_zero
from wikipulse.metrics.server import start_metrics_server
from wikipulse.stream.resilience import install_stop_handlers, wait_or_stop

logger = logging.getLogger(__name__)


def row_text(row: SpikeRow) -> str:
    """What gets embedded for a spike: its page's title, description and summary."""
    return page_text(row.canonical_title or row.title, row.description, row.summary)


def to_pages(rows: list[SpikeRow], embedder: Embedder) -> list[SpikePage]:
    """SpikePages for rows, with embeddings computed in one batch."""
    vectors = embedder.embed_many([row_text(row) for row in rows])
    return [
        SpikePage(
            spike_id=row.spike_id,
            title=row.title,
            canonical_title=row.canonical_title or row.title,
            started_at=row.started_at,
            ended_at=row.ended_at,
            score=row.score,
            links=frozenset(row.links or ()),
            embedding=vector,
        )
        for row, vector in zip(rows, vectors, strict=True)
    ]


def ready_rows(
    rows: list[SpikeRow], first_seen: dict[int, float], now: float, wait_s: float
) -> list[SpikeRow]:
    """The spikes to group now, in order, stopping at one still waiting for its page.

    `first_seen` (spike id -> when it was first seen, monotonic seconds) is updated. A
    spike whose page isn't enriched waits until `wait_s` seconds after it was first
    seen; the spikes after it wait too, so the input order never changes.
    """
    ready = []
    for row in rows:
        seen = first_seen.setdefault(row.spike_id, now)
        if not row.enriched and now - seen < wait_s:
            break
        ready.append(row)
    return ready


@dataclass
class RunStats:
    spikes: int = 0
    created: int = 0
    joined: int = 0
    merges: int = 0
    not_enriched: int = 0

    def summary(self) -> str:
        return (
            f"Grouped {self.spikes} spikes: {self.created} new events, {self.joined} joined "
            f"an event, {self.merges} merges, {self.not_enriched} without an enriched page."
        )


class GroupingRunner:
    """Feeds a detector run's spikes to a Grouper and stores the events."""

    def __init__(
        self,
        conn: psycopg.Connection,
        embedder: Embedder,
        config: EventsConfig,
        run_id: str,
        spike_run_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        enrich_wait_s: float | None = None,
    ) -> None:
        self.conn = conn
        self.embedder = embedder
        self.run_id = run_id
        self.spike_run_id = spike_run_id
        self.since = since
        self.until = until
        # None: don't wait for enrichment (a backfill groups what is there).
        self.enrich_wait_s = enrich_wait_s
        self.first_seen: dict[int, float] = {}  # spike id -> monotonic time
        self.stats = RunStats()
        start_at_zero(GROUPER_SPIKES, "created", "joined")
        self.grouper = self._restore(config)

    def _restore(self, config: EventsConfig) -> Grouper:
        """A Grouper holding the run's active events as stored in Postgres."""
        stored_spike_run, next_number, clock = run_info(self.conn, self.run_id)
        if stored_spike_run not in (None, self.spike_run_id):
            raise SystemExit(
                f"Grouping run {self.run_id!r} groups spike run {stored_spike_run!r}, "
                f"not {self.spike_run_id!r}; choose another --run-id or use --reset"
            )
        rows = active_members(self.conn, self.run_id)
        pages = to_pages([row for _, _, row in rows], self.embedder)
        events: dict[int, Event] = {}
        for (number, joined, _), page in zip(rows, pages, strict=True):
            events.setdefault(number, Event(number)).members.append(Member(page, joined))
        for event in events.values():
            event.refresh()
        if events:
            logger.info("Restored %d active events of run %s", len(events), self.run_id)
        return Grouper(config, next_number, list(events.values()), clock)

    def ready(self, rows: list[SpikeRow]) -> list[SpikeRow]:
        if self.enrich_wait_s is None:
            return rows
        return ready_rows(rows, self.first_seen, time.monotonic(), self.enrich_wait_s)

    def step(self) -> int:
        """One round: refresh spike ends, group new spikes, close events, save."""
        ends = spike_ends(self.conn, list(self.grouper.event_of))
        for spike_id, ended_at in ends.items():
            self.grouper.update_end(spike_id, ended_at)
        rows = self.ready(
            unassigned_spikes(self.conn, self.run_id, self.spike_run_id, self.since, self.until)
        )
        for page, row in zip(to_pages(rows, self.embedder), rows, strict=True):
            assignment = self.grouper.add(page)
            self.first_seen.pop(row.spike_id, None)
            self.stats.spikes += 1
            self.stats.not_enriched += not row.enriched
            self.stats.created += assignment.created
            GROUPER_SPIKES.labels("created" if assignment.created else "joined").inc()
            self.stats.joined += not assignment.created
            self.stats.merges += len(assignment.merged)
        watermark = latest_edit_time(self.conn, self.spike_run_id)
        if watermark is not None and self.until is not None:
            watermark = min(watermark, self.until)  # a replay stops at the end of its range
        if watermark is not None:
            self.grouper.advance(watermark)
        save_events(self.conn, self.run_id, self.spike_run_id, self.grouper.take_changes())
        GROUPER_ACTIVE_EVENTS.set(len(self.grouper.active))
        return len(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--spike-run", default="live", help="detector run whose spikes to group")
    parser.add_argument("--run-id", help="name of this grouping run (default: --spike-run)")
    parser.add_argument("--since", type=parse_day_or_time, help="only spikes started at/after")
    parser.add_argument("--until", type=parse_day_or_time, help="only spikes started before")
    parser.add_argument("--once", action="store_true", help="group what is there, then exit")
    parser.add_argument("--reset", action="store_true", help="delete the run's events first")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace, stop: asyncio.Event) -> RunStats:
    settings = get_settings()
    run_id = args.run_id or args.spike_run
    embedder = Embedder(settings.events_embed_model)
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        if args.reset:
            logger.info("Deleted %d events of run %s", delete_grouping_run(conn, run_id), run_id)
        runner = GroupingRunner(
            conn,
            embedder,
            settings.events_config(),
            run_id,
            args.spike_run,
            args.since,
            args.until,
            enrich_wait_s=None if args.once else settings.events_enrich_wait_s,
        )
        started = time.monotonic()
        heartbeat = Heartbeat.from_settings(settings)
        while not stop.is_set():
            heartbeat.beat()
            grouped = await asyncio.to_thread(runner.step)
            if grouped:
                active = len(runner.grouper.active)
                logger.info("%s (%d active events)", runner.stats.summary(), active)
            if args.once or await wait_or_stop(stop, settings.events_poll_interval_s):
                break
        logger.info("Took %.1f s", time.monotonic() - started)
    return runner.stats


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    start_metrics_server(get_settings())
    stop = asyncio.Event()
    install_stop_handlers(stop)
    stats = await run(args, stop)
    print(stats.summary())
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())
