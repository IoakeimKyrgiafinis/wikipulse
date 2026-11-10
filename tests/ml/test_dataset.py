"""Building the dataset: spikes, labels, the time-based split, versioned storage."""

import json
import random
from datetime import UTC, date, datetime, timedelta

import pyarrow.fs as pafs
import pytest

from tests.eval.helpers import at, spike
from tests.lake.helpers import local_store
from wikipulse.config import Settings
from wikipulse.eval.ground_truth import PortalDay
from wikipulse.eval.label_store import append_record, record_for
from wikipulse.eval.metrics import Label
from wikipulse.lake.table import events_to_table
from wikipulse.ml import dataset as dataset_module
from wikipulse.ml.dataset import (
    DATASET_SCHEMA,
    BuildOptions,
    DatasetStore,
    assign_splits,
    build,
    candidate_config,
    combined_label,
    contiguous_hours,
    day_of,
    label_of,
    portal_final,
    portal_status,
    purge_boundaries,
    resolve_as_of,
    revision_at,
    rule_fired,
    select_spikes,
    split_label_as_of,
    split_rows,
    summary,
    weak_label_of,
)
from wikipulse.ml.features import FEATURE_NAMES, PageEdit
from wikipulse.stream.models import EditEvent


def hour(day: int, h: int) -> datetime:
    return datetime(2026, 11, day, h, tzinfo=UTC)


# --- pieces -----------------------------------------------------------------------------


def test_contiguous_hours_keeps_the_last_unbroken_stretch():
    hours = [hour(1, 0), hour(1, 1), hour(1, 5), hour(1, 6), hour(1, 7)]
    run, missing = contiguous_hours(random.Random(1).sample(hours, len(hours)))
    assert run == [hour(1, 5), hour(1, 6), hour(1, 7)]
    assert missing == [hour(1, 2)]
    assert contiguous_hours([]) == ([], [])


def test_splits_are_by_time_never_random():
    days = [f"2026-11-{d:02d}" for d in range(1, 11)]
    split = assign_splits(random.Random(7).sample(days, 10), valid_days=2, test_days=3)
    assert [split[d] for d in days] == ["train"] * 5 + ["valid"] * 2 + ["test"] * 3
    train = max(d for d in days if split[d] == "train")
    assert all(d > train for d in days if split[d] != "train")


def test_split_rows_is_the_same_for_any_row_order():
    rows = [
        {"day": f"2026-11-0{d}", "started_at": at(d, h), "title": f"P{d}{h}"}
        for d in range(1, 6)
        for h in (1, 5)
    ]
    split_of = assign_splits((r["day"] for r in rows), 1, 1)
    a = split_rows(rows, split_of)
    b = split_rows(random.Random(3).sample(rows, len(rows)), split_of)
    assert a == b
    assert {r["day"] for r in a["test"]} == {"2026-11-05"}
    assert {r["day"] for r in a["valid"]} == {"2026-11-04"}


def test_too_few_days_for_the_split_is_an_error():
    with pytest.raises(ValueError):
        assign_splits(["2026-11-01", "2026-11-02"], valid_days=1, test_days=1)


def test_select_spikes_drops_warm_up_and_spikes_running_past_the_data():
    start, end = at(1), at(5)
    early = spike("Early", at(1, 12))  # inside the 24 h warm-up
    fine = spike("Fine", at(3, 12))
    late = spike("Late", end - timedelta(minutes=5))  # feature window ends after the data
    chosen = select_spikes([early, fine, late], start, end, timedelta(hours=24), 10)
    assert chosen == [fine]


def test_revision_at_is_the_newest_revision_up_to_the_cutoff():
    edits = [
        PageEdit(at(3, 1), "A", rev_new=10),
        PageEdit(at(3, 2), "B", rev_new=11),
        PageEdit(at(3, 3), "C", rev_new=12),
    ]
    assert revision_at(edits, at(3, 2.5)) == 11
    assert revision_at(edits, at(3, 0)) is None


