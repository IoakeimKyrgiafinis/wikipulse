"""Knowledge graph extraction: `uv run python -m wikipulse.graph.run [--run-id live] [--once]`.

Watches the events of a grouping run (stage 3) in Postgres. For each new event, each
event whose spikes changed, and each event that has just ended, it

1. reads the event's pages (summary, Wikidata ID, links) and fetches the text added
   during each page's burst of edits (`wikipulse.graph.inputs`),
2. asks the LLM (EXTRACT_MODEL) for a title, a one-sentence description, the entities
   and the relationships between them (`wikipulse.graph.extract`),
3. links the entities to Wikidata (`wikipulse.graph.link`),
4. replaces the event's part of the graph in Neo4j (`wikipulse.graph.store`).

A failure is logged and recorded per event (table `graph_extractions`) and the event is
tried again later; the other events go on. Events that were merged into another or
deleted from Postgres (e.g. by `wikipulse.events.run --reset`) leave the graph.

- Live: run it next to the grouper. Every GRAPH_POLL_INTERVAL_S seconds it processes new
  and changed events, oldest first; Ctrl+C stops it after the current event.
- A recorded day: `--run-id 03b-default-fix --since 2026-11-05 --until 2026-11-06 --once`
  processes that day's events and prints a report (events, entities, linked share,
  average time per event). `--limit N` stops after N events.
"""

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
from neo4j.exceptions import DriverError, Neo4jError

from wikipulse.config import Settings, get_settings
from wikipulse.db.graph import (
    ExtractionResult,
    current_events,
    extracted_events,
    forget_extraction,
    load_event,
    pending_events,
    record_failure,
    record_success,
)
from wikipulse.enrich.cache import PostgresCache
from wikipulse.enrich.client import WikiApiError, WikimediaClient
from wikipulse.enrich.run import parse_day_or_time
from wikipulse.graph.extract import Extractor
from wikipulse.graph.inputs import EventInput, event_key, with_changes
from wikipulse.graph.link import Linker
from wikipulse.graph.store import GraphCounts, GraphStore, build_graph_event
from wikipulse.health import Heartbeat
from wikipulse.llm import LLMError, get_client, trace
from wikipulse.logging_setup import log_context, setup_logging
from wikipulse.metrics.catalog import EXTRACTION_DURATION, EXTRACTIONS, start_at_zero
from wikipulse.metrics.server import start_metrics_server
from wikipulse.stream.resilience import install_stop_handlers, wait_or_stop

logger = logging.getLogger(__name__)

# A failed event is tried again after this long (doubling with each failure in a row).
RETRY = timedelta(minutes=10)
# What can go wrong with one event without stopping the runner.
EVENT_ERRORS = (
    LLMError,
    WikiApiError,
    httpx.HTTPError,
    Neo4jError,
    DriverError,
    KeyError,
    TypeError,
    ValueError,
)


@dataclass
class RunStats:
    events: int = 0
    failed: int = 0
    entities: int = 0  # entities written, summed over events
    linked: int = 0
    relations: int = 0
    seconds: float = 0.0  # time spent on successful events
    llm_seconds: float = 0.0
    output_tokens: int = 0
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        average = self.seconds / self.events if self.events else 0.0
        llm = self.llm_seconds / self.events if self.events else 0.0
        share = self.linked / self.entities if self.entities else 0.0
        return (
            f"Processed {self.events} events ({self.failed} failed). Summed over events "
            f"(an entity counts once per event): {self.entities} entities, {share:.0%} "
            f"linked, {self.relations} relations. "
            f"Average time per event {average:.1f} s (LLM {llm:.1f} s, "
            f"{self.output_tokens / max(self.events, 1):.0f} tokens out)."
        )


def stale_events(
    run_id: str, graph_keys: list[str], extracted: list[int], current: list[int]
) -> list[int]:
    """Event numbers in the graph or with an extraction row that are no longer current.

    `current` are the run's events in Postgres that are not merged away. Anything else is
    an event that was merged into another or deleted (its node would be a phantom).
    """
    in_graph = set()
    for key in graph_keys:
        key_run, _, number = key.rpartition(":")  # keys are "<run id>:<number>"
        if key_run == run_id:
            in_graph.add(int(number))
    return sorted((in_graph | set(extracted)) - set(current))


