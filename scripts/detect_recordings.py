"""Detect spikes in long recordings, offline, and store them in Postgres for evaluation.

    uv run python scripts/detect_recordings.py \
        --file data/recordings/a.jsonl --file data/recordings/b.jsonl \
        --variant default: --variant loose:min_edits=3,min_editors=2

Replaying days of recordings through Redis (`stream.replay` + `detect.run`) takes hours.
This script gives the same spikes much faster: it reads the files in the given order
with the stage-1 filter and deduplication (`is_wanted`, `normalize`, unique `event_id`),
runs one `SpikeDetector` per variant on the same edits, and writes each variant's spikes
to Postgres as run `<variant name>` (replacing that run's earlier spikes). Spec 02a's
determinism check showed the two paths produce identical spikes.

Overlapping recordings are fine: an event seen twice is kept once, as in stage 1.
Each variant's changes apply on top of the `DETECT_*` settings; pass the same changes to
`wikipulse.eval.detection --set` so its report shows the right configuration.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import psycopg

from wikipulse.config import DetectorConfig, get_settings
from wikipulse.db.spikes import delete_run, upsert_spikes
from wikipulse.detect.detector import SpikeDetector, SpikeRecord
from wikipulse.stream.filters import is_wanted, normalize
from wikipulse.stream.models import EditEvent
from wikipulse.stream.sources import parse_recording_line

logger = logging.getLogger("detect_recordings")


def load_events(paths: list[Path]) -> list[EditEvent]:
    """The kept edits of the recordings, in file order, each event once."""
    events: list[EditEvent] = []
    seen: set[str] = set()
    for path in paths:
        start, before = time.monotonic(), len(events)
        with path.open(encoding="utf-8") as lines:
            for line in lines:
                # Cheap pre-filter: a wanted message always names enwiki. `is_wanted`
                # still makes the real decision, so this changes nothing but speed.
                if "enwiki" not in line:
                    continue
                try:
                    raw = json.loads(parse_recording_line(line)[1])
                except ValueError:
                    continue
                if isinstance(raw, dict) and is_wanted(raw):
                    event = normalize(raw)
                    if event.event_id not in seen:
                        seen.add(event.event_id)
                        events.append(event)
        logger.info(
            "%s: %d new kept edits in %.0f s", path.name, len(events) - before,
            time.monotonic() - start,
        )  # fmt: skip
    return events


def detect(events: list[EditEvent], config: DetectorConfig) -> list[SpikeRecord]:
    detector = SpikeDetector(config)
    for event in events:
        detector.process(event)
    latest = {(s.title, s.started_at): s for s in detector.pop_changes()}
    return sorted(latest.values(), key=lambda s: s.started_at)


def parse_variant(text: str) -> tuple[str, dict[str, str]]:
    """`loose:min_edits=3,min_editors=2` -> ("loose", {...}); `default:` has no changes."""
    name, _, changes = text.partition(":")
    overrides = dict(item.split("=", 1) for item in changes.split(",") if item)
    return name, overrides


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, action="append", required=True)
    parser.add_argument("--variant", action="append", required=True, help="name:k=v,k=v")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("wikipulse.detect").setLevel(logging.WARNING)  # one line per spike
    settings = get_settings()
    events = load_events(args.file)
    humans = sum(not e.is_bot for e in events)
    first = min(e.event_time for e in events)
    last = max(e.event_time for e in events)
    logger.info("%d kept edits (%d by humans), %s to %s", len(events), humans, first, last)
    base = settings.detector_config().model_dump()
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        for text in args.variant:
            name, overrides = parse_variant(text)
            config = DetectorConfig.model_validate({**base, **overrides})
            start = time.monotonic()
            spikes = detect(events, config)
            delete_run(conn, name)
            upsert_spikes(conn, name, spikes)
            logger.info(
                "Run %s (%s): %d spikes in %.0f s",
                name, overrides or "defaults", len(spikes), time.monotonic() - start,
            )  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