def test_combined_label():
    llm_no = Label("P", at(3), at(3, 1), "not news", "llm")
    assert combined_label(True, llm_no) == (1, "portal")  # the portal wins
    assert combined_label(False, llm_no) == (0, "llm")
    assert combined_label(False, Label("P", at(3), at(3, 1), "news", "human")) == (1, "human")
    unsure = Label("P", at(3), at(3, 1), "unsure", "llm")
    assert combined_label(False, unsure) == (None, "unknown")


def test_not_on_the_portal_and_unlabeled_is_unknown_never_a_confident_zero():
    assert combined_label(False, None) == (None, "unknown")  # final weak negative
    assert combined_label(None, None) == (None, "unknown")  # provisional weak negative
    llm_no = Label("P", at(3), at(3, 1), "not news", "llm")
    assert combined_label(None, llm_no) == (0, "llm")  # a real label still decides


def test_rule_fired_only_counts_what_was_known_at_the_cutoff():
    candidate = spike("P", at(3, 12), delay_min=5)  # cutoff 12:10
    cutoff = at(3, 12) + timedelta(minutes=10)
    assert rule_fired(candidate, [spike("P", at(3, 12) + timedelta(minutes=4))], cutoff)
    assert not rule_fired(candidate, [spike("P", at(3, 12) + timedelta(minutes=30))], cutoff)
    earlier = spike("P", at(3, 6), duration_h=1)  # over at 07:00, long before this burst
    assert not rule_fired(candidate, [earlier], cutoff)


def test_portal_final_needs_every_day_present_and_settled():
    s = spike("P", at(5, 12))
    days = [PortalDay(date(2026, 11, d), [], []) for d in range(1, 10)]
    assert portal_final(s, days, at(12)) is True
    assert portal_final(s, days, at(9)) is False  # the 7 Nov page is not 3 days old yet
    days[5] = PortalDay(date(2026, 11, 6), [], [], missing=True)
    assert portal_final(s, days, at(12)) is False


def test_dataset_versions_are_numbered_never_overwritten_and_hashed(tmp_path):
    store = DatasetStore(pafs.LocalFileSystem(), (tmp_path / "datasets").as_posix())
    empty = DATASET_SCHEMA.empty_table()
    assert store.versions() == []
    v1 = store.write({"train": empty, "valid": empty, "test": empty}, {"rows": 0})
    v2 = store.write({"train": empty, "valid": empty, "test": empty}, {"rows": 0})
    assert (v1, v2) == ("v1", "v2")
    assert store.versions() == [1, 2]
    manifest = store.manifest("v2")
    assert manifest["version"] == "v2"
    assert set(manifest["files"]) == {"train.parquet", "valid.parquet", "test.parquet"}
    assert len(manifest["fingerprint"]) == 64
    (tmp_path / "datasets" / "v3").mkdir()  # an incomplete version (no manifest) ...
    assert store.versions() == [1, 2]  # ... is not listed, but its number is not reused
    assert store.write({"train": empty}, {}) == "v4"


def test_candidate_thresholds_change_only_what_they_name():
    settings = Settings(_env_file=None)
    config = candidate_config(settings, "min_edits=4,min_editors=3")
    assert (config.min_edits, config.min_editors) == (4, 3)
    assert config.long_min_edits == settings.detector_config().long_min_edits


def test_portal_status():
    days = [
        PortalDay(date(2026, 11, 1), [], []),
        PortalDay(date(2026, 11, 8), [], []),
        PortalDay(date(2026, 11, 9), [], [], missing=True),
    ]
    assert portal_status(days, at(10)) == {
        "2026-11-01": "final",
        "2026-11-08": "provisional",
        "2026-11-09": "missing",
    }


# --- split boundaries and labels as of a moment ----------------------------------------

