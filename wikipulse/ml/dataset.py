"""Versioned, labeled training data: `uv run python -m wikipulse.ml.dataset build`.

    uv run python -m wikipulse.ml.dataset build      # a new version datasets/v<n>/
    uv run python -m wikipulse.ml.dataset list       # the versions and their manifests

What `build` does, step by step:

1. **Edits.** Reads the lake (`ML_LAKE_PREFIX`, by default the recorded and backfilled
   history) hour by hour, in event-time order, over its last contiguous stretch of hours.
2. **Spikes.** Runs two spike detectors over those edits: the *candidates* (thresholds
   `ML_CANDIDATE_DETECTOR`, looser than the live defaults) and the live *default* rule.
   Both runs are stored in Postgres (run ids `07a-candidates` and `07a-default` by
   default, replaced on every build, never on a dry run) so `wikipulse.eval.label` can
   label them. Spikes in the first `ML_WARMUP_HOURS` (cold baselines) or whose feature
   window runs past the data are left out.
3. **Split by time**: the last `ML_TEST_DAYS` days are the test set, the `ML_VALID_DAYS`
   before them validation, everything earlier training. Never random. At each boundary
   the build *purges* spikes still running when the next split begins and spikes whose
   page spikes again in a later split within `ML_SPLIT_GAP_HOURS` (`purge_boundaries`).
4. **Features** (`features.py`): from the page's edits up to the cutoff (`started_at +
   FEATURE_WINDOW_MIN`) and the page's revision at the cutoff (`pages.py`, MediaWiki API,
   cached). `check_no_leakage` runs on every row.
5. **Labels, as of a moment.** Each split's labels use only evidence from before the next
   split begins (the test split: before the build's *as-of*, by default the end of the
   data). The *weak label*: the article is linked from the "Current events" portal within
   48 hours of the spike (spec 02b's matching); unknown (null) when it isn't linked and
   the portal days aren't final yet. The *02b label*: an LLM or human label from
   `evals/labels/spike_labels.jsonl` covering the spike, where one exists. `label`: 1 if
   on the portal, else the 02b news / not news label, else unknown (null).
6. **Version.** Writes `train/valid/test.parquet` and `manifest.json` (date range, rows,
   label balance, feature list, git commit, file hashes) to
   `<S3_BUCKET>/<ML_DATASET_PREFIX>/v<n>/`, the next free version number.
"""

import argparse
import hashlib
import io
import json
import logging
import math
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from wikipulse.config import DetectorConfig, Settings, get_settings
from wikipulse.detect.detector import SpikeDetector, SpikeRecord
from wikipulse.eval.ground_truth import FINAL_AFTER, PortalDay
from wikipulse.eval.metrics import Label, day_start, match_spikes, pick_label, spike_end
from wikipulse.lake.store import LakeStore
from wikipulse.logging_setup import setup_logging
from wikipulse.ml.features import (
    FEATURE_NAMES,
    FEATURES,
    PageEdit,
    PageSnapshot,
    SpikeWindow,
    check_no_leakage,
)
from wikipulse.stream.models import EditEvent

logger = logging.getLogger(__name__)

SPLITS = ("train", "valid", "test")
CANDIDATE_RUN = "07a-candidates"
RULE_RUN = "07a-default"
PORTAL_MARGIN_DAYS = 2  # portal days fetched around the data (the weak label's 48 h)

# --- 1. edits from the lake -------------------------------------------------------------


def contiguous_hours(hours: Sequence[datetime]) -> tuple[list[datetime], list[datetime]]:
    """(the last run of consecutive hours, the hours missing before it)."""
    if not hours:
        return [], []
    ordered = sorted(hours)
    start = 0
    missing: list[datetime] = []
    for i in range(1, len(ordered)):
        gap = ordered[i] - ordered[i - 1]
        if gap > timedelta(hours=1):
            start = i
            missing.append(ordered[i - 1] + timedelta(hours=1))
    return ordered[start:], missing


def lake_events(store: LakeStore, hours: Iterable[datetime]) -> Iterator[EditEvent]:
    """Every archived edit of the given hours, hour by hour, in event-time order.

    The archiver sorts each hour already; sorting again costs little and makes the order
    (and so the spikes) independent of how a file was written. Rows were validated when
    they were archived, so `model_construct` skips validation.
    """
    for hour in hours:
        table = store.read_hour(hour)
        if table is None:
            continue
        table = table.sort_by([("event_time", "ascending"), ("event_id", "ascending")])
        for row in table.to_pylist():
            yield EditEvent.model_construct(**row)


