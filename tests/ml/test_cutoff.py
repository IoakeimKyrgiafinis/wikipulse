"""Spec 11a: training rows and served rows share one feature code path.

1. The shared-features test: the dataset code and the serving code, given the same spike
   and the same edits, produce identical features (and identical model inputs).
2. The impossible-values test: on a synthetic day, no `serving`-mode training row has a
   feature value the live scorer can't produce for the same detector settings (such as
   `edits_before_start` below the default threshold), while the old `candidates` mode
   does have such rows (so the test can fail).
"""

import itertools
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pandas as pd
import pyarrow.fs as pafs
import pytest

from tests.eval.helpers import at, spike
from tests.lake.helpers import local_store
from tests.ml.test_dataset import burst, ticks
from tests.ml.test_features import FIRST, START, synthetic_spike
from wikipulse.config import Settings
from wikipulse.db.scores import SpikeToScore
from wikipulse.eval.ground_truth import PortalDay
from wikipulse.lake.table import events_to_table
from wikipulse.ml.cutoff import at_cutoff, features_at_cutoff, revision_at
from wikipulse.ml.dataset import (
    SPLITS,
    BuildOptions,
    DatasetStore,
    RowSources,
    assign_final_splits,
    build,
    build_row,
    page_edits,
)
from wikipulse.ml.features import FEATURE_NAMES, PageSnapshot
from wikipulse.ml.models import INPUT_COLUMNS, RULE_COLUMN, TEXT_COLUMN
from wikipulse.ml.pages import WikiPages
from wikipulse.ml.serve import LiveScorer, LoadedModel, ModelHolder, score_spikes
from wikipulse.stream.models import EditEvent

TITLE = "2026 Example election"
WINDOW_MIN = 10.0


class CapturingModel:
    """A stand-in for the MLflow model: records the input table, scores 0.5."""

    def __init__(self) -> None:
        self.frames: list[pd.DataFrame] = []

    def predict(self, frame: pd.DataFrame) -> pd.DataFrame:
        self.frames.append(frame.copy())
        return pd.DataFrame({"score": [0.5] * len(frame), "is_news": [True] * len(frame)})


class SnapshotPages:
    """Page snapshots without the MediaWiki API (the same ones the dataset row gets)."""

    def __init__(self, snapshots: dict[int, PageSnapshot | None]) -> None:
        self.by_revision = snapshots

    def snapshots(self, revids):
        return {r: self.by_revision.get(r) for r in revids}

    def leads(self, revids):
        raise AssertionError("a model without text must not ask for leads")


def served(spikes: list[SpikeToScore], edits_of, snapshots) -> tuple[list, pd.DataFrame]:
    """Score spikes the way wikipulse.ml.serve does; (scores, the model's input table)."""
    model = CapturingModel()
    loaded = LoadedModel("m", "1", model, threshold=0.5, needs_text=False)
    scores = score_spikes(loaded, spikes, edits_of, SnapshotPages(snapshots), WINDOW_MIN, None)
    return scores, model.frames[0]


def same(a: float, b: float) -> bool:
    return (math.isnan(a) and math.isnan(b)) or a == b


# --- 1. one code path -----------------------------------------------------------------


def test_dataset_and_serving_compute_identical_features_for_the_same_spike():
    edits = synthetic_spike()
    record = spike(TITLE, START, delay_min=8, duration_h=2)
    assert record.first_edit_at == FIRST
    cutoff = START + timedelta(minutes=WINDOW_MIN)
    revision = revision_at(edits, cutoff)
    assert revision is not None
    snapshot = PageSnapshot(
        revision, cutoff - timedelta(minutes=1), ("2026 elections", "Living people"), ("current",)
    )
    snapshots: dict[int, PageSnapshot | None] = {revision: snapshot}

    sources = RowSources({TITLE: edits}, snapshots, {TITLE: [record]}, [], [], WINDOW_MIN)
    row = build_row(record, sources, at(20))

    scores, frame = served(
        [SpikeToScore(1, "live", TITLE, FIRST, START)], lambda t: edits, snapshots
    )
    (score,) = scores

    assert row["cutoff"] == score.cutoff
    assert row["page_revision_id"] == score.page_revision_id == revision
    assert list(score.features) == FEATURE_NAMES
    assert all(same(row[name], score.features[name]) for name in FEATURE_NAMES)
    # the model sees the same input row in training and serving (the text is the title
    # for models without text in both)
    served_row = frame.iloc[0]
    assert all(same(row[name], served_row[name]) for name in FEATURE_NAMES)
    assert row[RULE_COLUMN] is True and bool(served_row[RULE_COLUMN]) is True
    assert served_row[TEXT_COLUMN] == row["title"]
    assert list(frame.columns) == INPUT_COLUMNS
    # and the page features came from the snapshot, not NaN
    assert row["cat_living_people"] == 1.0 and row["has_current_template"] == 1.0