# 1-4 Nov train, 5 Nov valid, 6-7 Nov test
SPLIT_OF = {
    f"2026-11-0{d}": "train" if d <= 4 else "valid" if d == 5 else "test" for d in range(1, 8)
}


def test_each_split_takes_its_labels_as_of_the_next_splits_start():
    as_of = split_label_as_of(SPLIT_OF, at(8, 6))
    assert as_of == {"train": at(5), "valid": at(6), "test": at(8, 6)}
    no_valid = {day: "test" if split == "valid" else split for day, split in SPLIT_OF.items()}
    assert split_label_as_of(no_valid, at(8))["train"] == at(5)  # straight to test


def test_purge_drops_spikes_running_into_the_next_split():
    as_of = split_label_as_of(SPLIT_OF, at(8))
    running = spike("Overnight", at(4, 23.5), duration_h=2)  # train, ends 5 Nov 01:30
    inside = spike("Evening", at(4, 20), duration_h=1)
    last = spike("Late test", at(7, 22), duration_h=1)  # the last split has no next one
    kept, purged = purge_boundaries([running, inside, last], SPLIT_OF, as_of, timedelta(hours=24))
    assert kept == [inside, last]
    assert purged == {"running": {"train": 1}, "same_page": {}}


def test_purge_drops_the_earlier_spike_of_a_page_that_spikes_again_across_a_boundary():
    as_of = split_label_as_of(SPLIT_OF, at(8))
    late_train = spike("Pedro", at(4, 21), duration_h=1)  # ends 22:00
    early_valid = spike("Pedro", at(5, 2.5))  # 4.5 h later, in validation
    old_train = spike("Yogurt", at(3, 10))  # ends 3 Nov 11:00 ...
    next_valid = spike("Yogurt", at(5, 12))  # ... 49 h before this one: beyond the gap
    same_split = spike("Twice", at(3, 10))  # spikes again the next day, but still in train
    again = spike("Twice", at(4, 9))
    spikes = [old_train, same_split, again, late_train, early_valid, next_valid]
    kept, purged = purge_boundaries(spikes, SPLIT_OF, as_of, timedelta(hours=24))
    assert late_train not in kept
    assert all(s in kept for s in (early_valid, old_train, next_valid, same_split, again))
    assert purged == {"running": {}, "same_page": {"train": 1}}
    kept, _ = purge_boundaries(spikes, SPLIT_OF, as_of, timedelta(0))  # gap 0: no page rule
    assert late_train in kept


def test_no_label_uses_evidence_from_a_later_split():
    portal = [PortalDay(date(2026, 11, d), [], []) for d in range(1, 10)]
    portal[4] = PortalDay(date(2026, 11, 5), ["Story"], ["Story"])  # linked on 5 Nov only
    train_spike = spike("Story", at(4, 12))  # train: labels as of 5 Nov 00:00
    assert day_of(train_spike) == "2026-11-04"
    # linked on a validation day: not a positive for training ...
    assert weak_label_of(train_spike, portal, at(5)) == (None, [], False)
    # ... and not a negative either (that would assert "not on the portal" too early)
    assert weak_label_of(train_spike, portal, at(10)) == (True, [date(2026, 11, 5)], True)
    llm = Label("Story", at(4, 11.9), at(5), "news", "llm")  # its burst ended at 5 Nov
    assert label_of(train_spike, [llm], at(5)) is None
    assert label_of(train_spike, [llm], at(5, 1)) is llm


def test_weak_negative_only_once_the_portal_days_are_final():
    portal = [PortalDay(date(2026, 11, d), [], []) for d in range(1, 12)]
    s = spike("Quiet", at(4, 12))  # depends on the portal days 2 to 6 Nov
    assert weak_label_of(s, portal, at(9)) == (False, [], True)  # 6 Nov + 3 days = 9 Nov
    assert weak_label_of(s, portal, at(8, 23)) == (None, [], False)  # not final yet


