"""Determinism check for spike detection (spec 02a acceptance criterion).

    uv run python scripts/determinism_check.py --file data/recordings/<recording>.jsonl

Replays the same recording twice, each time into a fresh Redis Stream, runs the detector
on each replay as its own run (`determinism-A`, `determinism-B`), and compares the spikes
stored in Postgres. Run B is stopped half way (`--restart-after`, default: half of the
stream) and restarted from its snapshot, so the check also shows that state survives a
restart. Exit code 0 means identical spikes.

Uses its own stream (`wiki:determinism:edits`) and run ids, so it never touches the live
stream or the live run.
"""

import argparse
import logging
import sys
from pathlib import Path

import psycopg
import redis

from wikipulse.config import get_settings
from wikipulse.db.spikes import spikes_for_run
from wikipulse.detect import run as detect_run
from wikipulse.logging_setup import setup_logging
from wikipulse.stream import replay

logger = logging.getLogger("determinism_check")

STREAM = "wiki:determinism:edits"


def replay_into_fresh_stream(path: Path, client: redis.Redis) -> int:
    client.delete(STREAM)
    replay.main(["--file", str(path), "--stream-key", STREAM])
    return client.xlen(STREAM)


def detect(run_id: str, reset: bool, max_entries: int | None = None) -> int:
    argv = ["--stream-key", STREAM, "--run-id", run_id, "--exit-when-idle", "3"]
    if reset:
        argv.append("--reset")
    if max_entries is not None:
        argv += ["--max-entries", str(max_entries)]
    return detect_run.run(detect_run.parse_args(argv), get_settings())


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay twice and compare detected spikes.")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--restart-after", type=int, help="entries before run B restarts")
    args = parser.parse_args()
    settings = get_settings()
    setup_logging()
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)

    length = replay_into_fresh_stream(args.file, client)
    logger.info("Run A: %d stream entries, uninterrupted", length)
    detect("determinism-A", reset=True)

    length_b = replay_into_fresh_stream(args.file, client)
    half = args.restart_after or length_b // 2
    logger.info("Run B: %d stream entries, restarted after %d", length_b, half)
    first = detect("determinism-B", reset=True, max_entries=half)
    second = detect("determinism-B", reset=False)
    logger.info("Run B processed %d + %d entries", first, second)
    client.delete(STREAM)
    client.close()

    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        spikes_a = spikes_for_run(conn, "determinism-A")
        spikes_b = spikes_for_run(conn, "determinism-B")
    active = sum(s.status == "active" for s in spikes_a)
    print(f"Stream entries: A {length}, B {length_b}")
    print(f"Spikes: A {len(spikes_a)} ({active} still active at the end), B {len(spikes_b)}")
    if spikes_a == spikes_b and length == length_b:
        print("PASS: both replays produced identical spikes (all fields compared)")
        return 0
    only_a = [s for s in spikes_a if s not in spikes_b]
    only_b = [s for s in spikes_b if s not in spikes_a]
    print(f"FAIL: {len(only_a)} spikes only in A, {len(only_b)} only in B")
    for spike in (only_a + only_b)[:10]:
        print(f"  {spike}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
