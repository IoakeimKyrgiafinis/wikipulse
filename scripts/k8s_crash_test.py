"""Pod-kill test for the consumer in the cluster: no event lost or doubled (spec 08a).

Usage: uv run python scripts/k8s_crash_test.py [--scale 1.0]

The logic of scripts/crash_test.py, with Kubernetes doing the restarts, and with only
ONE connection to Wikimedia at any time (crash_test.py records the stream next to the
consumer; here the reference is read afterwards, from the same position):

  0:00  note where the running consumer is: its checkpoint (the SSE id of its last
        message) and the last entry it wrote to the Redis Stream
  2:00  delete the consumer pod hard (`--grace-period=0 --force`: no clean-up, like a
        crash); the Deployment starts a new pod, which resumes from the checkpoint
  4:00  delete the new pod normally (SIGTERM, a graceful stop); again a new pod
  6:00  scale the consumer to 0 (it stops gracefully), note its final checkpoint
  then  read the stream again from the first checkpoint (Last-Event-ID), until the final
        one, and check that every kept edit is in Redis exactly once
  last  scale the consumer back to 1 (also when something failed)

Times are multiplied by --scale. Run it while the cluster's consumer is running
(`scripts/cluster.py up`); it uses kubectl's context `kind-wikipulse` and Redis at
REDIS_URL (the Compose Redis, the same one the cluster uses).
"""

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import redis

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crash_test import compare, report  # noqa: E402  (scripts/crash_test.py)

from wikipulse.config import get_settings  # noqa: E402
from wikipulse.logging_setup import setup_logging  # noqa: E402
from wikipulse.stream.filters import is_wanted  # noqa: E402
from wikipulse.stream.sources import LiveSource  # noqa: E402

logger = logging.getLogger("k8s_crash_test")

CONTEXT = "kind-wikipulse"
NAMESPACE = "wikipulse"
SELECTOR = "app.kubernetes.io/name=consumer"
CONSUMER_NAMESPACE = "wiki:k8s:consumer"  # --namespace of the consumer in k8s/consumer.yaml
HARD_KILL_AT_MIN, SOFT_KILL_AT_MIN, STOP_AT_MIN = 2.0, 4.0, 6.0
BEFORE_START = 2000  # Redis entries before the start excluded from the reference
REFERENCE_MARGIN = timedelta(seconds=60)  # read this far past the consumer's last edit
REFERENCE_TIMEOUT_S = 900


def kubectl(*args: str, check: bool = True) -> str:
    command = ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, *args]
    logger.info("$ %s", " ".join(command[3:]))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"kubectl failed: {result.stderr.strip()}")
    return result.stdout.strip()


def consumer_pods() -> list[str]:
    return kubectl("get", "pods", "-l", SELECTOR, "-o", "name").split()


def wait_until_ready() -> datetime:
    kubectl("rollout", "status", "deployment/consumer", "--timeout=300s")
    kubectl("wait", "--for=condition=Ready", "pod", "-l", SELECTOR, "--timeout=300s")
    return datetime.now(UTC)


@dataclass(frozen=True)
class Position:
    checkpoint: str  # SSE id of the consumer's last message
    entry_id: str  # its last Redis Stream entry


def position(client: redis.Redis, stream: str) -> Position:
    """Checkpoint and last entry, read in one transaction (the consumer's Lua script
    moves both at once, so they always belong together)."""
    with client.pipeline(transaction=True) as pipe:
        pipe.get(f"{CONSUMER_NAMESPACE}:last_id")
        pipe.xrevrange(stream, count=1)
        checkpoint, last = pipe.execute()
    if not checkpoint or not last:
        raise SystemExit("The consumer has no checkpoint yet: is it running?")
    return Position(checkpoint, last[0][0])


def entries_after(client: redis.Redis, stream: str, entry_id: str) -> list[tuple[str, datetime]]:
    """(event_id, event_time) of every entry written after `entry_id`."""
    result = []
    for _, fields in client.xrange(stream, min=f"({entry_id}"):
        data = json.loads(fields["data"])
        result.append((data["event_id"], datetime.fromisoformat(data["event_time"])))
    return result