def detect_spikes(
    events: Iterable[EditEvent], configs: dict[str, DetectorConfig]
) -> dict[str, list[SpikeRecord]]:
    """One detector per config over the same edits; each run's spikes, oldest first."""
    detectors = {name: SpikeDetector(config) for name, config in configs.items()}
    latest: dict[str, dict[tuple[str, datetime], SpikeRecord]] = {name: {} for name in configs}
    count = 0
    for event in events:
        for detector in detectors.values():
            detector.process(event)
        count += 1
        if count % 200_000 == 0:
            for name, detector in detectors.items():
                latest[name].update(((s.title, s.started_at), s) for s in detector.pop_changes())
            logger.info("Detection: %d edits (event time %s)", count, event.event_time)
    for name, detector in detectors.items():
        latest[name].update(((s.title, s.started_at), s) for s in detector.pop_changes())
    logger.info("Detection: %d edits in all", count)
    return {
        name: sorted(spikes.values(), key=lambda s: (s.started_at, s.title))
        for name, spikes in latest.items()
    }


def select_spikes(
    spikes: Iterable[SpikeRecord],
    data_start: datetime,
    data_end: datetime,
    warmup: timedelta,
    window_min: float,
) -> list[SpikeRecord]:
    """Spikes after the warm-up whose 24 h history and feature window lie inside the data."""
    first_allowed = data_start + warmup
    return [
        s
        for s in spikes
        if s.first_edit_at >= first_allowed and window_of(s, window_min).cutoff < data_end
    ]


def window_of(spike: SpikeRecord, window_min: float) -> SpikeWindow:
    """The only part of a stored spike the features may see."""
    return SpikeWindow(spike.title, spike.first_edit_at, spike.started_at, window_min)


EDIT_COLUMNS = [
    "event_time",
    "title",
    "user",
    "is_bot",
    "is_minor",
    "type",
    "len_old",
    "len_new",
    "rev_new",
    "comment",
]


def page_edits(store: LakeStore, titles: Iterable[str]) -> dict[str, list[PageEdit]]:
    """All archived edits of the given pages, oldest first (one filtered scan)."""
    wanted = sorted(set(titles))
    edits: dict[str, list[PageEdit]] = defaultdict(list)
    if not wanted:
        return edits
    table = store.dataset().to_table(columns=EDIT_COLUMNS, filter=ds.field("title").isin(wanted))
    for row in table.to_pylist():
        edits[row["title"]].append(
            PageEdit(
                time=row["event_time"],
                user=row["user"],
                is_bot=row["is_bot"],
                is_minor=row["is_minor"],
                is_new=row["type"] == "new",
                len_old=row["len_old"],
                len_new=row["len_new"],
                rev_new=row["rev_new"],
                comment=row["comment"],
            )
        )
    for page in edits.values():
        page.sort(key=lambda e: e.time)
    return edits


def revision_at(edits: Sequence[PageEdit], cutoff: datetime) -> int | None:
    """The page's revision current at the cutoff: the newest one saved at or before it."""
    known = [e for e in edits if e.time <= cutoff and e.rev_new is not None]
    return max(known, key=lambda e: e.time).rev_new if known else None


# --- 4 and 5. rows: features and labels ---------------------------------------------


def portal_days_of(spike: SpikeRecord, portal: Sequence[PortalDay], as_of: datetime) -> list[date]:
    """The portal days within 48 h of the spike that link its article, begun before `as_of`.

    A portal day's page is taken to belong to its day: a page whose day starts at or after
    `as_of` is evidence from later than the label may use.
    """
    days = match_spikes([spike], portal)[0].portal_days
    return [day for day in days if day_start(day) < as_of]


def portal_final(spike: SpikeRecord, portal: Sequence[PortalDay], as_of: datetime) -> bool:
    """Were all portal pages the weak label depends on complete at `as_of`?

    The weak label looks at portal days up to 48 h after the spike. A day whose page is
    missing or younger than `FINAL_AFTER` (3 days) at `as_of` may still gain links, so
    "not on the portal" isn't known yet for such a spike. `as_of` is a moment of the data
    (the end of the data, a split boundary, or an explicit `--as-of`), never the wall
    clock, so a rebuild gives the same answer.
    """
    first = (spike.first_edit_at - timedelta(hours=48)).astimezone(UTC).date()
    last = (spike_end(spike) + timedelta(hours=48)).astimezone(UTC).date()
    by_day = {p.day: p for p in portal}
    day = first
    while day <= last:
        page = by_day.get(day)
        if page is None or page.missing or day_start(day) + FINAL_AFTER > as_of:
            return False
        day += timedelta(days=1)
    return True


def weak_label_of(
    spike: SpikeRecord, portal: Sequence[PortalDay], as_of: datetime
) -> tuple[bool | None, list[date], bool]:
    """(weak label, the portal days linking the article, whether the portal days are final).

    True: linked from a portal day before `as_of` (a link is near-certain news even on a
    page still being written). False: not linked, and every portal day it depends on was
    final at `as_of`. None: not linked *yet*; the answer isn't known at `as_of`.
    """
    days = portal_days_of(spike, portal, as_of)
    final = portal_final(spike, portal, as_of)
    if days:
        return True, days, final
    return (False if final else None), days, final


def label_of(spike: SpikeRecord, labels: Sequence[Label], as_of: datetime) -> Label | None:
    """The 02b label covering the spike (a human label beats an LLM label).

    Only labels of bursts that ended before `as_of` count: the labeler read the burst's
    edits up to its end, so a later end would put data of a later split into the label.
    """
    covering = [label for label in labels if label.covers(spike) and label.end_at < as_of]
    return pick_label(covering)


