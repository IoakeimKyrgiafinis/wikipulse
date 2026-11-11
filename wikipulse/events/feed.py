"""Event feed: `uv run python -m wikipulse.events.feed [--run-id live] [--all]`.

Prints the active events of a grouping run with their pages, biggest first, and a
summary line (events, pages per event). A page that spiked more than once in an event is
one page, listed once with its number of spikes. `--all` includes ended events (a replayed day
has mostly ended events); `--min-pages 2` hides single-page events.

Each page shows the news classifier's highest score for its spikes (`news 0.87`, spec
07c), or `news  -  ` until `wikipulse.ml.serve` has scored them; the event's header shows
the highest score of all its pages.
"""

import argparse
import io
import sys
import time
from collections import Counter
from datetime import UTC
from statistics import mean, median

import psycopg

from wikipulse.config import get_settings
from wikipulse.db.events import FeedEvent, FeedPage, feed_events
from wikipulse.enrich.run import parse_day_or_time
from wikipulse.logging_setup import setup_logging

SIZE_BUCKETS = ((1, 1), (2, 2), (3, 5), (6, 10), (11, None))


def size_histogram(sizes: list[int]) -> dict[str, int]:
    """How many events have 1, 2, 3-5, 6-10 and 11+ pages."""
    histogram: dict[str, int] = {}
    for low, high in SIZE_BUCKETS:
        name = str(low) if low == high else (f"{low}-{high}" if high else f"{low}+")
        histogram[name] = sum(1 for s in sizes if s >= low and (high is None or s <= high))
    return histogram


def distinct_pages(event: FeedEvent) -> list[tuple[FeedPage, int]]:
    """The event's pages, each once: (its best spike, number of spikes), best first.

    Spikes come ordered by score, so the first spike seen of a page is its best one.
    """
    pages: dict[str, tuple[FeedPage, int]] = {}
    for spike in event.pages:
        best, count = pages.get(spike.canonical_title, (spike, 0))
        pages[spike.canonical_title] = (best, count + 1)
    return list(pages.values())


def page_count(event: FeedEvent) -> int:
    """Distinct pages in the event (a page that spiked twice counts once)."""
    return len({spike.canonical_title for spike in event.pages})


def by_size(events: list[FeedEvent]) -> list[FeedEvent]:
    """Biggest first (distinct pages), then by score, then oldest number."""
    return sorted(events, key=lambda e: (-page_count(e), -e.score, e.number))


def stats_line(events: list[FeedEvent]) -> str:
    """Number of events and distinct pages per event."""
    sizes = [page_count(e) for e in events]
    if not sizes:
        return "0 events."
    histogram = ", ".join(f"{k}: {v}" for k, v in size_histogram(sizes).items())
    statuses = Counter(e.status for e in events)
    spikes = sum(len(e.pages) for e in events)
    return (
        f"{len(events)} events ({', '.join(f'{v} {k}' for k, v in sorted(statuses.items()))}), "
        f"{sum(sizes)} pages ({spikes} spikes). Pages per event: mean {mean(sizes):.2f}, "
        f"median {median(sizes):g}, max {max(sizes)}. Events by size: {histogram}."
    )


def news_score(pages: list[FeedPage]) -> float | None:
    """The highest news classifier score among these spikes (None: none scored yet)."""
    scores = [p.news_score for p in pages if p.news_score is not None]
    return max(scores) if scores else None


def news_text(score: float | None) -> str:
    """'news 0.87' for a scored spike, 'news  -  ' while it isn't scored (spec 07c)."""
    return "news  -  " if score is None else f"news {score:.2f}"


def render_event(event: FeedEvent, max_pages: int) -> list[str]:
    """One event of the feed as text lines."""
    end = f"{event.ended_at.astimezone(UTC):%m-%d %H:%M}" if event.ended_at else "now"
    pages = distinct_pages(event)
    spikes = f", {len(event.pages)} spikes" if len(event.pages) != len(pages) else ""
    best = news_score(list(event.pages))
    lines = [
        f"#{event.number} {event.title}  [{len(pages)} page(s){spikes}, "
        f"{event.started_at.astimezone(UTC):%m-%d %H:%M} to {end}, {event.status}"
        f"{', ' + news_text(best) if best is not None else ''}]"
    ]
    for page, count in pages[:max_pages]:
        name = page.canonical_title
        if page.title != page.canonical_title:
            name += f" (spike title: {page.title})"
        if count > 1:
            name += f" ({count} spikes)"
        same_page = [p for p in event.pages if p.canonical_title == page.canonical_title]
        lines.append(
            f"    {page.score:6.1f}  {page.started_at.astimezone(UTC):%m-%d %H:%M}"
            f"  joined {page.joined_score:.2f}  {news_text(news_score(same_page))}  {name}"
        )
    if len(pages) > max_pages:
        lines.append(f"    ... and {len(pages) - max_pages} more")
    return lines


def render(events: list[FeedEvent], run_id: str, limit: int, max_pages: int) -> str:
    """The feed of a grouping run as text."""
    lines = [f"Events of grouping run '{run_id}'", stats_line(events), ""]
    for event in events[:limit]:
        lines.extend(render_event(event, max_pages))
    if not events:
        lines.append("  (no events)")
    elif len(events) > limit:
        lines.append(f"... and {len(events) - limit} more events (--limit)")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", default="live", help="grouping run to show (default: live)")
    parser.add_argument("--all", action="store_true", help="include ended events")
    parser.add_argument("--since", type=parse_day_or_time, help="events started at/after")
    parser.add_argument("--until", type=parse_day_or_time, help="events started before")
    parser.add_argument("--min-pages", type=int, default=1, help="hide smaller events")
    parser.add_argument("--limit", type=int, default=20, help="events to show (default 20)")
    parser.add_argument("--max-pages", type=int, default=10, help="pages shown per event")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="refresh periodically")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Titles can hold any Unicode character; Windows consoles may not. Replace, don't crash.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    settings = get_settings()
    setup_logging()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        try:
            while True:
                events = by_size(feed_events(conn, args.run_id, args.all, args.since, args.until))
                stats = stats_line(events)
                shown = [e for e in events if page_count(e) >= args.min_pages]
                text = render(shown, args.run_id, args.limit, args.max_pages)
                if args.min_pages > 1:
                    text += f"\n\nAll events, including smaller ones: {stats}"
                print(text, flush=True)
                if not args.watch:
                    break
                time.sleep(args.watch)
                print()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