def test_as_of_defaults_to_the_end_of_the_data_not_the_wall_clock():
    end, now = at(7), at(10)
    assert resolve_as_of(None, end, now) == end
    assert resolve_as_of(at(9), end, now) == at(9)
    with pytest.raises(ValueError):
        resolve_as_of(at(6), end, now)  # before the data's end
    with pytest.raises(ValueError):
        resolve_as_of(at(11), end, now)  # in the future


# --- the whole build, offline ---------------------------------------------------------

DAYS = range(1, 7)  # 1 Nov is warm-up; 2-6 Nov have spikes


def burst(day: int, title: str, editors: int, start_h: float = 12) -> list[EditEvent]:
    """`editors` people edit `title` within a few minutes on `day`."""
    t0 = at(day, start_h)
    return [
        EditEvent(
            event_id=f"{title}-{day}-{i}",
            event_time=t0 + timedelta(minutes=2 * i),
            received_at=t0,
            title=title,
            type="edit",
            user=f"Editor {i}",
            is_bot=False,
            is_minor=False,
            rev_old=day * 1000 + i,
            rev_new=day * 1000 + i + 1,
            len_old=1000,
            len_new=1050,
            comment="Results announced" if title.startswith("News") else "copyedit",
        )
        for i in range(editors)
    ]


def ticks(day: int) -> list[EditEvent]:
    """One bot edit every 20 minutes, so event time moves on and every hour has a file."""
    return [
        EditEvent(
            event_id=f"tick-{day}-{m}",
            event_time=at(day) + timedelta(minutes=m),
            received_at=at(day),
            title="Bot page",
            type="edit",
            user="TickBot",
            is_bot=True,
            is_minor=True,
            rev_old=None,
            rev_new=None,
            len_old=None,
            len_new=None,
            comment="",
        )
        for m in range(0, 24 * 60, 20)
    ]


@pytest.fixture
def offline_build(tmp_path):
    lake = local_store(tmp_path / "lake")
    events = [e for d in DAYS for e in ticks(d)]
    for d in range(2, 7):
        events += burst(d, f"News {d}", editors=6) + burst(d, f"Quiet {d}", editors=4, start_h=18)
    by_hour: dict[datetime, list[EditEvent]] = {}
    for event in events:
        by_hour.setdefault(event.event_time.replace(minute=0, second=0, microsecond=0), []).append(
            event
        )
    for h, rows in by_hour.items():
        lake.write_hour(h, events_to_table(rows))
    labels = tmp_path / "labels.jsonl"
    labeled = spike("Quiet 3", at(3, 18) + timedelta(minutes=6), delay_min=6)
    append_record(labels, record_for(labeled, "x", "news", "llm", at(9), model="ollama:test"))
    portal = [PortalDay(date(2026, 11, d), [f"News {d}"], [f"News {d}"]) for d in range(1, 9)]
    options = BuildOptions(
        labels=labels,
        use_db=False,
        use_pages=False,
        lake=lake,
        datasets=DatasetStore(pafs.LocalFileSystem(), (tmp_path / "datasets").as_posix()),
        portal=portal,
    )
    settings = Settings(_env_file=None, ml_valid_days=1, ml_test_days=2, ml_warmup_hours=24)
    return options, settings


