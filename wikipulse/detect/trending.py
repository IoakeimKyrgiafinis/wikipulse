"""Trending: `uv run python -m wikipulse.detect.trending [--run-id live] [--limit 20]`.

Prints the currently active spikes of a detector run from Postgres, highest score first.
The detector writes to Postgres at every snapshot (every minute by default), so the list
can be up to a minute behind the detector. `--all` also lists recently ended spikes.
"""

import argparse
import io
import sys
import time
from datetime import UTC, datetime

import psycopg

from wikipulse.config import get_settings
from wikipulse.db.spikes import active_spikes, latest_edit_time, spikes_for_run
from wikipulse.detect.detector import SpikeRecord
from wikipulse.logging_setup import setup_logging

HEADER = (
    f"{'score':>6} {'edits':>5} {'editors':>7} {'peak/10m':>8} {'reverts':>7} "
    f"{'started (UTC)':>13} {'duration':>8}  title"
)


def duration_text(spike: SpikeRecord, now: datetime) -> str:
    end = spike.ended_at or now
    minutes = int((end - spike.first_edit_at).total_seconds() // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m"


def render(spikes: list[SpikeRecord], run_id: str, latest: datetime | None) -> str:
    """A text table of spikes. `latest` is the run's newest edit time (its "now")."""
    now = latest or datetime.now(UTC)
    lines = [
        f"Spikes of run '{run_id}', detector time {latest:%Y-%m-%d %H:%M:%S} UTC"
        if latest
        else f"Spikes of run '{run_id}' (none yet)",
        HEADER,
    ]
    for s in spikes:
        status = "" if s.status == "active" else "  (ended)"
        lines.append(
            f"{s.score:6.1f} {s.total_edits:5d} {s.distinct_editors:7d} {s.peak_edits_10m:8d} "
            f"{s.revert_edits:7d} {s.started_at.astimezone(UTC):%m-%d %H:%M}"
            f" {duration_text(s, now):>8}  {s.title}{status}"
        )
    if not spikes:
        lines.append("  (no active spikes)")
    return "\n".join(lines)


def load(conn: psycopg.Connection, run_id: str, limit: int, show_all: bool) -> list[SpikeRecord]:
    if not show_all:
        return active_spikes(conn, run_id, limit)
    spikes = spikes_for_run(conn, run_id)
    spikes.sort(key=lambda s: (s.status != "active", -s.score))
    return spikes[:limit]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", default="live", help="detector run to show (default: live)")
    parser.add_argument("--limit", type=int, default=20, help="rows to show (default 20)")
    parser.add_argument("--all", action="store_true", help="include ended spikes")
    parser.add_argument("--watch", type=float, metavar="SECONDS", help="refresh periodically")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Titles can hold any Unicode character; on Windows, output redirected to a file or
    # pipe uses a legacy code page. Replace what it can't show instead of crashing.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    settings = get_settings()
    setup_logging()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        try:
            while True:
                spikes = load(conn, args.run_id, args.limit, args.all)
                print(render(spikes, args.run_id, latest_edit_time(conn, args.run_id)), flush=True)
                if not args.watch:
                    break
                time.sleep(args.watch)
                print()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
