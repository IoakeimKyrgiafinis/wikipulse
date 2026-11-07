"""Page enrichment: `uv run python -m wikipulse.enrich.run [--run-id live] [--once]`.

Watches the `spikes` table for spikes of one run (default `live`) and enriches each
spiking page: canonical title, summary, Wikidata ID, categories and links, stored in
the `pages` table (apply migrations first: `uv run python -m wikipulse.db.migrate`).
It looks for new spikes every ENRICH_POLL_INTERVAL_S seconds.

- Live: run it next to `wikipulse.detect.run`; Ctrl+C stops it.
- Backfill a day: `--run-id 02b-baseline --since 2026-11-06 --until 2026-11-07 --once`
  enriches that day's spikes and exits with a report (API calls, cache hit rate).
- `--force` enriches pages again even if they are fresh (API responses still come from
  the cache while it is fresh, so this costs no requests within ENRICH_CACHE_TTL_H).
"""

import argparse
import asyncio
import logging
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx
import psycopg

from wikipulse.config import Settings, get_settings
from wikipulse.db.pages import pending_titles, record_error, upsert_page
from wikipulse.enrich.cache import PostgresCache
from wikipulse.enrich.client import ClientStats, WikiApiError, WikimediaClient
from wikipulse.enrich.pages import Enricher, PageStatus
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.resilience import install_stop_handlers, wait_or_stop

logger = logging.getLogger(__name__)

# A page whose enrichment failed is tried again after this long.
ERROR_RETRY = timedelta(minutes=10)


@dataclass
class RunReport:
    pages: int = 0
    statuses: Counter = field(default_factory=Counter)
    seconds: float = 0.0
    client: ClientStats = field(default_factory=ClientStats)

    def summary(self) -> str:
        c = self.client
        statuses = ", ".join(f"{k} {v}" for k, v in sorted(self.statuses.items())) or "none"
        return (
            f"Enriched {self.pages} pages in {self.seconds:.1f} s ({statuses}). "
            f"API calls: {c.requests} (retries {c.retries}, most in flight {c.max_in_flight}). "
            f"Cache: {c.cache_hits} hits, {c.cache_misses} misses, "
            f"hit rate {c.hit_rate:.1%}."
        )


async def enrich_one(enricher: Enricher, conn: psycopg.Connection, title: str) -> PageStatus:
    """Enrich and store one title; a failure is recorded, not raised."""
    try:
        info = await enricher.enrich(title)
    except (WikiApiError, httpx.HTTPError) as error:
        logger.warning("Could not enrich %r: %s", title, error)
        await asyncio.to_thread(record_error, conn, title, str(error))
        return PageStatus.ERROR
    await asyncio.to_thread(upsert_page, conn, info)
    logger.debug("Enriched %r -> %r (%s)", title, info.title, info.status)
    return info.status


async def enrich_titles(enricher: Enricher, conn: psycopg.Connection, titles: list[str]) -> Counter:
    """Enrich titles concurrently; the client's semaphore bounds the requests in flight."""
    statuses = await asyncio.gather(*(enrich_one(enricher, conn, t) for t in titles))
    return Counter(status.value for status in statuses)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"{value!r}: dates are UTC; add +00:00 or use a date")
    return parsed


def parse_day_or_time(value: str) -> datetime:
    """`2026-11-06` (midnight UTC) or a full ISO timestamp with a time zone."""
    if len(value) == 10:
        value += "T00:00:00+00:00"
    return parse_time(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", default="live", help="whose spikes to enrich")
    parser.add_argument("--since", type=parse_day_or_time, help="only spikes started at/after")
    parser.add_argument("--until", type=parse_day_or_time, help="only spikes started before")
    parser.add_argument("--once", action="store_true", help="enrich what is pending, then exit")
    parser.add_argument("--force", action="store_true", help="enrich fresh pages again too")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace, settings: Settings, stop: asyncio.Event) -> RunReport:
    """Poll for pending spike titles and enrich them until `stop` (or once)."""
    ttl = timedelta(hours=settings.enrich_cache_ttl_h)
    report = RunReport()
    started = time.monotonic()
    with (
        psycopg.connect(settings.postgres_dsn, autocommit=True) as conn,
        psycopg.connect(settings.postgres_dsn, autocommit=True) as cache_conn,
    ):
        cache = PostgresCache(cache_conn)
        logger.info("Purged %d expired cache entries", cache.purge_older_than(ttl))
        client = WikimediaClient(
            settings.user_agent,
            cache,
            concurrency=settings.wiki_api_concurrency,
            ttl=ttl,
            min_interval_s=settings.enrich_min_interval_s,
        )
        report.client = client.stats
        enricher = Enricher(
            client, settings.wiki_api_url, settings.wiki_rest_url, settings.enrich_max_links
        )
        async with client:
            while not stop.is_set():
                titles = await asyncio.to_thread(
                    pending_titles,
                    conn,
                    args.run_id,
                    ttl,
                    ERROR_RETRY,
                    args.since,
                    args.until,
                    args.force,
                )
                if titles:
                    logger.info("Enriching %d page(s) of run %s", len(titles), args.run_id)
                    report.statuses += await enrich_titles(enricher, conn, titles)
                    report.pages += len(titles)
                    report.seconds = time.monotonic() - started
                    logger.info("So far: %s", report.summary())
                if args.once or await wait_or_stop(stop, settings.enrich_poll_interval_s):
                    break
                args.force = False  # --force applies to the first round only
    report.seconds = time.monotonic() - started
    return report


async def amain(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request
    stop = asyncio.Event()
    install_stop_handlers(stop)
    report = await run(args, settings, stop)
    print(report.summary())
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(amain(argv))


if __name__ == "__main__":
    sys.exit(main())
