"""Crash test for the live consumer: kill it, restart it, and check nothing was lost or doubled.

Usage: uv run python scripts/crash_test.py [--scale 1.0]

Timeline (minutes, multiplied by --scale):
  0:00  start the recorder and the consumer, both from the same `since` timestamp
  2:00  kill the consumer hard (no chance to clean up, like a crash or power cut)
  3:00  restart the consumer; it resumes from its checkpoint with Last-Event-ID
  5:00  stop the consumer gracefully; the recorder runs a little longer, then stops

Then it compares the event ids in Redis with the filtered recording: every kept event
the recorder saw (up to the consumer's last one) must be in Redis exactly once.

It uses its own Redis keys (`wiki:crashtest:*`) and a recording in data/recordings/,
so it doesn't disturb a running live consumer. It talks to Wikimedia: two connections
at once (recorder and consumer) for about five minutes.
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import redis

from wikipulse.config import get_settings
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.filters import is_wanted
from wikipulse.stream.sources import parse_recording_line

logger = logging.getLogger("crash_test")

NAMESPACE = "wiki:crashtest"
STREAM_KEY = f"{NAMESPACE}:edits"
KILL_AT_MIN, RESTART_AT_MIN, STOP_AT_MIN = 2.0, 3.0, 5.0
RECORDER_EXTRA_MIN = 0.5  # the recorder outlives the consumer, so it saw everything it saw


@dataclass
class Result:
    expected: int
    in_redis: int
    missing: set[str]
    extra: set[str]
    duplicates: int
    during_downtime: int
    during_downtime_in_redis: int

    @property
    def passed(self) -> bool:
        return not self.missing and not self.extra and self.duplicates == 0 and self.expected > 0


def python_module(module: str, *args: str) -> list[str]:
    """Run modules with this interpreter directly (not via `uv run`), so kill() hits it."""
    return [sys.executable, "-m", module, *args]


def start(command: list[str], log_path: Path) -> subprocess.Popen:
    log = log_path.open("a", encoding="utf-8")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)


def stop_gracefully(process: subprocess.Popen, timeout: float = 30) -> None:
    """The equivalent of pressing Ctrl+C in the process's console."""
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)  # arrives as SIGBREAK
    else:
        process.send_signal(signal.SIGINT)
    process.wait(timeout=timeout)


def clear_keys(client: redis.Redis) -> None:
    for key in client.scan_iter(match=f"{NAMESPACE}:*", count=1000):
        client.delete(key)


def sleep_until(started: float, minutes: float) -> None:
    time.sleep(max(0.0, started + minutes * 60 - time.monotonic()))


def recording_events(path: Path) -> list[tuple[str, datetime]]:
    """(event_id, received_at) of each kept event in the recording, in stream order."""
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        _, data = parse_recording_line(line)
        raw = json.loads(data)
        if is_wanted(raw):
            received = datetime.fromisoformat(json.loads(line)["received_at"])
            events.append((raw["meta"]["id"], received))
    return events


def compare(
    recorded: list[tuple[str, datetime]],
    redis_ids: list[str],
    downtime: tuple[datetime, datetime],
) -> Result:
    position = {event_id: i for i, (event_id, _) in enumerate(recorded)}
    found = [position[e] for e in redis_ids if e in position]
    last = max(found) if found else -1
    expected = {event_id for event_id, _ in recorded[: last + 1]}
    actual = set(redis_ids)
    down = {e for e, received in recorded[: last + 1] if downtime[0] <= received <= downtime[1]}
    return Result(
        expected=len(expected),
        in_redis=len(redis_ids),
        missing=expected - actual,
        extra=actual - {event_id for event_id, _ in recorded},
        duplicates=len(redis_ids) - len(actual),
        during_downtime=len(down),
        during_downtime_in_redis=len(down & actual),
    )


def redis_event_ids(client: redis.Redis) -> list[str]:
    return [json.loads(f["data"])["event_id"] for _, f in client.xrange(STREAM_KEY)]


def run(scale: float) -> Result:
    settings = get_settings()
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    clear_keys(client)
    begin = datetime.now(UTC)
    since = (begin - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_dir = settings.data_dir / "recordings"
    out_dir.mkdir(parents=True, exist_ok=True)
    recording = out_dir / f"crashtest-{begin:%Y%m%dT%H%M%SZ}.jsonl"
    recorder_log = recording.with_suffix(".recorder.log")
    consumer_log = recording.with_suffix(".consumer.log")
    record_minutes = (STOP_AT_MIN + RECORDER_EXTRA_MIN) * scale
    consumer_cmd = python_module(
        "wikipulse.stream.consume", "--since", since, "--stream-key", STREAM_KEY,
        "--namespace", NAMESPACE,
    )  # fmt: skip
    started = time.monotonic()
    recorder = start(
        python_module(
            "wikipulse.stream.record", "--minutes", f"{record_minutes:.3f}", "--since", since,
            "--output", str(recording),
        ),
        recorder_log,
    )  # fmt: skip
    consumer = start(consumer_cmd, consumer_log)
    logger.info("Recorder and consumer started (since %s); logs next to %s", since, recording)

    sleep_until(started, KILL_AT_MIN * scale)
    consumer.kill()  # hard kill: TerminateProcess on Windows, SIGKILL on Linux
    consumer.wait()
    killed_at = datetime.now(UTC)
    logger.info("Consumer killed; %d events in Redis so far", client.xlen(STREAM_KEY))

    sleep_until(started, RESTART_AT_MIN * scale)
    restarted_at = datetime.now(UTC)
    consumer = start(consumer_cmd, consumer_log)
    logger.info(
        "Consumer restarted; it resumes from checkpoint %s", client.get(f"{NAMESPACE}:last_id")
    )

    sleep_until(started, STOP_AT_MIN * scale)
    stop_gracefully(consumer)
    logger.info("Consumer stopped gracefully (exit code %s)", consumer.returncode)
    recorder.wait(timeout=record_minutes * 60 + 120)
    logger.info("Recorder finished (exit code %s): %s", recorder.returncode, recording)

    result = compare(
        recording_events(recording), redis_event_ids(client), (killed_at, restarted_at)
    )
    logger.info("Consumer stats: %s", client.hgetall(f"{NAMESPACE}:stats"))
    client.close()
    return result


def report(result: Result) -> None:
    print(f"Kept events in the recording (up to the consumer's last): {result.expected}")
    print(f"Entries in Redis:                                         {result.in_redis}")
    print(f"Missing from Redis:                                       {len(result.missing)}")
    print(f"In Redis but not in the recording:                        {len(result.extra)}")
    print(f"Duplicate entries in Redis:                               {result.duplicates}")
    print(
        f"Events that arrived while the consumer was dead:          {result.during_downtime}"
        f" ({result.during_downtime_in_redis} of them recovered after the restart)"
    )
    print("PASS" if result.passed else "FAIL")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scale", type=float, default=1.0, help="multiply all durations (e.g. 0.2 for a dry run)"
    )
    args = parser.parse_args()
    setup_logging()
    result = run(args.scale)
    report(result)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