def test_both_sides_call_the_shared_functions(monkeypatch):
    """A change to the shared code reaches both: no private copy on either side."""
    import wikipulse.ml.dataset as dataset_module
    import wikipulse.ml.serve as serve_module

    calls = []

    def spy(item, snapshots):
        calls.append(item.window.title)
        return features_at_cutoff(item, snapshots)

    monkeypatch.setattr(dataset_module, "features_at_cutoff", spy)
    monkeypatch.setattr(serve_module, "features_at_cutoff", spy)
    edits = synthetic_spike()
    record = spike(TITLE, START, delay_min=8)
    build_row(record, RowSources({TITLE: edits}, {}, {}, [], [], WINDOW_MIN), at(20))
    served([SpikeToScore(1, "live", TITLE, FIRST, START)], lambda t: edits, {})
    assert calls == [TITLE, TITLE]
    item = at_cutoff(TITLE, FIRST, START, edits, WINDOW_MIN)
    assert item.window.cutoff == START + timedelta(minutes=WINDOW_MIN)


# --- 1b. end to end: the dataset from the lake, the live scorer from the stream ---------
#
# The tests above hand both sides the same edits list and snapshot dict. This one runs
# the production paths from their real inputs: `build` reads a lake (`page_edits`) and
# fetches snapshots with `WikiPages`; the `LiveScorer` reads the same edits from a fake
# Redis stream an hour at a time into its pruning `EditBuffer`, polls a fake Postgres for
# due spikes, and fetches snapshots with its own `WikiPages`. Only the I/O is faked: the
# stream, the spikes table and the MediaWiki API (an `httpx.MockTransport`).


def e2e_events() -> list[EditEvent]:
    """1 to 7 Nov: bot ticks; from the 2nd each day a burst on a page that bursts daily
    (its 24 h history matters), a news burst with two edits the evening before, and a
    slow burst only the long-window rule catches. Revision ids are unique."""
    events = [e for d in range(1, 8) for e in ticks(d)]
    revision = iter(range(500_000, 600_000))

    def edit(title: str, when: datetime, user: str, comment: str = "update") -> EditEvent:
        rev = next(revision)
        return EditEvent(
            event_id=f"e2e-{rev}",
            event_time=when,
            received_at=when,
            title=title,
            type="edit",
            user=user,
            is_bot=False,
            is_minor=False,
            rev_old=rev - 1,
            rev_new=rev,
            len_old=3000,
            len_new=3080,
            comment=comment,
        )

    for d in range(2, 8):
        daily = at(d, 13 - d)  # 23 hours after the day before's burst
        events += [edit("Daily", daily + timedelta(minutes=2 * i), f"D{i}") for i in range(6)]
        events += [edit(f"News {d}", at(d - 1, 20 + i), f"Early {i}") for i in range(2)]
        news = at(d, 15)
        events += [
            edit(f"News {d}", news + timedelta(minutes=2 * i), f"N{i}", "died") for i in range(7)
        ]
        slow = at(d, 20)
        events += [
            edit(f"Slow {d}", slow + timedelta(minutes=6 * i), f"S{i % 4}") for i in range(12)
        ]
    return sorted(events, key=lambda e: (e.event_time, e.event_id))


