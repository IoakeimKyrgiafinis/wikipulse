"""Statistics: `uv run python -m wikipulse.stream.stats [--watch]`.

Prints the consumer's counters, the Redis Stream length, the rate of kept events over
the last minute, the share of bot edits, and the lag (now minus the newest event time).
`--watch` refreshes every 2 seconds and also shows the rate of all incoming messages.
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import redis

from wikipulse.config import get_settings
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.consumer import LIVE_NAMESPACE, RedisKeys

RATE_WINDOW_S = 60
WATCH_INTERVAL_S = 2.0
MAX_ENTRIES_SCANNED = 50_000  # cap for the last-minute scan (only reached during fast replays)
COUNTERS = ("received", "kept", "discarded", "duplicates", "malformed", "reconnects")


@dataclass
class Snapshot:
    """Everything `stats` shows, read from Redis at one moment."""

    taken_at: float  # Unix time
    counters: dict[str, int]
    kept_bots: int
    last_event_time: str | None
    stream_length: int
    recent_kept: int  # entries added in the last RATE_WINDOW_S seconds
    recent_bots: int
    newest_event_time: datetime | None


def recent_entries(client: redis.Redis, stream: str, now: float) -> list[tuple[str, dict]]:
    """Stream entries added in the last minute. Entry ids start with their insertion time."""
    start_ms = int((now - RATE_WINDOW_S) * 1000)
    return client.xrange(stream, min=f"{start_ms}-0", max="+", count=MAX_ENTRIES_SCANNED)


def is_bot_entry(fields: dict) -> bool:
    try:
        return bool(json.loads(fields["data"]).get("is_bot"))
    except (KeyError, ValueError, AttributeError):
        return False


def take_snapshot(client: redis.Redis, keys: RedisKeys) -> Snapshot:
    """Read the stats hash and the stream; compute the last-minute figures."""
    now = time.time()
    stats = client.hgetall(keys.stats)
    entries = recent_entries(client, keys.stream, now)
    newest = client.xrevrange(keys.stream, count=1)
    newest_time = datetime.fromisoformat(newest[0][1]["event_time"]) if newest else None
    return Snapshot(
        taken_at=now,
        counters={name: int(stats.get(name, 0)) for name in COUNTERS},
        kept_bots=int(stats.get("kept_bots", 0)),
        last_event_time=stats.get("last_event_time"),
        stream_length=client.xlen(keys.stream),
        recent_kept=len(entries),
        recent_bots=sum(is_bot_entry(fields) for _, fields in entries),
        newest_event_time=newest_time,
    )


def percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def render(snap: Snapshot, keys: RedisKeys, previous: Snapshot | None = None) -> str:
    """Format a snapshot as text. With `previous`, include rates since that snapshot."""
    c = snap.counters
    lines = [
        f"WikiPulse stream stats at {datetime.fromtimestamp(snap.taken_at, UTC):%H:%M:%S} UTC"
        f"  ({keys.stats}, stream {keys.stream})",
        f"  received     {c['received']:>10}",
        f"  kept         {c['kept']:>10}   bots {percent(snap.kept_bots, c['kept'])}",
        f"  discarded    {c['discarded']:>10}",
        f"  duplicates   {c['duplicates']:>10}",
        f"  malformed    {c['malformed']:>10}",
        f"  reconnects   {c['reconnects']:>10}",
        f"  stream length {snap.stream_length:>9}",
        f"  kept events/s, last {RATE_WINDOW_S} s: {snap.recent_kept / RATE_WINDOW_S:.2f}"
        f"   (bots {percent(snap.recent_bots, snap.recent_kept)})",
    ]
    if previous is not None:
        elapsed = snap.taken_at - previous.taken_at
        received = c["received"] - previous.counters["received"]
        kept = c["kept"] - previous.counters["kept"]
        lines.append(
            f"  since last refresh: {received / elapsed:.1f} messages/s received, "
            f"{kept / elapsed:.2f} kept/s"
        )
    lines.append(f"  last event time: {snap.last_event_time or 'n/a'}")
    if snap.newest_event_time is not None:
        lag = snap.taken_at - snap.newest_event_time.timestamp()
        lines.append(f"  lag (now - newest event_time): {lag:.1f} s")
    else:
        lines.append("  lag: n/a (stream is empty)")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--watch", action="store_true", help="refresh every 2 seconds")
    parser.add_argument("--stream-key", help="Redis Stream to inspect (default: STREAM_KEY)")
    parser.add_argument(
        "--namespace",
        default=LIVE_NAMESPACE,
        help=f"consumer key prefix, e.g. wiki:replay (default: {LIVE_NAMESPACE})",
    )
    return parser.parse_args(argv)


def watch(client: redis.Redis, keys: RedisKeys) -> None:
    previous: Snapshot | None = None
    clear = "\x1b[2J\x1b[H" if sys.stdout.isatty() else "\n"
    while True:
        snap = take_snapshot(client, keys)
        print(clear + render(snap, keys, previous), flush=True)
        previous = snap
        time.sleep(WATCH_INTERVAL_S)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    keys = RedisKeys.for_namespace(args.stream_key or settings.stream_key, args.namespace)
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        if args.watch:
            watch(client, keys)
        else:
            print(render(take_snapshot(client, keys), keys))
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
