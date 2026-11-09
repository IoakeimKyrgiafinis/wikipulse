"""Crash test for the lake archiver: kill it hard mid-hour, check for gaps and duplicates.

    uv run python scripts/lake_crash_test.py --recording data/recordings/<file>.jsonl

1. Fills its own Redis stream (`wiki:lakecrash:edits`) with the kept edits of the first
   `--messages` lines of a recording, normalized exactly like the stage 1 consumer, plus a
   few *late* edits at the end (event times in the first hour, after later hours).
2. Reference: archives the stream in-process without any crash (prefix `ref/edits`).
3. Crash run: starts `python -m wikipulse.lake.archive` as a child process (prefix
   `crash/edits`, frequent flushes) and kills it hard (TerminateProcess / SIGKILL, no
   cleanup) once it has read 25%, 50% and 75% of the stream; restarts it after each kill.
   At every kill it reports the unacknowledged edits and whether their hour already had
   a file, i.e. that the kill really happened in the middle of a partly written hour.
4. Checks: every edit of the stream is in the crash-run archive exactly once, in the
   partition of its event-time hour; nothing else is there; no entry is left pending;
   and every hour file equals the reference run's.

Everything lives in the bucket `wikipulse-crashtest` (not the real lake) and in its own
stream and consumer groups; all are reset at the start. Exit code 0 = PASS.
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

import redis
from fill_stream import fill, kept_edits  # scripts/fill_stream.py, next to this file

from wikipulse.config import Settings, get_settings
from wikipulse.lake.archiver import Archiver, parse_entry, reset_group
from wikipulse.lake.partitions import hour_of
from wikipulse.lake.store import LakeStore
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.models import EditEvent

logger = logging.getLogger("lake_crash_test")

STREAM = "wiki:lakecrash:edits"
BUCKET = "wikipulse-crashtest"
REF_GROUP, CRASH_GROUP = "lake:crashtest-ref", "lake:crashtest"
REF_PREFIX, CRASH_PREFIX = "ref/edits", "crash/edits"
KILL_POINTS = (0.25, 0.50, 0.75)
LATE_EDITS = 5
LOG_DIR = Path("data") / "lake-crashtest"


def build_stream(client: redis.Redis, recording: Path, max_messages: int) -> list[EditEvent]:
    """Fill STREAM with the recording's kept edits plus LATE_EDITS late ones at the end."""
    client.delete(STREAM)
    events = list(kept_edits(recording, max_messages))
    if not events:
        raise SystemExit(f"No kept edits in the first {max_messages} lines of {recording}")
    first_hour = min(hour_of(event.event_time) for event in events)
    late = [
        event.model_copy(
            update={
                "event_id": f"late-{i}-{event.event_id}",
                "event_time": first_hour + timedelta(minutes=10 + i),
            }
        )
        for i, event in enumerate(events[:LATE_EDITS])
    ]
    events += late
    fill(client, STREAM, iter(events))
    return events


def arrived_late(events: list[EditEvent]) -> int:
    """Edits that arrived after an edit of a later hour (their hour may be written already)."""
    latest, count = None, 0
    for event in events:
        hour = hour_of(event.event_time)
        if latest is not None and hour < latest:
            count += 1
        latest = hour if latest is None else max(latest, hour)
    return count


def entries_read(client: redis.Redis, group: str) -> int:
    for info in client.xinfo_groups(STREAM):
        if info["name"] == group:
            return int(info.get("entries-read") or 0)
    return 0


def archive_command(buffer_rows: int, idle_exit: float | None = None) -> list[str]:
    """The archiver CLI with this interpreter directly (not via `uv run`), so kill() hits it."""
    command = [sys.executable, "-m", "wikipulse.lake.archive", "--stream-key", STREAM]
    command += ["--group", CRASH_GROUP, "--prefix", CRASH_PREFIX]
    command += ["--flush-interval", "1", "--max-buffer-rows", str(buffer_rows)]
    if idle_exit is not None:
        command += ["--exit-when-idle", str(idle_exit)]
    return command


def start(command: list[str], log_name: str) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = (LOG_DIR / log_name).open("w", encoding="utf-8")
    env = os.environ | {"S3_BUCKET": BUCKET}
    return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)


def pending_hours(client: redis.Redis) -> Counter:
    """How many unacknowledged edits of the crash group fall into each hour."""
    pending = client.xpending_range(STREAM, CRASH_GROUP, "-", "+", count=1_000_000)
    hours: Counter = Counter()
    for item in pending:
        entry_id = item["message_id"]
        entries = client.xrange(STREAM, entry_id, entry_id)
        event = parse_entry(entry_id, entries[0][1]) if entries else None
        if event is not None:
            hours[hour_of(event.event_time)] += 1
    return hours


