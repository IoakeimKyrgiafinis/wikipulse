"""The detection eval gate (spec 09c): fail a pull request when detection gets worse.

    uv run python -m wikipulse.eval.gate                    # check against the baseline
    uv run python -m wikipulse.eval.gate --set min_edits=3  # try a change first
    uv run python -m wikipulse.eval.gate --write-baseline   # accept the current numbers

It runs the spike detector, with the `DETECT_*` settings (plus `--set` changes), over a
small committed recording (`tests/fixtures/detection_gate/edits.jsonl.gz`, 16 hours of
English Wikipedia), scores the spikes against frozen labels
(`evals/labels/detection_gate_labels.jsonl`), and compares precision and recall with the
stored baseline (`evals/baselines/detection_gate.json`). If either drops by more than the
baseline's `margin`, the gate fails (exit code 1). No network, no database, no GPU, and
the same input always gives the same numbers, so CI can run it on every pull request.

The numbers:

- **Precision**: of the spikes the detector raised in the evaluated hours, the share
  labeled `news`. A spike no label covers counts as *not news*: a change that makes the
  detector raise new spikes must earn them by labeling them (the gate lists them).
  `unsure` labels are left out.
- **Recall**: of the bursts labeled `news`, the share some spike of the run covers
  (same article, overlapping active periods, as in spec 02b).

The labels cover every spike that four detector variants (strict, default, medium,
loose) raised on the recording, so moving a threshold within that range is judged on
real labels. How the recording and the labels were made: `scripts/make_gate_fixture.py`.
"""

import argparse
import gzip
import hashlib
import json
import logging
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wikipulse.config import DetectorConfig, get_settings
from wikipulse.detect.detector import SpikeDetector, SpikeRecord
from wikipulse.eval.label_store import load_labels
from wikipulse.eval.metrics import Label, pick_label, ratio
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.models import EditEvent

logger = logging.getLogger(__name__)

DEFAULT_FIXTURE = Path("tests") / "fixtures" / "detection_gate" / "edits.jsonl.gz"
DEFAULT_LABELS = Path("evals") / "labels" / "detection_gate_labels.jsonl"
DEFAULT_BASELINE = Path("evals") / "baselines" / "detection_gate.json"
FIXTURE_FORMAT = "wikipulse-gate-edits/1"
# How far precision or recall may fall below the baseline before the gate fails.
DEFAULT_MARGIN = 0.05
# The comment a fixture edit gets when the original was a revert: the detector only
# looks at whether the comment marks a revert (`is_revert`).
REVERT_COMMENT = "Reverted edits"


# --- The fixture -------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureInfo:
    """The fixture's first line: where it came from and which hours are evaluated."""

    source: str
    start: datetime  # first edit at or after this (the warm-up begins)
    evaluate_from: datetime  # spikes whose burst begins before this are warm-up
    end: datetime  # edits before this
    edits: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FIXTURE_FORMAT,
            "source": self.source,
            "start": self.start.isoformat(),
            "evaluate_from": self.evaluate_from.isoformat(),
            "end": self.end.isoformat(),
            "edits": self.edits,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FixtureInfo":
        if data.get("format") != FIXTURE_FORMAT:
            raise ValueError(f"not a gate fixture (format {data.get('format')!r})")
        return cls(
            source=data["source"],
            start=datetime.fromisoformat(data["start"]),
            evaluate_from=datetime.fromisoformat(data["evaluate_from"]),
            end=datetime.fromisoformat(data["end"]),
            edits=data["edits"],
            note=data.get("note", ""),
        )


def compact_edit(event: EditEvent, user_number: int, revert: bool) -> dict[str, Any]:
    """One fixture line: event time, title, a numbered user, and the revert flag."""
    return {
        "t": round(event.event_time.timestamp(), 3),
        "p": event.title,
        "u": user_number,
        "r": int(revert),
    }


def edit_from_compact(number: int, data: dict[str, Any]) -> EditEvent:
    """The `EditEvent` the detector sees for one fixture line (a human edit)."""
    moment = datetime.fromtimestamp(data["t"], UTC)
    return EditEvent(
        event_id=f"gate-{number}",
        event_time=moment,
        received_at=moment,
        title=data["p"],
        type="edit",
        user=f"user-{data['u']}",
        is_bot=False,
        is_minor=False,
        rev_old=None,
        rev_new=None,
        len_old=None,
        len_new=None,
        comment=REVERT_COMMENT if data["r"] else "",
    )


def write_fixture(path: Path, info: FixtureInfo, lines: Iterable[dict[str, Any]]) -> None:
    """Write the fixture: gzip, one JSON object per line, the info first.

    The gzip header's time is fixed (mtime=0), so the same input gives the same bytes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in [info.to_dict(), *lines]
    )
    with path.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as out:
        out.write(text.encode("utf-8"))


def read_fixture(path: Path) -> tuple[FixtureInfo, list[EditEvent]]:
    """The fixture's info and its edits, in file (= event time) order."""
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        info = FixtureInfo.from_dict(json.loads(next(lines)))
        events = [edit_from_compact(n, json.loads(line)) for n, line in enumerate(lines)]
    if len(events) != info.edits:
        raise ValueError(f"{path}: {len(events)} edits, the info line says {info.edits}")
    return info, events


