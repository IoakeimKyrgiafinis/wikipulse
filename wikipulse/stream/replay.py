"""Replay: `uv run python -m wikipulse.stream.replay --file <path> [--speed 10|max]`.

Feeds a recording through exactly the same processing core as the live consumer
(`Consumer.process`), so everything downstream sees the same Redis Stream entries.
`--speed max` (the default) goes as fast as possible; `--speed N` replays N times
faster than real time, pacing by the events' own timestamps.

Replay keeps its own checkpoint and stats under `wiki:replay:*`, so it never moves the
live consumer's checkpoint. Each run deduplicates on its own (fresh dedup keys), so
replaying the same file twice writes the same events twice. Use `--stream-key` to
replay into a separate stream.
"""

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

import redis.asyncio as aioredis

from wikipulse.config import Settings, get_settings
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.consumer import Consumer, RedisKeys
from wikipulse.stream.resilience import install_stop_handlers
from wikipulse.stream.sources import FileSource

logger = logging.getLogger(__name__)

REPLAY_NAMESPACE = "wiki:replay"


def parse_speed(value: str) -> float | None:
    """`max` means no pacing (None); otherwise a positive speed-up factor."""
    if value.strip().lower() == "max":
        return None
    try:
        speed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("speed must be a positive number or 'max'") from None
    if speed <= 0:
        raise argparse.ArgumentTypeError("speed must be a positive number or 'max'")
    return speed


def replay_keys(stream: str, run_id: str) -> RedisKeys:
    """Replay keys: shared checkpoint and stats, but dedup keys unique to this run."""
    keys = RedisKeys.for_namespace(stream, REPLAY_NAMESPACE)
    return RedisKeys(
        stream=keys.stream,
        checkpoint=keys.checkpoint,
        stats=keys.stats,
        seen_prefix=f"{REPLAY_NAMESPACE}:{run_id}:seen:",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, required=True, help="recording to replay")
    parser.add_argument(
        "--speed", type=parse_speed, default=None, help="'max' (default) or a factor like 10"
    )
    parser.add_argument("--stream-key", help="Redis Stream to write to (default: STREAM_KEY)")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace, settings: Settings) -> dict[str, str]:
    stop = asyncio.Event()
    install_stop_handlers(stop)
    keys = replay_keys(args.stream_key or settings.stream_key, uuid.uuid4().hex)
    redis = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.delete(keys.stats)  # stats describe this replay only
        consumer = Consumer(redis, keys, settings.stream_maxlen)
        speed = "max" if args.speed is None else f"{args.speed:g}x"
        logger.info("Replaying %s into %s at speed %s", args.file, keys.stream, speed)
        await consumer.consume(FileSource(args.file, speed=args.speed), stop)
        return await redis.hgetall(keys.stats)
    finally:
        await redis.aclose()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.file.is_file():
        print(f"No such recording: {args.file}", file=sys.stderr)
        return 2
    settings = get_settings()
    setup_logging()
    stats = asyncio.run(run(args, settings))
    print("Replay finished: " + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