def wiki_api(events: list[EditEvent]) -> httpx.MockTransport:
    """The MediaWiki API for these edits: each revision's wikitext and page creation."""
    by_revision = {e.rev_new: e for e in events if e.rev_new is not None}
    page_ids = {title: 9000 + i for i, title in enumerate(sorted({e.title for e in events}))}

    def revision_page(revid: int) -> dict:
        e = by_revision[revid]
        text = "{{Current}}\n[[Category:Living people]]" if "News" in e.title else "Text."
        return {
            "pageid": page_ids[e.title],
            "title": e.title,
            "revisions": [
                {
                    "revid": revid,
                    "timestamp": e.event_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "slots": {"main": {"content": text}},
                }
            ],
        }

    def answer(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "revids" in params:
            pages = [revision_page(int(r)) for r in params["revids"].split("|")]
            return httpx.Response(200, json={"query": {"pages": pages}})
        pageid = int(params["pageids"])
        first = {"revid": 1, "timestamp": f"20{pageid % 20 + 1:02d}-01-01T00:00:00Z"}
        page = {"pageid": pageid, "revisions": [first]}
        return httpx.Response(200, json={"query": {"pages": [page]}})

    return httpx.MockTransport(answer)


class FakeStream:
    """A Redis stream that gains one more hour of edits each time a reader waits."""

    def __init__(self, events: list[EditEvent]) -> None:
        self.entries = [
            (f"{int(e.event_time.timestamp() * 1000)}-{i}", {"data": e.model_dump_json()})
            for i, e in enumerate(events)
        ]
        self.times = [e.event_time for e in events]
        self.visible = 0
        self.reveal()

    def reveal(self) -> None:
        if self.visible >= len(self.entries):
            return
        until = self.times[self.visible].replace(minute=0, second=0) + timedelta(hours=1)
        while self.visible < len(self.entries) and self.times[self.visible] < until:
            self.visible += 1

    @staticmethod
    def key(entry_id: str) -> tuple[int, int]:
        ms, seq = entry_id.split("-")
        return int(ms), int(seq)

    def unread(self, last: str) -> list:
        return [e for e in self.entries[: self.visible] if self.key(e[0]) > self.key(last)]

    def xrevrange(self, stream, count):
        return [self.entries[self.visible - 1]] if self.visible else []

    def xread(self, streams, count, block=None):
        (last,) = streams.values()
        new = self.unread(last)
        if not new and block is not None:  # waiting: the next hour arrives
            self.reveal()
            new = self.unread(last)
        return [("stream", new[:count])] if new else []


class FakeSpikesTable:
    """`spikes_to_score` and `upsert_scores` over the detector's spikes, in memory."""

    def __init__(self, spikes: list[SpikeToScore], window_min: float) -> None:
        self.spikes = spikes
        self.window = timedelta(minutes=window_min)
        self.scores: dict[int, Any] = {}

    def to_score(self, conn, run_id, window_min, cutoff_until, first_edit_from=None, **_):
        return [
            s
            for s in self.spikes
            if s.id not in self.scores
            and s.started_at + self.window <= cutoff_until
            and (first_edit_from is None or s.first_edit_at >= first_edit_from)
        ]

    def upsert(self, conn, scores) -> int:
        for score in scores:
            self.scores[score.spike_id] = score
        return len(scores)


def e2e_settings(folder) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=folder,
        wiki_api_min_interval_s=0,
        ml_valid_days=1,
        ml_test_days=2,
        ml_warmup_hours=24,
    )


def test_lake_dataset_and_stream_scorer_agree_end_to_end(tmp_path, monkeypatch):
    import wikipulse.ml.serve as serve_module

    events = e2e_events()
    api = wiki_api(events)
    lake = local_store(tmp_path / "lake")
    by_hour: dict[datetime, list[EditEvent]] = {}
    for event in events:
        by_hour.setdefault(event.event_time.replace(minute=0, second=0), []).append(event)
    for hour, rows in by_hour.items():
        lake.write_hour(hour, events_to_table(rows))

    # training: the serving-mode dataset, from the lake, snapshots through WikiPages
    store = DatasetStore(pafs.LocalFileSystem(), (tmp_path / "datasets").as_posix())
    settings = e2e_settings(tmp_path / "dataset")
    options = BuildOptions(
        labels=tmp_path / "labels.jsonl",
        use_db=False,
        lake=lake,
        datasets=store,
        portal=[PortalDay(date(2026, 10, 29) + timedelta(days=d), [], []) for d in range(14)],
        as_of=datetime(2026, 11, 13, tzinfo=UTC),
        pages=WikiPages(settings, api),
    )
    version = build(options, settings)
    rows = [r for split in SPLITS for r in store.read(version, split).to_pylist()]
    assert {r["title"].rsplit(" ", 1)[0] for r in rows} == {"Daily", "News", "Slow"}
    assert all(r["page_snapshot"] for r in rows)  # every page came through the fake API

    # serving: the live scorer, reading the same edits from the stream
    serve_settings = e2e_settings(tmp_path / "serve")
    window_min = serve_settings.feature_window_min
    table = FakeSpikesTable(
        [
            SpikeToScore(i, "live", r["title"], r["first_edit_at"], r["started_at"])
            for i, r in enumerate(rows)
        ],
        window_min,
    )
    monkeypatch.setattr(serve_module, "spikes_to_score", table.to_score)
    monkeypatch.setattr(serve_module, "upsert_scores", table.upsert)
    monkeypatch.setattr(serve_module, "record_score_metrics", lambda scores: None)
    model = CapturingModel()
    holder = ModelHolder(
        "m",
        resolve=lambda name, alias: "1",
        load=lambda name, version: LoadedModel(name, version, model, 0.5, needs_text=False),
    )
    clock = itertools.count()
    scorer = LiveScorer(
        FakeStream(events),
        conn=None,
        stream="stream",
        run_id="live",
        holder=holder,
        pages=WikiPages(serve_settings, api),
        window_min=window_min,
        lateness=timedelta(seconds=serve_settings.ml_serve_lateness_s),
        keep=timedelta(hours=serve_settings.ml_serve_buffer_hours),
        poll_s=1,
        batch=200,
        clock=lambda: float(next(clock)),
    )
    scorer.run(stop=lambda: False, exit_when_idle=1)
    assert scorer.buffer.start > events[0].event_time  # the buffer did prune

    assert len(table.scores) == len(rows)
    for i, row in enumerate(rows):
        score = table.scores[i]
        assert score.cutoff == row["cutoff"], row["title"]
        assert score.page_revision_id == row["page_revision_id"], row["title"]
        assert score.history_complete, row["title"]
        for name in FEATURE_NAMES:
            assert same(row[name], score.features[name]), (row["title"], name)
    assert any(r["prior_edits_24h"] > 0 for r in rows)  # the history reached both sides
    assert any(r["cat_living_people"] == 1.0 for r in rows)  # snapshots, not NaN