def rule_fired(spike: SpikeRecord, rule_spikes: Sequence[SpikeRecord], cutoff: datetime) -> bool:
    """Had the default detector started a spike on the page for this burst by the cutoff?

    Known at the cutoff: the rule spike started by then, and it had not ended before the
    candidate's burst began. This is the "current rule-based detector" baseline of 07b.
    """
    for rule in rule_spikes:
        if rule.started_at > cutoff:
            continue  # started too late to count
        if rule.ended_at is not None and rule.ended_at < spike.first_edit_at:
            continue  # an earlier burst that was over before this one began
        return True
    return False


UNKNOWN = "unknown"  # label_source of a row without a label


def combined_label(weak: bool | None, label: Label | None) -> tuple[int | None, str]:
    """The best available label (1 = news, 0 = not news, None = unknown) and its source.

    A portal link is near-certain news, so it wins. Otherwise a news / not news label
    (human, else LLM) decides. Otherwise the label is unknown: "not on the portal" is not
    "not news" (the LLM calls about half of those spikes news), so it never makes a 0.
    """
    if weak:
        return 1, "portal"
    if label is not None and label.label in ("news", "not news"):
        return int(label.label == "news"), label.labeler
    return None, UNKNOWN


@dataclass
class RowSources:
    """Everything `build_rows` reads besides the candidate spikes."""

    edits: dict[str, list[PageEdit]]
    snapshots: dict[int, PageSnapshot | None]  # by revision id
    rule_spikes: dict[str, list[SpikeRecord]]  # default-rule spikes by title
    portal: list[PortalDay]
    labels: list[Label]
    window_min: float


def build_row(spike: SpikeRecord, src: RowSources, label_as_of: datetime) -> dict[str, Any]:
    """One dataset row: identifiers, the features (leakage-checked), labels as of a moment.

    `label_as_of` bounds the labels' evidence: the start of the next split (or the
    build's as-of for the last split), so no label uses data from a later split.
    """
    window = window_of(spike, src.window_min)
    edits = src.edits.get(spike.title, [])
    revision = revision_at(edits, window.cutoff)
    page = src.snapshots.get(revision) if revision is not None else None
    features = check_no_leakage(window, edits, page)
    weak, portal_days, final = weak_label_of(spike, src.portal, label_as_of)
    label = label_of(spike, src.labels, label_as_of)
    value, source = combined_label(weak, label)
    started = spike.started_at.astimezone(UTC)
    return {
        "spike_id": f"{spike.title}|{started.isoformat()}",
        "title": spike.title,
        "first_edit_at": spike.first_edit_at.astimezone(UTC),
        "started_at": started,
        "cutoff": window.cutoff.astimezone(UTC),
        "day": started.date().isoformat(),
        **{name: float(value) for name, value in features.items()},
        "page_revision_id": revision,
        "page_snapshot": page is not None,
        "default_rule_fired": rule_fired(
            spike, src.rule_spikes.get(spike.title, []), window.cutoff
        ),
        "weak_label": weak,
        "weak_label_final": final,
        "portal_days": ",".join(d.isoformat() for d in portal_days),
        "label_02b": label.label if label else None,
        "label_02b_labeler": label.labeler if label else None,
        "label_02b_model": label.model if label else None,
        "label": value,
        "label_source": source,
        "label_as_of": label_as_of.astimezone(UTC),
    }


def build_rows(
    spikes: Sequence[SpikeRecord], src: RowSources, label_as_of: Callable[[SpikeRecord], datetime]
) -> list[dict[str, Any]]:
    """One dataset row per spike (features and label)."""
    rows = []
    for number, spike in enumerate(spikes, start=1):
        rows.append(build_row(spike, src, label_as_of(spike)))
        if number % 500 == 0:
            logger.info("Features: %d of %d spikes", number, len(spikes))
    return rows


# --- 3. split by time -----------------------------------------------------------------


def assign_splits(days: Iterable[str], valid_days: int, test_days: int) -> dict[str, str]:
    """Day -> split: the last `test_days` days test, the `valid_days` before them valid.

    By whole UTC days, oldest first, never at random: the model is always evaluated on
    days after everything it was trained on, as it will be used.
    """
    ordered = sorted(set(days))
    if len(ordered) <= valid_days + test_days:
        raise ValueError(
            f"{len(ordered)} days of spikes can't make {valid_days} validation and "
            f"{test_days} test days plus at least one training day"
        )
    test_from = len(ordered) - test_days
    valid_from = test_from - valid_days
    return {
        day: "train" if i < valid_from else "valid" if i < test_from else "test"
        for i, day in enumerate(ordered)
    }


def day_of(spike: SpikeRecord) -> str:
    """The UTC day a spike belongs to (its start), as the rows store it."""
    return spike.started_at.astimezone(UTC).date().isoformat()


