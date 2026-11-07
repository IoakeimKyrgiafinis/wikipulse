"""Live consumer: `uv run python -m wikipulse.stream.consume`.

Reads Wikimedia's recentchange stream and writes English Wikipedia article edits to
the Redis Stream `STREAM_KEY`. Resumes from its checkpoint after a restart or crash.
Stop it with Ctrl+C; it finishes the current message first.
"""

import argparse
import asyncio
import logging
import sys

import redis.asyncio as aioredis

from wikipulse.config import Settings, get_settings
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.consumer import LIVE_NAMESPACE, Consumer, RedisKeys
from wikipulse.stream.resilience import Backoff, install_stop_handlers
from wikipulse.stream.sources import LiveSource

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


async def run(args: argparse.Namespace, settings: Settings) -> None:
    stop = asyncio.Event()
    install_stop_handlers(stop)
    keys = RedisKeys.for_namespace(args.stream_key or settings.stream_key, args.namespace)
    source = LiveSource(settings.stream_url, settings.user_agent, since=args.since)
    redis = aioredis.Redis.from_url(settings.redis_url)
    try:
        consumer = Consumer(redis, keys, settings.stream_maxlen)
        logger.info("Consuming %s into Redis Stream %s", settings.stream_url, keys.stream)
        await consumer.run_live(source, stop, Backoff(max_s=settings.reconnect_max_s))
    finally:
        await redis.aclose()
    logger.info("Stopped cleanly; checkpoint saved in %s", keys.checkpoint)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    warn_if_placeholder_user_agent(settings)
    asyncio.run(run(args, settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