# --- the test split: the newest days whose portal is final ------------------------------


def test_the_test_split_is_the_newest_final_days_and_later_days_wait():
    days = [f"2026-11-0{d}" for d in range(2, 8)]
    final = set(days[:4])  # 2 to 5 Nov final, 6 and 7 not yet
    split_of, delayed = assign_final_splits(days, final, valid_days=1, test_days=2)
    assert split_of == {
        "2026-11-02": "train",
        "2026-11-03": "valid",
        "2026-11-04": "test",
        "2026-11-05": "test",
    }
    assert delayed == ["2026-11-06", "2026-11-07"]
    with pytest.raises(ValueError, match="final portal"):
        assign_final_splits(days, {"2026-11-02"}, valid_days=1, test_days=2)
    with pytest.raises(ValueError, match="training day"):
        assign_final_splits(days, set(days[:2]), valid_days=1, test_days=2)


# --- 2. no impossible values on a synthetic day ---------------------------------------


def slow_burst(day: int, title: str, edits: int, editors: int, start_h: float) -> list[EditEvent]:
    """`edits` edits by `editors` people, one every 6 minutes: only the long-window rule
    can start a spike (never more than 2 edits in 10 minutes)."""
    t0 = at(day, start_h)
    return [
        EditEvent(
            event_id=f"{title}-{day}-slow-{i}",
            event_time=t0 + timedelta(minutes=6 * i),
            received_at=t0,
            title=title,
            type="edit",
            user=f"Slow editor {i % editors}",
            is_bot=False,
            is_minor=False,
            rev_old=day * 10_000 + i,
            rev_new=day * 10_000 + i + 1,
            len_old=2000,
            len_new=2040,
            comment="expand",
        )
        for i in range(edits)
    ]


@pytest.fixture
def synthetic_lake(tmp_path):
    """Six days of edits: day 1 warm-up, then each day bursts of 3 to 8 editors and slow
    hour-long bursts (8 to 12 edits), around the default and the candidate thresholds."""
    lake = local_store(tmp_path / "lake")
    events = [e for d in range(1, 8) for e in ticks(d)]
    for d in range(2, 8):
        for n, h in zip((3, 4, 5, 6, 8), (2, 5, 8, 11, 14), strict=True):
            events += burst(d, f"Burst {n} {d}", editors=n, start_h=h)
        events += slow_burst(d, f"Slow 8 {d}", edits=8, editors=3, start_h=17)
        events += slow_burst(d, f"Slow 12 {d}", edits=12, editors=4, start_h=20)
    by_hour: dict[datetime, list[EditEvent]] = {}
    for event in events:
        by_hour.setdefault(event.event_time.replace(minute=0, second=0, microsecond=0), []).append(
            event
        )
    for h, rows in by_hour.items():
        lake.write_hour(h, events_to_table(rows))
    portal = [PortalDay(date(2026, 11, d), [], []) for d in range(1, 11)]
    return lake, portal, tmp_path


