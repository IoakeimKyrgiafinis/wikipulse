"""Event feed: `uv run python -m wikipulse.events.feed [--run-id live] [--all]`.

Prints the active events of a grouping run with their pages, biggest first, and a
summary line (events, pages per event). `--all` includes ended events (a replayed day
has mostly ended events); `--min-pages 2` hides single-page events.
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
from wikipulse.db.events import FeedEvent, feed_events
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


def stats_line(events: list[FeedEvent]) -> str:
    """Number of events and pages per event."""
    sizes = [len(e.pages) for e in events]
    if not sizes:
        return "0 events."
    histogram = ", ".join(f"{k}: {v}" for k, v in size_histogram(sizes).items())
    statuses = Counter(e.status for e in events)
    return (
        f"{len(events)} events ({', '.join(f'{v} {k}' for k, v in sorted(statuses.items()))}), "
        f"{sum(sizes)} pages. Pages per event: mean {mean(sizes):.2f}, median "
        f"{median(sizes):g}, max {max(sizes)}. Events by size: {histogram}."
    )


def render_event(event: FeedEvent, max_pages: int) -> list[str]:
    end = f"{event.ended_at.astimezone(UTC):%m-%d %H:%M}" if event.ended_at else "now"
    lines = [
        f"#{event.number} {event.title}  [{len(event.pages)} page(s), "
        f"{event.started_at.astimezone(UTC):%m-%d %H:%M} to {end}, {event.status}]"
    ]
    for page in event.pages[:max_pages]:
        name = page.canonical_title
        if page.title != page.canonical_title:
            name += f" (spike title: {page.title})"
        lines.append(
            f"    {page.score:6.1f}  {page.started_at.astimezone(UTC):%m-%d %H:%M}"
            f"  joined {page.joined_score:.2f}  {name}"
        )
    if len(event.pages) > max_pages:
        lines.append(f"    ... and {len(event.pages) - max_pages} more")
    return lines


def render(events: list[FeedEvent], run_id: str, limit: int, max_pages: int) -> str:
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
                events = feed_events(conn, args.run_id, args.all, args.since, args.until)
                stats = stats_line(events)
                shown = [e for e in events if len(e.pages) >= args.min_pages]
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
