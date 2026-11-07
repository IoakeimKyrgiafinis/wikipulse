"""Try detector thresholds on a recording, fast and offline (no Redis, no Postgres).

    uv run python scripts/compare_thresholds.py --file data/recordings/<rec>.jsonl \
        --set min_edits=5,min_editors=3 --set min_edits=4,min_editors=3

Reads the recording with the stage-1 filter (`is_wanted` + `normalize`), runs one
`SpikeDetector` per `--set` (changes on top of the configured DETECT_* values), and
prints how many spikes each finds and the top ones. This is the "change one threshold,
replay, compare" loop in seconds; `wikipulse.detect.run` on a replay gives the same
spikes, because both use the same detector on the same events in the same order.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from wikipulse.config import DetectorConfig, get_settings
from wikipulse.detect.detector import SpikeDetector, SpikeRecord
from wikipulse.stream.filters import is_wanted, normalize
from wikipulse.stream.models import EditEvent
from wikipulse.stream.sources import parse_recording_line


def load_events(path: Path) -> list[EditEvent]:
    """The kept edits of a recording, in recording order (as replay feeds them)."""
    events = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            try:
                raw = json.loads(parse_recording_line(line)[1])
            except ValueError:
                continue
            if isinstance(raw, dict) and is_wanted(raw):
                event = normalize(raw)
                if event.event_id not in seen:  # stage 1 deduplicates too
                    seen.add(event.event_id)
                    events.append(event)
    return events


def parse_overrides(text: str) -> dict[str, str]:
    return dict(item.split("=", 1) for item in text.split(",") if item)


def detect(events: list[EditEvent], config: DetectorConfig) -> list[SpikeRecord]:
    detector = SpikeDetector(config)
    for event in events:
        detector.process(event)
    latest = {(s.title, s.started_at): s for s in detector.pop_changes()}
    return sorted(latest.values(), key=lambda s: -s.score)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--set", action="append", default=[], help="e.g. min_edits=4,min_editors=2")
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # titles with dashes etc. on Windows consoles
    logging.basicConfig(level=logging.WARNING)
    base = get_settings().detector_config()
    events = load_events(args.file)
    humans = sum(not e.is_bot for e in events)
    print(
        f"{len(events)} kept edits ({humans} by humans), "
        f"{events[0].event_time:%Y-%m-%d %H:%M} to {events[-1].event_time:%H:%M} UTC"
    )
    for text in args.set or [""]:
        config = DetectorConfig.model_validate({**base.model_dump(), **parse_overrides(text)})
        spikes = detect(events, config)
        print(f"\n[{text or 'configured defaults'}] {len(spikes)} spikes")
        for s in spikes[: args.top]:
            print(
                f"  {s.score:6.1f}  {s.total_edits:3d} edits {s.distinct_editors:2d} editors "
                f"peak {s.peak_edits_10m:2d}  {s.started_at:%H:%M}  {s.title}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