def split_label_as_of(split_of: dict[str, str], as_of: datetime) -> dict[str, datetime]:
    """Per split, the moment its labels' evidence must come from before.

    The start (midnight UTC) of the next split that has days, and for the last split the
    build's `as_of`. So a training label never uses portal pages or edits of the
    validation or test days, and a validation label none of the test days.
    """
    starts = {}
    for day, split in split_of.items():
        moment = day_start(date.fromisoformat(day))
        starts[split] = min(starts.get(split, moment), moment)
    result = {}
    for i, split in enumerate(SPLITS):
        later = [starts[name] for name in SPLITS[i + 1 :] if name in starts]
        result[split] = min([as_of, *later])
    return result


def purge_boundaries(
    spikes: Sequence[SpikeRecord],
    split_of: dict[str, str],
    label_as_of: dict[str, datetime],
    gap: timedelta,
) -> tuple[list[SpikeRecord], dict[str, dict[str, int]]]:
    """Leave out the spikes that tie a split to a later one: (kept, counts per reason).

    - *running*: still running when the next split began (its edits, and the burst a
      labeler reads, lie on both sides of the boundary);
    - *same page*: its page spikes again in a later split less than `gap` after it
      ended (one story on both sides: the model would meet it in training and be
      tested on its continuation).

    The earlier spike goes, the later one stays: evaluation sets keep their size, and the
    later spike's features may legitimately see the earlier one as page history.
    """
    order = {name: i for i, name in enumerate(SPLITS)}
    by_title: dict[str, list[SpikeRecord]] = defaultdict(list)
    for spike in spikes:
        by_title[spike.title].append(spike)
    kept = []
    purged: dict[str, Counter[str]] = {"running": Counter(), "same_page": Counter()}
    for spike in spikes:
        split = split_of[day_of(spike)]
        end = spike_end(spike)
        if end >= label_as_of[split]:
            purged["running"][split] += 1
            continue
        if any(
            order[split_of[day_of(other)]] > order[split] and other.first_edit_at - end < gap
            for other in by_title[spike.title]
        ):
            purged["same_page"][split] += 1
            continue
        kept.append(spike)
    return kept, {reason: dict(counts) for reason, counts in purged.items()}