# --- Detection and scoring -----------------------------------------------------------


def detect(events: Sequence[EditEvent], config: DetectorConfig) -> list[SpikeRecord]:
    """Every spike the detector raises on the edits, in its final state, oldest first."""
    detector = SpikeDetector(config)
    for event in events:
        detector.process(event)
    latest = {(s.title, s.started_at): s for s in detector.pop_changes()}
    return sorted(latest.values(), key=lambda s: (s.started_at, s.title))


def in_window(spikes: Iterable[SpikeRecord], start: datetime, end: datetime) -> list[SpikeRecord]:
    """The spikes whose burst began in [start, end): the ones the gate scores."""
    return [s for s in spikes if start <= s.first_edit_at < end]


@dataclass
class GateResult:
    """The gate's numbers for one detector configuration."""

    spikes: int  # spikes in the evaluated hours
    counts: dict[str, int]  # per label value, plus "unlabeled"
    news_bursts: int  # bursts labeled news in the evaluated hours
    found: int  # of those, covered by a spike of the run
    unlabeled: list[SpikeRecord] = field(default_factory=list)
    found_bursts: list[str] = field(default_factory=list)  # burst_key() of each found one

    @property
    def precision(self) -> float | None:
        """news / (news + not news + unlabeled); `unsure` is left out."""
        news = self.counts.get("news", 0)
        judged = news + self.counts.get("not news", 0) + self.counts.get("unlabeled", 0)
        return ratio(news, judged)

    @property
    def recall(self) -> float | None:
        return ratio(self.found, self.news_bursts)


def burst_key(label: Label) -> str:
    """How a labeled burst is named in the baseline and the report."""
    return f"{label.title} ({label.first_edit_at:%Y-%m-%d %H:%M} UTC)"


def covering(labels: Sequence[Label], spike: SpikeRecord) -> list[Label]:
    return [label for label in labels if label.covers(spike)]


def score(
    spikes: Sequence[SpikeRecord], labels: Sequence[Label], start: datetime, end: datetime
) -> GateResult:
    """Precision on the run's spikes in [start, end), recall on the news labels there.

    Recall may use any spike of the run: a burst counts as found when some spike covers
    it, also one that began just before `start`.
    """
    scored = in_window(spikes, start, end)
    counts = {"news": 0, "not news": 0, "unsure": 0, "unlabeled": 0}
    unlabeled = []
    for spike in scored:
        chosen = pick_label(covering(labels, spike))
        if chosen is None:
            counts["unlabeled"] += 1
            unlabeled.append(spike)
        else:
            counts[chosen.label] += 1
    news = [lb for lb in labels if lb.label == "news" and start <= lb.first_edit_at < end]
    found = [burst_key(lb) for lb in news if any(lb.covers(s) for s in spikes)]
    return GateResult(len(scored), counts, len(news), len(found), unlabeled, found)


# --- The baseline --------------------------------------------------------------------


def file_digest(path: Path) -> str:
    """sha256 of a file; text files with line endings normalized, so checkouts agree."""
    data = path.read_bytes()
    if path.suffix != ".gz":
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def baseline_data(
    result: GateResult, config: DetectorConfig, fixture: Path, labels: Path, margin: float
) -> dict[str, Any]:
    return {
        "precision": result.precision,
        "recall": result.recall,
        "margin": margin,
        "spikes": result.spikes,
        "counts": result.counts,
        "news_bursts": result.news_bursts,
        "found": result.found,
        "found_bursts": result.found_bursts,
        "config": config.model_dump(),
        "fixture_sha256": file_digest(fixture),
        "labels_sha256": file_digest(labels),
    }


def check(result: GateResult, baseline: dict[str, Any]) -> list[str]:
    """The reasons the gate fails (empty: it passes)."""
    failures = []
    margin = baseline["margin"]
    for name in ("precision", "recall"):
        now, before = getattr(result, name), baseline[name]
        if before is None:
            continue
        if now is None or now < before - margin - 1e-9:
            failures.append(
                f"{name} dropped from {before:.3f} to {fmt(now)} "
                f"(more than the allowed {margin:.3f})"
            )
    return failures


def stale_inputs(baseline: dict[str, Any], fixture: Path, labels: Path) -> list[str]:
    """The inputs that changed since the baseline was written."""
    stale = []
    if baseline.get("fixture_sha256") != file_digest(fixture):
        stale.append(f"{fixture} changed")
    if baseline.get("labels_sha256") != file_digest(labels):
        stale.append(f"{labels} changed")
    return stale


