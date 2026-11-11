"""Make the detection eval gate's recording and labels (spec 09c), from a local recording.

    uv run python scripts/make_gate_fixture.py \
        --file data/recordings/history-20261106T2100Z.jsonl \
        --start 2026-11-06T21:00Z --evaluate-from 2026-11-07T01:00Z --end 2026-11-07T13:00Z

Runs once, on the laptop (the recordings in data/ are never committed); what it writes is
committed and then read by `python -m wikipulse.eval.gate`:

1. **The recording** `tests/fixtures/detection_gate/edits.jsonl.gz`: the human edits of
   English Wikipedia articles between `--start` and `--end`, in event-time order, kept
   small and free of personal data:
   - only what the detector reads: event time, title, user, and whether the edit summary
     marks a revert. User names (IP addresses for anonymous editors) become numbers in
     order of first appearance; edit summaries are dropped;
   - bot edits are left out (the detector only uses them to move its clock forward);
   - pages with fewer than `MIN_PAGE_EDITS` human edits in the whole period are left
     out: no start rule in the range the gate covers can fire on them. They only feed
     the overall edit rate (the daily rhythm), which thinning by the same share hardly
     changes; the spikes of all four variants were the same with and without them.
2. **The labels** `evals/labels/detection_gate_labels.jsonl`: one label per burst that
   any of four detector variants (`VARIANTS`) raised in the evaluated hours. Spikes of
   different variants on the same article with overlapping periods are one burst. A
   burst already labeled in `evals/labels/spike_labels.jsonl` (spec 02b or 07a, with the
   page summary) takes that label; the others are labeled by the LLM labeler of spec 02b
   (`wikipulse.eval.label`), seeing the burst's edit comments from the recording but no
   page summary (fetching one would contact Wikimedia). Human labels already in the
   gate's labels file are kept, never overwritten.

After a new fixture or new labels, run `python -m wikipulse.eval.gate --write-baseline`
and commit the three files together.
"""

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so `wikipulse` imports when run as a script
sys.path.insert(0, str(ROOT / "scripts"))

from detect_recordings import load_events  # noqa: E402

from wikipulse.config import DetectorConfig, get_settings  # noqa: E402
from wikipulse.detect.detector import SpikeRecord, is_revert  # noqa: E402
from wikipulse.eval import gate  # noqa: E402
from wikipulse.eval.label import MAX_COMMENTS, LLMLabeler, SpikeContext  # noqa: E402
from wikipulse.eval.label_store import (  # noqa: E402
    DEFAULT_LABELS_PATH,
    LabelRecord,
    load_records,
    record_for,
)
from wikipulse.eval.metrics import pick_label, spike_end  # noqa: E402
from wikipulse.llm import get_client  # noqa: E402
from wikipulse.logging_setup import setup_logging  # noqa: E402
from wikipulse.stream.models import EditEvent  # noqa: E402

logger = logging.getLogger("make_gate_fixture")

MIN_PAGE_EDITS = 3
RUN_ID = "09c-gate"
# The thresholds of spec 02b's four variants: the labels cover all of them.
VARIANTS: dict[str, dict[str, int]] = {
    "strict": {"min_edits": 6, "min_editors": 4, "long_min_edits": 12, "long_min_editors": 5},
    "default": {},  # the code's defaults (DetectorConfig)
    "medium": {"min_edits": 4, "min_editors": 3, "long_min_edits": 8, "long_min_editors": 3},
    "loose": {"min_edits": 3, "min_editors": 2, "long_min_edits": 6, "long_min_editors": 2},
}


def human_edits(events: Sequence[EditEvent], start: datetime, end: datetime) -> list[EditEvent]:
    """The human edits in [start, end) on pages with enough of them, in event-time order."""
    chosen = [e for e in events if not e.is_bot and start <= e.event_time < end]
    pages = Counter(e.title for e in chosen)
    chosen = [e for e in chosen if pages[e.title] >= MIN_PAGE_EDITS]
    return sorted(chosen, key=lambda e: e.event_time)  # stable: ties keep file order


def compact_lines(events: Sequence[EditEvent]) -> list[dict]:
    users: dict[str, int] = {}
    return [
        gate.compact_edit(e, users.setdefault(e.user, len(users) + 1), is_revert(e.comment))
        for e in events
    ]


@dataclass
class Burst:
    """Overlapping spikes of one article, from any of the variants."""

    title: str
    first_edit_at: datetime
    end_at: datetime
    spikes: list[SpikeRecord]

    def representative(self) -> SpikeRecord:
        """The burst as one spike, for the labeler: its biggest spike, over the whole period."""
        biggest = max(self.spikes, key=lambda s: s.total_edits)
        return replace(biggest, first_edit_at=self.first_edit_at, ended_at=self.end_at)


def bursts_of(spikes: Sequence[SpikeRecord]) -> list[Burst]:
    """Group spikes of the same article whose active periods overlap."""
    bursts: list[Burst] = []
    for spike in sorted(spikes, key=lambda s: (s.title, s.first_edit_at)):
        last = bursts[-1] if bursts else None
        if last and last.title == spike.title and spike.first_edit_at <= last.end_at:
            last.end_at = max(last.end_at, spike_end(spike))
            last.spikes.append(spike)
        else:
            bursts.append(Burst(spike.title, spike.first_edit_at, spike_end(spike), [spike]))
    return sorted(bursts, key=lambda b: (b.first_edit_at, b.title))