def split_rows(
    rows: Sequence[dict[str, Any]], split_of: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    """Rows by split (`split_of`: day -> split, from `assign_splits`), oldest first."""
    splits: dict[str, list[dict[str, Any]]] = {name: [] for name in SPLITS}
    for row in sorted(rows, key=lambda r: (r["started_at"], r["title"])):
        splits[split_of[row["day"]]].append(row)
    return splits


# --- 6. versioned storage ------------------------------------------------------------

ID_FIELDS = [
    pa.field("spike_id", pa.string(), nullable=False),
    pa.field("title", pa.string(), nullable=False),
    pa.field("first_edit_at", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("started_at", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("cutoff", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("day", pa.string(), nullable=False),
]
META_FIELDS = [
    pa.field("page_revision_id", pa.int64()),
    pa.field("page_snapshot", pa.bool_(), nullable=False),
    pa.field("default_rule_fired", pa.bool_(), nullable=False),
]
LABEL_FIELDS = [
    pa.field("weak_label", pa.bool_()),  # null: not on the portal yet, days not final
    pa.field("weak_label_final", pa.bool_(), nullable=False),
    pa.field("portal_days", pa.string(), nullable=False),
    pa.field("label_02b", pa.string()),
    pa.field("label_02b_labeler", pa.string()),
    pa.field("label_02b_model", pa.string()),
    pa.field("label", pa.int8()),  # null: unknown
    pa.field("label_source", pa.string(), nullable=False),
    pa.field("label_as_of", pa.timestamp("us", tz="UTC"), nullable=False),
]
DATASET_SCHEMA = pa.schema(
    [
        *ID_FIELDS,
        *(pa.field(name, pa.float64(), nullable=False) for name in FEATURE_NAMES),
        *META_FIELDS,
        *LABEL_FIELDS,
    ]
)
MANIFEST = "manifest.json"


def rows_to_table(rows: Sequence[dict[str, Any]]) -> pa.Table:
    """The rows as an Arrow table in the dataset's schema."""
    columns = {name: [row[name] for row in rows] for name in DATASET_SCHEMA.names}
    return pa.Table.from_pydict(columns, schema=DATASET_SCHEMA)


def parquet_bytes(table: pa.Table) -> bytes:
    """A table as zstd-compressed Parquet bytes."""
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="zstd")
    return sink.getvalue()


class DatasetStore:
    """Dataset versions under `root` ('<bucket>/<prefix>' in S3, or a local folder).

    A version is a folder `v<n>` holding one Parquet file per split and `manifest.json`.
    The manifest is written last, so a version without one is incomplete and ignored.
    Versions are never overwritten: every build gets the next number.
    """

    def __init__(self, filesystem: pafs.FileSystem, root: str) -> None:
        self.fs = filesystem
        local = filesystem.type_name == "local"
        self.root = (root.rstrip("/") or "/") if local else root.strip("/")

    @classmethod
    def from_settings(cls, settings: Settings) -> "DatasetStore":
        lake = LakeStore.from_settings(settings, settings.ml_dataset_prefix)  # creates the bucket
        return cls(lake.fs, lake.root)

    def versions(self) -> list[int]:
        """Complete versions (those with a manifest), oldest first."""
        if self.fs.get_file_info(self.root).type == pafs.FileType.NotFound:
            return []
        found = []
        for info in self.fs.get_file_info(pafs.FileSelector(self.root)):
            name = info.base_name
            if info.type == pafs.FileType.Directory and name.startswith("v") and name[1:].isdigit():
                manifest = self.fs.get_file_info(f"{info.path}/{MANIFEST}")
                if manifest.type == pafs.FileType.File:
                    found.append(int(name[1:]))
        return sorted(found)

    def _next_version(self) -> int:
        taken = [
            int(info.base_name[1:])
            for info in self._listing()
            if info.base_name.startswith("v") and info.base_name[1:].isdigit()
        ]
        return max(taken, default=0) + 1

    def _listing(self) -> list[pafs.FileInfo]:
        if self.fs.get_file_info(self.root).type == pafs.FileType.NotFound:
            return []
        return self.fs.get_file_info(pafs.FileSelector(self.root))

    def _put(self, path: str, data: bytes) -> None:
        folder = path.rsplit("/", 1)[0]
        self.fs.create_dir(folder, recursive=True)
        with self.fs.open_output_stream(path) as out:
            out.write(data)

    def write(self, splits: dict[str, pa.Table], manifest: dict[str, Any]) -> str:
        """Store a new version; returns its name ('v3'). Adds file hashes to the manifest."""
        version = f"v{self._next_version()}"
        files = {}
        for name, table in splits.items():
            data = parquet_bytes(table)
            self._put(f"{self.root}/{version}/{name}.parquet", data)
            files[f"{name}.parquet"] = {
                "rows": table.num_rows,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        fingerprint = hashlib.sha256(
            "".join(f"{n}:{f['sha256']}" for n, f in sorted(files.items())).encode()
        ).hexdigest()
        manifest = {"version": version, **manifest, "files": files, "fingerprint": fingerprint}
        text = json.dumps(manifest, indent=2, ensure_ascii=False, default=str)
        self._put(f"{self.root}/{version}/{MANIFEST}", text.encode("utf-8"))
        return version

    def manifest(self, version: str) -> dict[str, Any]:
        with self.fs.open_input_stream(f"{self.root}/{version}/{MANIFEST}") as stream:
            return json.loads(stream.read().decode("utf-8"))

    def read(self, version: str, split: str) -> pa.Table:
        """One split of a version (for training in spec 07b)."""
        return pq.read_table(f"{self.root}/{version}/{split}.parquet", filesystem=self.fs)


# --- the manifest ---------------------------------------------------------------------


def balance(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Row count and label balance of one split."""
    n = len(rows)
    weak = sum(r["weak_label"] is True for r in rows)
    weak_negative = sum(r["weak_label"] is False for r in rows)
    positives = sum(r["label"] == 1 for r in rows)
    negatives = sum(r["label"] == 0 for r in rows)
    labeled = [r for r in rows if r["label_02b"] is not None]
    news = sum(r["label_02b"] == "news" for r in labeled)
    not_news = sum(r["label_02b"] == "not news" for r in labeled)
    days = sorted({r["day"] for r in rows})
    return {
        "rows": n,
        "days": days,
        "first_started_at": min((r["started_at"] for r in rows), default=None),
        "last_started_at": max((r["started_at"] for r in rows), default=None),
        "weak_label": {
            "positive": weak,
            "negative": weak_negative,
            "unknown": n - weak - weak_negative,
            "positive_share": share(weak, weak + weak_negative),
        },
        "weak_label_final_rows": sum(r["weak_label_final"] for r in rows),
        "label_02b": {
            "labeled": len(labeled),
            "news": news,
            "not news": not_news,
            "unsure": len(labeled) - news - not_news,
            "news_share": share(news, news + not_news),
            "labelers": dict(Counter(r["label_02b_labeler"] for r in labeled)),
        },
        "label": {
            "positive": positives,
            "negative": negatives,
            "unknown": n - positives - negatives,
            "positive_share": share(positives, positives + negatives),
            "sources": dict(Counter(r["label_source"] for r in rows)),
        },
        "default_rule_fired": sum(r["default_rule_fired"] for r in rows),
        "page_snapshots": sum(r["page_snapshot"] for r in rows),
    }


def share(part: int, whole: int) -> float | None:
    """`part / whole` rounded to four decimals, or None when `whole` is 0."""
    return round(part / whole, 4) if whole else None


def file_sha256(path: Path) -> str | None:
    """The SHA-256 of a file, or None if it doesn't exist."""
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def jsonable(value: Any) -> Any:
    """NaN is not valid JSON; the manifest stores it as null."""
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


# --- the command line -----------------------------------------------------------------


def parse_overrides(text: str) -> dict[str, str]:
    """'min_edits=4,min_editors=3' -> {'min_edits': '4', 'min_editors': '3'}."""
    return dict(item.split("=", 1) for item in text.split(",") if item.strip())


def candidate_config(settings: Settings, overrides: str) -> DetectorConfig:
    """The candidate detector's settings: the defaults with `overrides`."""
    base = settings.detector_config().model_dump()
    return DetectorConfig.model_validate({**base, **parse_overrides(overrides)})


def store_runs(settings: Settings, runs: dict[str, list[SpikeRecord]]) -> None:
    """Replace the runs' spikes in Postgres (so the label tool can sample them)."""
    import psycopg

    from wikipulse.db.spikes import delete_run, upsert_spikes

    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        for run_id, spikes in runs.items():
            delete_run(conn, run_id)
            upsert_spikes(conn, run_id, spikes)
            logger.info("Stored %d spikes as run %s", len(spikes), run_id)


def fetch_snapshots(
    settings: Settings,
    edits: dict[str, list[PageEdit]],
    spikes: Sequence[SpikeRecord],
    window_min: float,
) -> tuple[dict[int, PageSnapshot | None], dict[str, int]]:
    """Each spike's page at its cutoff revision (MediaWiki API, cached in data/cache/ml)."""
    from wikipulse.eval.wiki_client import WikiClient
    from wikipulse.ml.pages import (
        RevisionCache,
        fetch_creation_times,
        fetch_revisions,
        snapshot_of,
    )

    revids = set()
    for spike in spikes:
        cutoff = window_of(spike, window_min).cutoff
        revision = revision_at(edits.get(spike.title, []), cutoff)
        if revision is not None:
            revids.add(revision)
    cache = settings.data_dir / "cache" / "ml"
    with WikiClient(settings.user_agent, cache, settings.wiki_api_min_interval_s) as client:
        cached = RevisionCache(cache / "revisions")
        revisions = fetch_revisions(client, settings.wiki_api_url, revids, cached)
        pageids = {r.pageid for r in revisions.values() if r.pageid is not None}
        created = fetch_creation_times(client, settings.wiki_api_url, pageids)
        stats = {"requests": client.requests, "cache_hits": client.cache_hits}
    snapshots = {
        revid: snapshot_of(rev, created.get(rev.pageid) if rev.pageid else None)
        for revid, rev in revisions.items()
    }
    stats.update(revisions_wanted=len(revids), revisions_found=len(revisions))
    logger.info("Page snapshots: %s", stats)
    return snapshots, stats


def fetch_portal(settings: Settings, first: date, last: date, as_of: datetime) -> list[PortalDay]:
    """Portal days up to the as-of moment (cached in data/ground_truth, as in spec 02b).

    Days that begin at or after `as_of` are not fetched: no label may use them.
    """
    from wikipulse.eval.detection import load_portal
    from wikipulse.eval.wiki_client import WikiClient

    cache = settings.data_dir / "ground_truth"
    last_day = (as_of - timedelta(microseconds=1)).astimezone(UTC).date()
    with WikiClient(settings.user_agent, cache, settings.wiki_api_min_interval_s) as client:
        return load_portal(client, settings.wiki_api_url, first, last, last_day)


def portal_status(portal: Sequence[PortalDay], as_of: datetime) -> dict[str, str]:
    """Per portal day: 'final', 'provisional' (younger than 3 days at `as_of`) or 'missing'."""
    status = {}
    for page in portal:
        settled = day_start(page.day) + FINAL_AFTER
        state = "missing" if page.missing else "final" if settled <= as_of else "provisional"
        status[page.day.isoformat()] = state
    return status


def portal_sha256(portal: Sequence[PortalDay]) -> str:
    """A hash of the portal links the labels were made from (they change until final)."""
    content = [
        [p.day.isoformat(), p.missing, sorted(p.links), sorted(p.leads), sorted(p.resolve.items())]
        for p in sorted(portal, key=lambda p: p.day)
    ]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode("utf-8")).hexdigest()


def parse_moment(text: str) -> datetime:
    """'2026-11-13' or '2026-11-13T06:00' (UTC unless an offset is given)."""
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def resolve_as_of(as_of: datetime | None, data_end: datetime, now: datetime) -> datetime:
    """The moment labels are taken as of: `as_of`, by default the end of the data.

    Not the wall clock, so rebuilding later gives the same labels. It may not lie before
    the data's end (a label would be older than its spike) or in the future (portal pages
    from then can't have been read). `now` is only used for that check.
    """
    moment = as_of or data_end
    if moment < data_end:
        raise ValueError(f"--as-of {moment} is before the end of the data ({data_end})")
    if moment > now:
        raise ValueError(f"--as-of {moment} is in the future")
    return moment


@dataclass
class BuildOptions:
    """What `build` needs besides the settings (the CLI arguments; tests set them)."""

    lake_prefix: str | None = None
    candidate: str | None = None  # detector changes for the candidates
    run_id: str = CANDIDATE_RUN
    rule_run_id: str = RULE_RUN
    labels: Path = Path("evals") / "labels" / "spike_labels.jsonl"
    use_db: bool = True
    use_pages: bool = True
    lake: LakeStore | None = None  # tests pass a local lake ...
    datasets: "DatasetStore | None" = None  # ... a local dataset store ...
    portal: list[PortalDay] | None = None  # ... and portal days, so nothing goes online
    dry_run: bool = False  # build and report, but store nothing (no version, no Postgres)
    as_of: datetime | None = None  # labels as of this moment (default: the data's end)


def build(options: BuildOptions, settings: Settings) -> str:
    """The whole build (see the module docstring); returns the new version's name."""
    from wikipulse.eval.detection import git_commit
    from wikipulse.eval.label_store import load_labels

    clock = time.monotonic()
    built_at = datetime.now(UTC)  # wall clock: metadata only, never in the rows
    window_min = settings.feature_window_min
    lake = options.lake or LakeStore.from_settings(
        settings, options.lake_prefix or settings.ml_lake_prefix
    )
    hours, missing = contiguous_hours(lake.hours())
    if not hours:
        raise ValueError(f"no edits in the lake at {lake.root}")
    if missing:
        logger.warning(
            "Gaps in the lake; using the hours from %s on (gaps at %s)", hours[0], missing
        )
    data_start, data_end = hours[0], hours[-1] + timedelta(hours=1)
    as_of = resolve_as_of(options.as_of, data_end, built_at)
    logger.info("Edits: %d hours from %s to %s in %s", len(hours), data_start, data_end, lake.root)

    candidates = candidate_config(settings, options.candidate or settings.ml_candidate_detector)
    configs = {options.run_id: candidates, options.rule_run_id: settings.detector_config()}
    counter: Counter[str] = Counter()

    def counted(events: Iterable[EditEvent]) -> Iterator[EditEvent]:
        for event in events:
            counter["edits"] += 1
            counter["human_edits"] += not event.is_bot
            yield event

    runs = detect_spikes(counted(lake_events(lake, hours)), configs)
    if options.use_db and not options.dry_run:
        store_runs(settings, runs)
    warmup = timedelta(hours=settings.ml_warmup_hours)
    selected = select_spikes(runs[options.run_id], data_start, data_end, warmup, window_min)
    split_of = assign_splits(
        (day_of(s) for s in selected), settings.ml_valid_days, settings.ml_test_days
    )
    split_as_of = split_label_as_of(split_of, as_of)
    gap = timedelta(hours=settings.ml_split_gap_hours)
    spikes, purged = purge_boundaries(selected, split_of, split_as_of, gap)
    logger.info(
        "Using %d of %d candidate spikes (%d selected; purged at split boundaries: %s)",
        len(spikes),
        len(runs[options.run_id]),
        len(selected),
        purged,
    )

    edits = page_edits(lake, {s.title for s in spikes})
    snapshots: dict[int, PageSnapshot | None] = {}
    page_stats: dict[str, int] = {}
    if options.use_pages:
        snapshots, page_stats = fetch_snapshots(settings, edits, spikes, window_min)
    margin = timedelta(days=PORTAL_MARGIN_DAYS)
    portal = options.portal
    if portal is None:
        first, last = (data_start - margin).date(), (data_end + margin).date()
        portal = fetch_portal(settings, first, last, as_of)
    rule_by_title: dict[str, list[SpikeRecord]] = defaultdict(list)
    for spike in runs[options.rule_run_id]:
        rule_by_title[spike.title].append(spike)
    labels = load_labels(options.labels)
    sources = RowSources(edits, snapshots, rule_by_title, portal, labels, window_min)
    # check_no_leakage runs on every row; labels use only evidence from before the next split
    rows = build_rows(spikes, sources, lambda s: split_as_of[split_of[day_of(s)]])
    splits = split_rows(rows, split_of)

    manifest = {
        "created_at": built_at.isoformat(),
        "git": git_commit(),
        "command": " ".join(sys.argv),
        "source": {
            "lake": lake.root,
            "first_hour": data_start.isoformat(),
            "end": data_end.isoformat(),
            "hours": len(hours),
            "gaps_before_first_hour": [h.isoformat() for h in missing],
            **counter,
        },
        "date_range": {
            "first_day": min((r["day"] for r in rows), default=None),
            "last_day": max((r["day"] for r in rows), default=None),
        },
        "feature_window_min": window_min,
        "warmup_hours": settings.ml_warmup_hours,
        "detector": {
            "candidates": {"run_id": options.run_id, "config": candidates.model_dump()},
            "default_rule": {
                "run_id": options.rule_run_id,
                "config": settings.detector_config().model_dump(),
            },
            "spikes_detected": {name: len(found) for name, found in runs.items()},
            "candidate_spikes_selected": len(selected),
            "candidate_spikes_used": len(spikes),
        },
        "labels": {
            "as_of": as_of.isoformat(),
            "as_of_from": "--as-of" if options.as_of else "end of the data",
            "weak_label": (
                "true: article linked from Portal:Current events within 48 h of the spike, "
                "on a portal day before label_as_of; false: not linked and all those portal "
                "days final at label_as_of; null: not linked, days not final yet"
            ),
            "weak_label_final": "all portal days the weak label depends on final at label_as_of",
            "label_02b": (
                "LLM or human label covering the spike (a human label wins), of a burst "
                "that ended before label_as_of"
            ),
            "label": "1 if weak_label; else label_02b news / not news; else null (unknown)",
            "label_as_of": "the start of the next split; for the last split labels.as_of",
            "labels_file": options.labels.as_posix(),
            "labels_file_sha256": file_sha256(options.labels),
            "portal_days": portal_status(portal, as_of),
            "portal_sha256": portal_sha256(portal),
        },
        "split": {
            "method": "by UTC day of started_at: last days test, the days before them valid",
            "valid_days": settings.ml_valid_days,
            "test_days": settings.ml_test_days,
            "label_as_of": {name: moment.isoformat() for name, moment in split_as_of.items()},
            "gap_hours": settings.ml_split_gap_hours,
            "purged": {
                "running": "spike still running when the next split began",
                "same_page": "same page spiked again in a later split within gap_hours",
                "rows": purged,
            },
        },
        "rows": len(rows),
        "splits": {name: balance(split) for name, split in splits.items()},
        "features": FEATURES,
        "columns": DATASET_SCHEMA.names,
        "page_snapshots": page_stats or "skipped",
        "leakage_check": {"rows_checked": len(rows), "passed": True},
    }
    tables = {name: rows_to_table(split) for name, split in splits.items()}
    manifest["build_seconds"] = round(time.monotonic() - clock, 1)
    if options.dry_run:
        report = summary(jsonable({"version": "(dry run)", **manifest}))
        logger.info("Dry run, nothing stored:\n%s", report)
        return "(dry run)"
    datasets = options.datasets or DatasetStore.from_settings(settings)
    version = datasets.write(tables, jsonable(manifest))
    logger.info("Wrote dataset %s to %s: %d rows", version, datasets.root, len(rows))
    return version


def summary(manifest: dict[str, Any]) -> str:
    """A few lines about one version, for `list` and the end of `build`."""
    lines = [
        f"{manifest['version']}  built {manifest['created_at'][:16]}  "
        f"commit {(manifest['git'].get('commit') or '?')[:10]}"
        f"{' (dirty)' if manifest['git'].get('dirty') else ''}  "
        f"{manifest['rows']} rows  {manifest['date_range']['first_day']} to "
        f"{manifest['date_range']['last_day']}"
    ]
    for name, split in manifest["splits"].items():
        weak, label, b02 = split["weak_label"], split["label"], split["label_02b"]
        lines.append(
            f"  {name:5}  {split['rows']:5} rows  {len(split['days'])} days  "
            f"weak +{weak['positive']} -{weak['negative']} ?{weak.get('unknown', 0)}  "
            f"02b labeled {b02['labeled']} (news {b02['news']}, not news {b02['not news']})  "
            f"label +{label['positive']} -{label['negative']} ?{label.get('unknown', 0)} "
            f"({pct(label['positive_share'])} of known positive)"
        )
    return "\n".join(lines)


def pct(value: float | None) -> str:
    """A share as a percentage with one decimal, or `n/a`."""
    return "n/a" if value is None else f"{100 * value:.1f}%"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build", help="build a new dataset version")
    make.add_argument("--lake-prefix", help="lake prefix to read (default: ML_LAKE_PREFIX)")
    make.add_argument("--candidate", help="detector changes, e.g. min_edits=4,min_editors=3")
    make.add_argument("--run-id", default=CANDIDATE_RUN, help="Postgres run of the candidates")
    make.add_argument("--rule-run-id", default=RULE_RUN, help="Postgres run of the default rule")
    make.add_argument("--labels", type=Path, default=BuildOptions.labels)
    make.add_argument("--no-db", action="store_true", help="don't store the runs in Postgres")
    make.add_argument("--no-pages", action="store_true", help="skip the page snapshots (API)")
    make.add_argument(
        "--dry-run", action="store_true", help="report only: no version, nothing in Postgres"
    )
    make.add_argument(
        "--as-of",
        type=parse_moment,
        help="labels as of this UTC moment, e.g. 2026-11-13 (default: the end of the data)",
    )
    show = commands.add_parser("list", help="list the dataset versions")
    show.add_argument("--json", action="store_true", help="print the full manifests")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.command == "build":
        logging.getLogger("wikipulse.detect").setLevel(logging.WARNING)  # a line per spike
        options = BuildOptions(
            lake_prefix=args.lake_prefix,
            candidate=args.candidate,
            run_id=args.run_id,
            rule_run_id=args.rule_run_id,
            labels=args.labels,
            use_db=not args.no_db,
            use_pages=not args.no_pages,
            dry_run=args.dry_run,
            as_of=args.as_of,
        )
        version = build(options, settings)
        if not options.dry_run:
            print(summary(DatasetStore.from_settings(settings).manifest(version)))
        return 0
    store = DatasetStore.from_settings(settings)
    for number in store.versions():
        manifest = store.manifest(f"v{number}")
        print(json.dumps(manifest, indent=2) if args.json else summary(manifest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