def config_changes(before: dict[str, Any], config: DetectorConfig) -> dict[str, tuple]:
    now = config.model_dump()
    return {k: (before.get(k), v) for k, v in now.items() if before.get(k) != v}


# --- The report ----------------------------------------------------------------------


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def report_lines(
    result: GateResult,
    baseline: dict[str, Any] | None,
    changes: dict[str, tuple],
    failures: Sequence[str],
) -> Iterator[str]:
    """The gate's report as Markdown (also readable as plain text)."""
    verdict = "FAIL" if failures else "PASS"
    yield f"## Detection eval gate: {verdict}"
    yield ""
    yield "| | this change | baseline |"
    yield "|---|---:|---:|"
    for name in ("precision", "recall"):
        before = fmt(baseline[name]) if baseline else "-"
        yield f"| {name} | {fmt(getattr(result, name))} | {before} |"
    yield f"| spikes scored | {result.spikes} | {baseline['spikes'] if baseline else '-'} |"
    yield f"| news bursts found | {result.found} of {result.news_bursts} | " + (
        f"{baseline['found']} of {baseline['news_bursts']} |" if baseline else "- |"
    )
    yield ""
    c = result.counts
    yield (
        f"Spike labels: {c['news']} news, {c['not news']} not news, {c['unsure']} unsure, "
        f"{c['unlabeled']} without a label (counted as not news)."
    )
    if baseline:
        yield f"Allowed drop: {baseline['margin']:.3f}."
    if changes:
        yield ""
        yield "Detector settings that differ from the baseline's:"
        for key, (old, new) in sorted(changes.items()):
            yield f"- `{key}`: {old} -> {new}"
    for failure in failures:
        yield ""
        yield f"**Fails:** {failure}"
    lost = (
        sorted(set(baseline.get("found_bursts", [])) - set(result.found_bursts)) if baseline else []
    )
    if lost:
        yield ""
        yield "News bursts the baseline found and this change misses:"
        yield from (f"- {key}" for key in lost)
    gained = (
        sorted(set(result.found_bursts) - set(baseline.get("found_bursts", []))) if baseline else []
    )
    if gained:
        yield ""
        yield "News bursts this change finds and the baseline missed:"
        yield from (f"- {key}" for key in gained)
    if result.unlabeled:
        yield ""
        yield "Spikes without a label (label them with scripts/make_gate_fixture.py):"
        for spike in result.unlabeled[:20]:
            yield f"- {spike.title} ({spike.first_edit_at:%Y-%m-%d %H:%M} UTC)"
        if len(result.unlabeled) > 20:
            yield f"- ... and {len(result.unlabeled) - 20} more"


# --- Command line --------------------------------------------------------------------


def parse_overrides(items: list[str]) -> dict[str, str]:
    """`["min_edits=4,min_editors=2"]` -> {"min_edits": "4", "min_editors": "2"}."""
    overrides = {}
    for item in items:
        for part in item.split(","):
            if part.strip():
                key, sep, value = part.partition("=")
                if not sep:
                    raise SystemExit(f"--set wants key=value, not {part!r}")
                overrides[key.strip()] = value.strip()
    return overrides


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--set", action="append", default=[], help="detector setting, e.g. min_edits=4"
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="store this run's numbers as the new baseline (and pass)",
    )
    parser.add_argument("--margin", type=float, help="with --write-baseline: the allowed drop")
    parser.add_argument("--markdown", type=Path, help="also append the report to this file")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    base = get_settings().detector_config().model_dump()
    config = DetectorConfig.model_validate({**base, **parse_overrides(args.set)})
    info, events = read_fixture(args.fixture)
    labels = load_labels(args.labels)
    result = score(detect(events, config), labels, info.evaluate_from, info.end)
    baseline = json.loads(args.baseline.read_text("utf-8")) if args.baseline.exists() else None

    if args.write_baseline:
        margin = args.margin if args.margin is not None else DEFAULT_MARGIN
        if args.margin is None and baseline:
            margin = baseline["margin"]
        data = baseline_data(result, config, args.fixture, args.labels, margin)
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        with args.baseline.open("w", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(data, indent=2) + "\n")
        logger.info("Wrote the baseline %s", args.baseline)
        failures: list[str] = []
        baseline = data
    elif baseline is None:
        failures = [f"no baseline at {args.baseline}: run with --write-baseline first"]
    else:
        failures = [
            f"the baseline is stale ({reason}): check the change, then run with "
            "--write-baseline and commit the baseline"
            for reason in stale_inputs(baseline, args.fixture, args.labels)
        ] + check(result, baseline)

    changes = config_changes(baseline["config"], config) if baseline else {}
    text = "\n".join(report_lines(result, baseline, changes, failures)) + "\n"
    sys.stdout.write(text)
    if args.markdown:
        with args.markdown.open("a", encoding="utf-8") as out:
            out.write(text)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # titles on a Windows console
    logging.getLogger("wikipulse.detect").setLevel(logging.WARNING)  # a line per spike
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