def build_mode(synthetic_lake, mode: str) -> tuple[list[dict], dict]:
    lake, portal, tmp_path = synthetic_lake
    options = BuildOptions(
        mode=mode,
        labels=tmp_path / "labels.jsonl",
        use_db=False,
        use_pages=False,
        lake=lake,
        datasets=DatasetStore(pafs.LocalFileSystem(), (tmp_path / f"datasets-{mode}").as_posix()),
        portal=portal,
        as_of=datetime(2026, 11, 13, tzinfo=UTC),  # 2 to 7 Nov are final then
    )
    settings = Settings(_env_file=None, ml_valid_days=1, ml_test_days=2, ml_warmup_hours=24)
    version = build(options, settings)
    store = options.datasets
    rows = [
        row
        for split in ("train", "valid", "test")
        for row in store.read(version, split).to_pylist()
    ]
    return rows, store.manifest(version)


def test_serving_mode_rows_only_hold_values_serving_can_produce(synthetic_lake):
    settings = Settings(_env_file=None)
    rule = settings.detector_config()
    rows, manifest = build_mode(synthetic_lake, "serving")
    assert manifest["mode"] == "serving"
    assert manifest["detector"]["rows_from_run"] == "07a-default"
    assert "candidates" not in manifest["detector"]
    titles = {r["title"].rsplit(" ", 1)[0] for r in rows}
    assert titles == {"Burst 5", "Burst 6", "Burst 8", "Slow 12"}  # what the live rule fires on

    # the live detector starts a spike only once a start rule holds, so the burst up to
    # the start has at least this many human edits from at least this many editors
    fewest_edits = min(rule.min_edits, rule.long_min_edits)
    for row in rows:
        assert row["edits_before_start"] >= fewest_edits, row["title"]
        assert row["default_rule_fired"] is True  # as serving sets it

    # and every row is exactly what the scorer computes live for that spike
    lake, _, _ = synthetic_lake
    edits = page_edits(lake, {r["title"] for r in rows})
    to_score = [
        SpikeToScore(i, "07a-default", r["title"], r["first_edit_at"], r["started_at"])
        for i, r in enumerate(rows)
    ]
    scores, _ = served(to_score, lambda t: edits.get(t, []), {})
    for row, score in zip(rows, scores, strict=True):
        assert all(same(row[n], score.features[n]) for n in FEATURE_NAMES), row["title"]


def test_candidates_mode_has_the_values_serving_never_produces(synthetic_lake):
    """The control: the old mode does make rows serving can't (the skew 11a fixed)."""
    rule = Settings(_env_file=None).detector_config()
    rows, manifest = build_mode(synthetic_lake, "candidates")
    assert manifest["mode"] == "candidates"
    too_few = [
        r for r in rows if r["edits_before_start"] < min(rule.min_edits, rule.long_min_edits)
    ]
    assert too_few and {r["edits_before_start"] for r in too_few} == {4.0}
    # the skew of the 07c review: even rows where the default rule had fired by the cutoff
    # (the rows drift monitoring compares with) start at the candidate's 4th edit
    assert any(r["default_rule_fired"] for r in too_few)


def test_serving_mode_delays_days_whose_portal_is_not_final(synthetic_lake):
    lake, portal, tmp_path = synthetic_lake
    options = BuildOptions(
        mode="serving",
        labels=tmp_path / "labels.jsonl",
        use_db=False,
        use_pages=False,
        lake=lake,
        datasets=DatasetStore(pafs.LocalFileSystem(), (tmp_path / "delayed").as_posix()),
        portal=portal,
        as_of=datetime(2026, 11, 11, 12, tzinfo=UTC),  # 6 Nov final, 7 Nov not yet
    )
    settings = Settings(_env_file=None, ml_valid_days=1, ml_test_days=2, ml_warmup_hours=24)
    version = build(options, settings)
    manifest = options.datasets.manifest(version)
    assert manifest["split"]["delayed_days"] == ["2026-11-07"]
    assert manifest["splits"]["test"]["days"] == ["2026-11-05", "2026-11-06"]
    assert manifest["splits"]["valid"]["days"] == ["2026-11-04"]
    assert manifest["splits"]["train"]["days"] == ["2026-11-02", "2026-11-03"]