def ids_up_to(client: redis.Redis, stream: str, entry_id: str) -> set[str]:
    """Event ids of the last entries up to `entry_id`: written before the test started."""
    entries = client.xrevrange(stream, max=entry_id, count=BEFORE_START)
    return {json.loads(fields["data"])["event_id"] for _, fields in entries}


async def read_reference(start: str, end: str, until: datetime) -> list[tuple[str, datetime]]:
    """Kept events of the live stream from checkpoint `start` on, up to checkpoint `end`
    (or past event time `until`). The consumer is stopped: this is the only connection."""
    settings = get_settings()
    source = LiveSource(settings.stream_url, settings.user_agent)
    events: list[tuple[str, datetime]] = []
    messages = 0
    async with asyncio.timeout(REFERENCE_TIMEOUT_S):
        async for sse_id, data in source.messages(start):
            messages += 1
            raw = json.loads(data)
            meta = raw.get("meta", {}) if isinstance(raw, dict) else {}
            if isinstance(raw, dict) and is_wanted(raw):
                events.append((meta["id"], datetime.fromisoformat(meta["dt"])))
            if sse_id == end:
                logger.info("Reached the final checkpoint after %d messages", messages)
                break
            if meta.get("dt") and datetime.fromisoformat(meta["dt"]) > until:
                logger.info("Passed the consumer's last event time after %d messages", messages)
                break
    return events


def in_window(events: list[tuple[str, datetime]], window: tuple[datetime, datetime]) -> set[str]:
    return {event_id for event_id, at in events if window[0] <= at <= window[1]}


def run(scale: float) -> int:
    settings = get_settings()
    stream = settings.stream_key
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    started = time.monotonic()

    def sleep_until(minutes: float) -> None:
        time.sleep(max(0.0, started + minutes * 60 * scale - time.monotonic()))

    wait_until_ready()
    start = position(client, stream)
    logger.info("Start: checkpoint %s, entry %s", start.checkpoint, start.entry_id)
    try:
        sleep_until(HARD_KILL_AT_MIN)
        pods = consumer_pods()
        kubectl("delete", *pods, "--grace-period=0", "--force", "--wait=false")
        hard = (datetime.now(UTC), wait_until_ready())
        logger.info("Hard kill: %s replaced in %.0f s", pods, (hard[1] - hard[0]).total_seconds())

        sleep_until(SOFT_KILL_AT_MIN)
        pods = consumer_pods()
        killed = datetime.now(UTC)
        kubectl("delete", *pods)  # SIGTERM, waits for the pod to stop
        soft = (killed, wait_until_ready())
        logger.info("Graceful delete: replaced in %.0f s", (soft[1] - soft[0]).total_seconds())

        sleep_until(STOP_AT_MIN)
        kubectl("scale", "deployment/consumer", "--replicas=0")
        kubectl("wait", "--for=delete", "pod", "-l", SELECTOR, "--timeout=120s", check=False)
        end = position(client, stream)
        written = entries_after(client, stream, start.entry_id)
        before = ids_up_to(client, stream, start.entry_id)
        logger.info("Stopped: %d entries written since the start", len(written))

        last_time = max(at for _, at in written) + REFERENCE_MARGIN
        reference = asyncio.run(read_reference(start.checkpoint, end.checkpoint, last_time))
        reference = [(event_id, at) for event_id, at in reference if event_id not in before]
    finally:
        kubectl("scale", "deployment/consumer", "--replicas=1")
        client.close()

    redis_ids = [event_id for event_id, _ in written]
    result = compare(reference, redis_ids, hard)
    report(result)
    soft_ids = in_window(reference, soft)
    print(
        f"Events that arrived around the graceful delete:           {len(soft_ids)}"
        f" ({len(soft_ids & set(redis_ids))} of them in Redis)"
    )
    return 0 if result.passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scale", type=float, default=1.0, help="multiply all wait times")
    args = parser.parse_args()
    setup_logging()
    return run(args.scale)


if __name__ == "__main__":
    sys.exit(main())