def graph_summary(run_id: str, counts: GraphCounts) -> str:
    """The graph of a run in one line."""
    methods = ", ".join(f"{name} {count}" for name, count in counts.by_method.items())
    return (
        f"Graph of run {run_id}: {counts.events} events, {counts.entities} distinct "
        f"entities, {counts.linked_share:.0%} linked to Wikidata ({methods}), "
        f"{counts.relations} relations."
    )


class GraphRunner:
    """Extracts events of one grouping run into the graph, one event at a time."""

    def __init__(
        self,
        conn: psycopg.Connection,
        store: GraphStore,
        extractor: Extractor,
        linker: Linker,
        wiki: WikimediaClient,
        api_url: str,
        run_id: str,
        max_pages: int = 6,
        max_chars: int = 1200,
    ) -> None:
        self.conn = conn
        self.store = store
        self.extractor = extractor
        self.linker = linker
        self.wiki = wiki
        self.api_url = api_url
        self.run_id = run_id
        self.max_pages = max_pages
        self.max_chars = max_chars
        self.stats = RunStats()
        start_at_zero(EXTRACTIONS, "ok", "failed")

    async def process(self, number: int) -> bool:
        """Extract one event into the graph. False if it failed (recorded, not raised)."""
        started = time.perf_counter()
        event = await asyncio.to_thread(load_event, self.conn, self.run_id, number)
        if event is None:
            return True  # merged away meanwhile
        # Logs and the LLM trace of this event carry its correlation id (spec 09b).
        with log_context(correlation_id=event.correlation_id, event_key=event.key):
            return await self._process(event, started)

    async def _process(self, event: EventInput, started: float) -> bool:
        try:
            with trace("graph extraction", as_type="chain", input={"event_key": event.key}):
                result = await self._extract(event, started)
        except EVENT_ERRORS as error:
            seconds = time.perf_counter() - started
            message = f"{type(error).__name__}: {error}"
            logger.warning("Event %s (%s) failed, will retry: %s", event.key, event.title, message)
            await asyncio.to_thread(record_failure, self.conn, event, message, seconds)
            EXTRACTIONS.labels("failed").inc()
            self.stats.failed += 1
            self.stats.failures.append(f"{event.key}: {message}")
            return False
        await asyncio.to_thread(record_success, self.conn, event, result)
        EXTRACTIONS.labels("ok").inc()
        EXTRACTION_DURATION.observe(result.seconds)
        self._count(result)
        logger.info(
            "Event %s: %d entities (%d linked), %d relations in %.1f s",
            event.key,
            result.entities,
            result.linked,
            result.relations,
            result.seconds,
        )
        return True

    async def _extract(self, event: EventInput, started: float) -> ExtractionResult:
        shown = await with_changes(
            self.wiki, self.api_url, event.pages[: self.max_pages], self.max_chars
        )
        extraction = await asyncio.to_thread(self.extractor.extract, event, shown)
        types = {entity.name: entity.type for entity in extraction.entities}
        links = await self.linker.link(list(types), event.pages, types)
        pages = [*shown, *event.pages[self.max_pages :]]
        graph_event = build_graph_event(event, pages, extraction, links, datetime.now(UTC))
        await asyncio.to_thread(self.store.write_event, graph_event)
        entities = graph_event.entities
        return ExtractionResult(
            model=extraction.model,
            prompt_version=extraction.prompt_version,
            pages=len(shown),
            entities=len(entities),
            linked=sum(entity.wikidata_id is not None for entity in entities),
            relations=len(graph_event.relations),
            seconds=time.perf_counter() - started,
            input_tokens=extraction.usage.input_tokens,
            output_tokens=extraction.usage.output_tokens,
            llm_seconds=extraction.usage.latency_s,
        )

    def _count(self, result: ExtractionResult) -> None:
        stats = self.stats
        stats.events += 1
        stats.entities += result.entities
        stats.linked += result.linked
        stats.relations += result.relations
        stats.seconds += result.seconds
        stats.llm_seconds += result.llm_seconds
        stats.output_tokens += result.output_tokens

    async def remove_stale(self) -> int:
        """Delete events from the graph that were merged away or deleted from Postgres."""
        # Read Postgres first: an event created after this read is not in the graph yet.
        current = await asyncio.to_thread(current_events, self.conn, self.run_id)
        extracted = await asyncio.to_thread(extracted_events, self.conn, self.run_id)
        graph_keys = await asyncio.to_thread(self.store.event_keys, self.run_id)
        numbers = stale_events(self.run_id, graph_keys, extracted, current)
        for number in numbers:
            await asyncio.to_thread(self.store.delete_event, event_key(self.run_id, number))
            await asyncio.to_thread(forget_extraction, self.conn, self.run_id, number)
        if numbers:
            logger.info(
                "Removed %d stale event(s) of run %s from the graph", len(numbers), self.run_id
            )
        return len(numbers)

    async def step(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        force: bool = False,
        limit: int | None = None,
        stop: asyncio.Event | None = None,
        heartbeat: Callable[[], None] = lambda: None,
    ) -> int:
        """One round: drop stale events, then process pending ones. Returns the count.

        `heartbeat` is called after every event (see `wikipulse.health`).
        """
        await self.remove_stale()
        numbers = await asyncio.to_thread(
            pending_events, self.conn, self.run_id, RETRY, since, until, force, limit
        )
        if numbers:
            logger.info("%d event(s) of run %s to extract", len(numbers), self.run_id)
        done = 0
        for number in numbers:
            if stop is not None and stop.is_set():
                break
            await self.process(number)
            done += 1
            heartbeat()
        return done

    def reset(self) -> int:
        """Remove the run's events from the graph and forget their extractions."""
        numbers = extracted_events(self.conn, self.run_id)
        for key in self.store.event_keys(self.run_id):
            self.store.delete_event(key)
        for number in numbers:
            forget_extraction(self.conn, self.run_id, number)
        return len(numbers)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", default="live", help="grouping run whose events to extract")
    parser.add_argument("--since", type=parse_day_or_time, help="only events started at/after")
    parser.add_argument("--until", type=parse_day_or_time, help="only events started before")
    parser.add_argument("--once", action="store_true", help="process what is there, then exit")
    parser.add_argument("--limit", type=int, help="process at most this many events per round")
    parser.add_argument("--force", action="store_true", help="extract done events again too")
    parser.add_argument("--reset", action="store_true", help="remove the run's graph first")
    parser.add_argument("--model", help="backend:model (default: EXTRACT_MODEL)")
    return parser.parse_args(argv)


