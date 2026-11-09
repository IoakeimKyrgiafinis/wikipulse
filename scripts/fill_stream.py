"""Fast bulk load of a recording's kept edits into a Redis Stream (for lake runs and tests).

    uv run python scripts/fill_stream.py --recording data/recordings/<file>.jsonl
        --stream-key wiki:lake:history:edits [--max-messages N] [--reset]

Applies the same filter and normalization as the stage 1 consumer (`is_wanted`,
`normalize`, deduplication by event id, `received_at` from the recording) and writes the
same entry fields, but in pipelined batches without the consumer's per-message
checkpoint. That makes it many times faster than `wikipulse.stream.replay`, which goes
through the crash-safe live code path one message at a time. Use replay to test the
consumer; use this to get a lot of recorded history into a stream quickly.
"""

import argparse
import json
import logging
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import redis

from wikipulse.config import get_settings
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.filters import is_wanted, normalize
from wikipulse.stream.models import EditEvent

logger = logging.getLogger("fill_stream")

BATCH = 2000


def kept_edits(path: Path, max_messages: int | None = None) -> Iterator[EditEvent]:
    """The kept, normalized, deduplicated edits of the first `max_messages` lines."""
    seen: set[str] = set()
    with path.open(encoding="utf-8") as lines:
        for number, line in enumerate(lines):
            if max_messages is not None and number >= max_messages:
                return
            try:
                record = json.loads(line)
                raw = json.loads(record["data"])
                if not isinstance(raw, dict) or not is_wanted(raw):
                    continue
                event = normalize(raw, datetime.fromisoformat(record["received_at"]))
            except (ValueError, KeyError, TypeError):
                continue  # broken line or malformed message: the consumer drops it too
            if event.event_id not in seen:
                seen.add(event.event_id)
                yield event


def entry_fields(event: EditEvent) -> dict[str, str]:
    """The stream entry the stage 1 consumer writes for an edit."""
    return {
        "data": event.model_dump_json(),
        "title": event.title,
        "event_time": event.event_time.isoformat(),
    }


def fill(client: redis.Redis, stream: str, events: Iterator[EditEvent]) -> int:
    """XADD every edit in pipelined batches; returns how many were written."""
    written = 0
    pipe = client.pipeline(transaction=False)
    for event in events:
        pipe.xadd(stream, entry_fields(event))
        written += 1
        if written % BATCH == 0:
            pipe.execute()
            if written % (BATCH * 25) == 0:
                logger.info("%d edits written (event time %s)", written, event.event_time)
    pipe.execute()
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--recording", type=Path, required=True)
    parser.add_argument("--stream-key", required=True, help="the stream to fill")
    parser.add_argument("--max-messages", type=int, help="read only this many recording lines")
    parser.add_argument("--reset", action="store_true", help="delete the stream first")
    args = parser.parse_args()
    setup_logging()
    settings = get_settings()
    if args.stream_key in (settings.stream_key, settings.replay_stream_key):
        logger.error("Refusing to bulk-load into %s; pick a stream of your own", args.stream_key)
        return 2
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        if args.reset:
            client.delete(args.stream_key)
        written = fill(client, args.stream_key, kept_edits(args.recording, args.max_messages))
        logger.info(
            "Wrote %d edits to %s (length now %d)",
            written,
            args.stream_key,
            client.xlen(args.stream_key),
        )
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