def test_build_writes_a_versioned_time_split_dataset(offline_build):
    options, settings = offline_build
    store = options.datasets
    options.dry_run = True
    assert build(options, settings) == "(dry run)"
    assert store.versions() == []  # a dry run stores nothing
    options.dry_run = False
    version = build(options, settings)
    assert version == "v1"
    manifest = store.manifest("v1")
    tables = {name: store.read("v1", name).to_pylist() for name in ("train", "valid", "test")}
    rows = [row for split in tables.values() for row in split]

    # 10 bursts on 2-6 Nov: News pages (6 editors) and Quiet pages (4 editors)
    assert sorted(r["title"] for r in rows) == sorted(
        [f"News {d}" for d in range(2, 7)] + [f"Quiet {d}" for d in range(2, 7)]
    )
    assert {r["day"] for r in tables["train"]} == {"2026-11-02", "2026-11-03"}
    assert {r["day"] for r in tables["valid"]} == {"2026-11-04"}
    assert {r["day"] for r in tables["test"]} == {"2026-11-05", "2026-11-06"}
    news = [r for r in rows if r["title"].startswith("News")]
    assert all(r["weak_label"] and r["label"] == 1 and r["label_source"] == "portal" for r in news)
    quiet3 = next(r for r in rows if r["title"] == "Quiet 3")
    assert (quiet3["label_02b"], quiet3["label_02b_labeler"], quiet3["label"]) == ("news", "llm", 1)
    quiet4 = next(r for r in rows if r["title"] == "Quiet 4")
    # validation day, not on the portal: the portal days aren't final at the test split's
    # start, and nobody labeled it, so both labels are unknown, not 0
    assert (quiet4["weak_label"], quiet4["label"], quiet4["label_source"]) == (
        None,
        None,
        "unknown",
    )
    assert quiet4["label_as_of"] == at(5)  # the test split's start
    assert all(r["label_as_of"] == at(4) for r in tables["train"])
    assert all(r["label_as_of"] == at(7) for r in tables["test"])  # the end of the data
    assert all(r["default_rule_fired"] == r["title"].startswith("News") for r in rows)
    assert all(r["edits"] in (5.0, 6.0, 4.0, 3.0) for r in rows)  # only up to the cutoff

    assert manifest["rows"] == 10
    assert manifest["features"].keys() == set(FEATURE_NAMES)
    assert manifest["splits"]["train"]["weak_label"]["positive"] == 2
    assert manifest["splits"]["test"]["label"]["positive"] == 2
    assert manifest["splits"]["test"]["label"]["unknown"] == 2  # Quiet 5 and Quiet 6
    assert manifest["splits"]["test"]["label"]["negative"] == 0
    assert manifest["labels"]["as_of"] == at(7).isoformat()
    assert manifest["split"]["purged"]["rows"] == {"running": {}, "same_page": {}}
    assert manifest["leakage_check"] == {"rows_checked": 10, "passed": True}
    assert manifest["source"]["hours"] == 6 * 24
    assert manifest["date_range"] == {"first_day": "2026-11-02", "last_day": "2026-11-06"}
    assert "commit" in manifest["git"]
    json.dumps(manifest)  # valid JSON throughout (no NaN)
    assert "10 rows" in summary(manifest)
    assert build(options, settings) == "v2"  # a rebuild is a new version
    assert store.manifest("v2")["fingerprint"] == manifest["fingerprint"]  # same data, same hash


def fixed_clock(moment: datetime) -> type[datetime]:
    """`datetime` whose `now()` is always `moment`."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    return Clock


def test_a_dry_run_writes_nothing_not_even_to_postgres(offline_build, monkeypatch):
    options, settings = offline_build

    def no_postgres(*args, **kwargs):
        raise AssertionError("a dry run must not write the runs to Postgres")

    monkeypatch.setattr(dataset_module, "store_runs", no_postgres)
    options.use_db, options.dry_run = True, True
    assert build(options, settings) == "(dry run)"
    assert options.datasets.versions() == []


def test_the_fingerprint_does_not_depend_on_the_wall_clock(offline_build, monkeypatch):
    options, settings = offline_build
    fingerprints = []
    for moment in (at(10), datetime(2027, 6, 1, tzinfo=UTC)):  # 3 days and 7 months later
        monkeypatch.setattr(dataset_module, "datetime", fixed_clock(moment))
        version = build(options, settings)
        fingerprints.append(options.datasets.manifest(version)["fingerprint"])
    assert fingerprints[0] == fingerprints[1]