def wiki_client(settings: Settings, cache_conn: psycopg.Connection) -> WikimediaClient:
    """The same polite, cached client as page enrichment (stage 3)."""
    return WikimediaClient(
        settings.user_agent,
        PostgresCache(cache_conn),
        concurrency=settings.wiki_api_concurrency,
        ttl=timedelta(hours=settings.enrich_cache_ttl_h),
        min_interval_s=settings.enrich_min_interval_s,
    )


async def run(args: argparse.Namespace, settings: Settings, stop: asyncio.Event) -> str:
    """Process events until `stop` (or once); returns the final report."""
    with (
        psycopg.connect(settings.postgres_dsn, autocommit=True) as conn,
        psycopg.connect(settings.postgres_dsn, autocommit=True) as cache_conn,
        GraphStore.from_settings(settings) as store,
        get_client(args.model or settings.extract_model) as llm,
    ):
        await asyncio.to_thread(store.ensure_schema)
        async with wiki_client(settings, cache_conn) as wiki:
            runner = GraphRunner(
                conn,
                store,
                Extractor(llm, settings.extract_max_chars),
                Linker(wiki, settings.wiki_api_url, settings.wikidata_api_url),
                wiki,
                settings.wiki_api_url,
                args.run_id,
                settings.extract_max_pages,
                settings.extract_max_chars,
            )
            if args.reset:
                removed = await asyncio.to_thread(runner.reset)
                logger.info("Removed %d extracted events of run %s", removed, args.run_id)
            force = args.force
            heartbeat = Heartbeat.from_settings(settings)
            while not stop.is_set():
                heartbeat.beat()
                await runner.step(args.since, args.until, force, args.limit, stop, heartbeat)
                if args.once or await wait_or_stop(stop, settings.graph_poll_interval_s):
                    break
                force = False  # --force applies to the first round only
            counts = await asyncio.to_thread(store.run_counts, args.run_id)
            stats = wiki.stats
            api = (
                f"Wikimedia API: {stats.requests} requests, {stats.cache_hits} cache hits, "
                f"{stats.retries} retries."
            )
        usage = llm.total_usage
        model = (
            f"LLM {llm.setting}: {usage.calls} calls, {usage.input_tokens} tokens in, "
            f"{usage.output_tokens} out, {usage.latency_s:.0f} s."
        )
    return "\n".join([runner.stats.summary(), graph_summary(args.run_id, counts), api, model])


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    start_metrics_server(settings)
    stop = asyncio.Event()
    install_stop_handlers(stop)
    print(await run(args, settings, stop))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())
