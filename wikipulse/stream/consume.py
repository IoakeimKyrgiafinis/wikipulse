"""Live consumer: `uv run python -m wikipulse.stream.consume`.

Reads Wikimedia's recentchange stream and writes English Wikipedia article edits to
the Redis Stream `STREAM_KEY`. Resumes from its checkpoint after a restart or crash.
Stop it with Ctrl+C; it finishes the current message first.

Only one consumer writes a stream at a time: this one first takes the stream's writer
lease (`wikipulse.stream.lease`) and waits, with a log message, while another consumer
(e.g. the one in the Kubernetes cluster) holds it.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable

import redis.asyncio as aioredis

from wikipulse.config import Settings, get_settings
from wikipulse.health import Heartbeat
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.consumer import LIVE_NAMESPACE, Consumer, RedisKeys
from wikipulse.stream.lease import StreamLease
from wikipulse.stream.resilience import Backoff, install_stop_handlers
from wikipulse.stream.sources import LiveSource, Source

logger = logging.getLogger(__name__)


def warn_if_placeholder_user_agent(settings: Settings) -> None:
    """Wikimedia asks for a real contact address in the User-Agent; remind the user."""
    if settings.user_agent_is_placeholder():
        logger.warning(
            "USER_AGENT still contains placeholder text; set a real contact in .env "
            "(see https://meta.wikimedia.org/wiki/User-Agent_policy)"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--since",
        help="ISO timestamp to start from, used only when there is no checkpoint yet",
    )
    parser.add_argument("--stream-key", help="Redis Stream to write to (default: STREAM_KEY)")
    parser.add_argument(
        "--namespace",
        default=LIVE_NAMESPACE,
        help=f"prefix for the checkpoint, stats and dedup keys (default: {LIVE_NAMESPACE})",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace, settings: Settings) -> bool:
    """Consume until stopped. False if the writer lease was lost to another consumer."""
    stop = asyncio.Event()
    install_stop_handlers(stop)
    keys = RedisKeys.for_namespace(args.stream_key or settings.stream_key, args.namespace)
    source = LiveSource(settings.stream_url, settings.user_agent, since=args.since)
    redis = aioredis.Redis.from_url(settings.redis_url)
    heartbeat = Heartbeat.from_settings(settings)
    lease = StreamLease(redis, keys.stream, settings.stream_lease_ttl_s)
    try:
        # Before the first connection to Wikimedia: a waiting consumer stays unconnected.
        if not await lease.acquire(stop, on_wait=heartbeat):
            return True  # stopped while waiting
        consumer = Consumer(redis, keys, settings.stream_maxlen, lease=lease)
        logger.info("Consuming %s into Redis Stream %s", settings.stream_url, keys.stream)
        backoff = Backoff(max_s=settings.reconnect_max_s)
        return await consume_holding(lease, consumer, source, stop, backoff, heartbeat)
    finally:
        await redis.aclose()


async def consume_holding(
    lease: StreamLease,
    consumer: Consumer,
    source: Source,
    stop: asyncio.Event,
    backoff: Backoff,
    heartbeat: Callable[[], None] = lambda: None,
) -> bool:
    """Consume while renewing the lease; give it up at the end. False if it was lost.

    The lease is lost when a renewal finds another holder (`StreamLease.keep`) or a write
    does (`LeaseLost` in `Consumer.run_live`); both set `stop`.
    """
    keeper = asyncio.create_task(lease.keep(stop))
    try:
        await consumer.run_live(source, stop, backoff, heartbeat)
    finally:
        stop.set()
        await keeper
    try:
        held = await lease.holder() == lease.owner
        await lease.release()
    except Exception as exc:  # Redis gone: the lease expires by itself after its TTL
        logger.warning("Could not release the writer lease: %s: %s", type(exc).__name__, exc)
        return True
    logger.info("Stopped; checkpoint saved in %s", consumer.keys.checkpoint)
    return held


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    warn_if_placeholder_user_agent(settings)
    return 0 if asyncio.run(run(args, settings)) else 1


if __name__ == "__main__":
    sys.exit(main())