def comments_for(burst: Burst, events: Sequence[EditEvent]) -> list[str]:
    """The burst's human edit comments from the recording, newest first."""
    found = [
        e.comment
        for e in events
        if e.title == burst.title
        and not e.is_bot
        and burst.first_edit_at <= e.event_time <= burst.end_at
        and e.comment.strip()
    ]
    return list(reversed(found))[:MAX_COMMENTS]


def existing_label(burst: Burst, records: Sequence[LabelRecord]) -> LabelRecord | None:
    """The label spike_labels.jsonl gives this burst (human beats LLM, later beats earlier)."""
    matching = [r for r in records if any(r.is_for(s) for s in burst.spikes)]
    chosen = pick_label([r.to_label() for r in matching])
    if chosen is None:
        return None
    return next(r for r in reversed(matching) if r.to_label() == chosen)


def copied(burst: Burst, record: LabelRecord) -> LabelRecord:
    return record.model_copy(
        update={
            "title": burst.title,
            "run_id": RUN_ID,
            "started_at": min(s.started_at for s in burst.spikes),
            "first_edit_at": burst.first_edit_at,
            "end_at": burst.end_at,
            "reason": f"[from {record.run_id}] {record.reason}",
        }
    )


def label_bursts(
    bursts: Sequence[Burst],
    events: Sequence[EditEvent],
    sources: Sequence[LabelRecord],
    kept: Sequence[LabelRecord],
    labeler: LLMLabeler | None,
    model: str,
) -> list[LabelRecord]:
    """One record per burst: a kept human label, a 02b/07a label, or a new LLM label."""
    records = []
    for number, burst in enumerate(bursts, start=1):
        human = [r for r in kept if r.labeler == "human" and any(r.is_for(s) for s in burst.spikes)]
        previous = existing_label(burst, sources)
        if human:
            record = human[-1]
        elif previous is not None:
            record = copied(burst, previous)
        elif labeler is None:
            raise SystemExit(f"{burst.title}: no label yet, and --no-llm was given")
        else:
            spike = burst.representative()
            context = SpikeContext(spike, comments=comments_for(burst, events))
            label, reason = labeler.label(context)
            record = record_for(
                spike, RUN_ID, label, "llm", datetime.now(UTC), model=model, reason=reason
            )
        logger.info("[%d/%d] %s: %s (%s)", number, len(bursts), burst.title, record.label,
                    record.labeler)  # fmt: skip
        records.append(record)
    return records


def write_labels(path: Path, records: Sequence[LabelRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as out:
        for record in records:
            out.write(record.model_dump_json() + "\n")


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, action="append", required=True)
    parser.add_argument("--start", type=parse_time, required=True)
    parser.add_argument("--evaluate-from", type=parse_time, required=True)
    parser.add_argument("--end", type=parse_time, required=True)
    parser.add_argument("--fixture", type=Path, default=gate.DEFAULT_FIXTURE)
    parser.add_argument("--labels", type=Path, default=gate.DEFAULT_LABELS)
    parser.add_argument("--source-labels", type=Path, default=DEFAULT_LABELS_PATH)
    parser.add_argument("--model", help="LLM for new labels (default: EVAL_LABEL_MODEL)")
    parser.add_argument("--no-llm", action="store_true", help="fail instead of asking the LLM")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    logging.getLogger("wikipulse.detect").setLevel(logging.WARNING)
    settings = get_settings()
    events = load_events(args.file)
    chosen = human_edits(events, args.start, args.end)
    note = (
        f"Human edits of English Wikipedia articles with at least {MIN_PAGE_EDITS} in the "
        "period; users numbered, edit summaries reduced to a revert flag."
    )
    source = ", ".join(path.name for path in args.file)
    info = gate.FixtureInfo(source, args.start, args.evaluate_from, args.end, len(chosen), note)
    gate.write_fixture(args.fixture, info, compact_lines(chosen))
    logger.info("Wrote %d edits to %s", len(chosen), args.fixture)

    _, fixture_events = gate.read_fixture(args.fixture)  # exactly what the gate will see
    spikes = []
    for name, overrides in VARIANTS.items():
        found = gate.detect(fixture_events, DetectorConfig(**overrides))
        window = gate.in_window(found, args.evaluate_from, args.end)
        logger.info("Variant %s: %d spikes, %d in the evaluated hours", name, len(found),
                    len(window))  # fmt: skip
        spikes += window
    bursts = bursts_of(spikes)
    logger.info("%d bursts to label", len(bursts))

    model = args.model or settings.eval_label_model
    labeler = None if args.no_llm else LLMLabeler(get_client(model, settings))
    try:
        records = label_bursts(
            bursts, events, load_records(args.source_labels), load_records(args.labels),
            labeler, model,
        )  # fmt: skip
    finally:
        if labeler is not None:
            labeler.close()
    write_labels(args.labels, records)
    counts = Counter(r.label for r in records)
    logger.info("Wrote %d labels to %s: %s", len(records), args.labels, dict(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