def crash_at(client: redis.Redis, store: LakeStore, fraction: float, total: int, rows: int) -> bool:
    """Run the archiver until it has read `fraction` of the stream, then kill it hard.

    Returns True if the kill left unwritten edits of an hour that already had a file
    (a crash in the middle of a partly archived hour).
    """
    target = int(total * fraction)
    process = start(archive_command(rows), f"crash-{int(fraction * 100)}.log")
    while entries_read(client, CRASH_GROUP) < target and process.poll() is None:
        time.sleep(0.005)
    process.kill()  # hard kill: TerminateProcess on Windows, SIGKILL on Linux
    process.wait(timeout=30)
    hours = pending_hours(client)
    written = set(store.hours())
    mid_hour = [hour for hour in hours if hour in written]
    logger.info(
        "Killed at %d of %d entries read: %d edits unacknowledged in %d hours, %d of those "
        "hours already had a file (%s)",
        entries_read(client, CRASH_GROUP),
        total,
        sum(hours.values()),
        len(hours),
        len(mid_hour),
        ", ".join(f"{hour:%d %H}:00 ({hours[hour]})" for hour in sorted(mid_hour)) or "-",
    )
    return bool(mid_hour)


def verify(client: redis.Redis, events: list[EditEvent], crash: LakeStore, ref: LakeStore) -> bool:
    """Compare the crash-run archive with the stream and with the reference archive."""
    expected = {event.event_id: hour_of(event.event_time) for event in events}
    counts: Counter = Counter()
    wrong_hour = 0
    for hour in crash.hours():
        table = crash.read_hour(hour)
        ids = table.column("event_id").to_pylist()
        for event_id, moment in zip(ids, table.column("event_time").to_pylist(), strict=True):
            counts[event_id] += 1
            wrong_hour += hour_of(moment) != hour
    missing = set(expected) - set(counts)
    extra = set(counts) - set(expected)
    duplicates = sum(n - 1 for n in counts.values() if n > 1)
    late_ok = all(counts[e] == 1 for e in expected if e.startswith("late-"))
    pending = client.xpending(STREAM, CRASH_GROUP)["pending"]
    same_hours = crash.hours() == ref.hours()
    same_files = same_hours and all(
        crash.read_hour(hour).equals(ref.read_hour(hour)) for hour in ref.hours()
    )
    logger.info(
        "Stream: %d edits in %d hours. Archive: %d rows. Missing %d, extra %d, duplicates %d, "
        "in the wrong hour %d, late edits archived %s, still pending %d, "
        "identical to the reference run %s",
        len(expected),
        len(set(expected.values())),
        sum(counts.values()),
        len(missing),
        len(extra),
        duplicates,
        wrong_hour,
        late_ok,
        pending,
        same_files,
    )
    return not (missing or extra or duplicates or wrong_hour or pending) and late_ok and same_files


def run(settings: Settings, recording: Path, max_messages: int, buffer_rows: int) -> bool:
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        for group in (REF_GROUP, CRASH_GROUP):
            reset_group(client, STREAM, group)
        events = build_stream(client, recording, max_messages)
        total = len(events)
        logger.info(
            "Stream %s: %d edits from %d recording lines; %d arrived after a later hour "
            "(%d of them added on purpose)",
            STREAM,
            total,
            max_messages,
            arrived_late(events),
            LATE_EDITS,
        )
        ref = LakeStore.from_settings(settings, REF_PREFIX)
        crash = LakeStore.from_settings(settings, CRASH_PREFIX)
        ref.fs.delete_dir_contents(BUCKET, missing_dir_ok=True)

        reference = Archiver(client, STREAM, REF_GROUP, ref, flush_interval_s=1e9, block_ms=100)
        reference.ensure_group("0")
        reference.run(idle_exit_s=0.5)
        logger.info("Reference run: %d hour files", len(ref.hours()))

        Archiver(client, STREAM, CRASH_GROUP, crash).ensure_group("0")
        mid_hour = [crash_at(client, crash, f, total, buffer_rows) for f in KILL_POINTS]
        final = start(archive_command(buffer_rows, idle_exit=2), "final.log")
        final.wait(timeout=600)
        logger.info("Restarted archiver finished with exit code %s", final.returncode)
        passed = verify(client, events, crash, ref) and final.returncode == 0
        if not any(mid_hour):
            logger.warning("No kill landed in a partly archived hour; try other --buffer-rows")
            passed = False
        return passed
    finally:
        client.delete(STREAM)
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recording", type=Path, required=True, help="a stage 1 recording")
    parser.add_argument(
        "--messages", type=int, default=1_000_000, help="recording lines to use (default 1000000)"
    )
    parser.add_argument(
        "--buffer-rows",
        type=int,
        default=1500,
        help="the crashing archiver writes every this many edits (default 1500)",
    )
    args = parser.parse_args()
    setup_logging()
    settings = get_settings().model_copy(update={"s3_bucket": BUCKET})
    passed = run(settings, args.recording, args.messages, args.buffer_rows)
    logger.info("Lake crash test: %s", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
