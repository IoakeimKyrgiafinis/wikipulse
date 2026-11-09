"""The hourly statistics handler: numbers, idempotence, skipping, failures."""

from datetime import UTC, datetime

import pyarrow.parquet as pq
import pytest

from tests.workers.helpers import (
    PREFIX,
    MemoryStats,
    context,
    edit_by,
    event_for,
    key_of,
    local_lake,
    write_hour,
)
from wikipulse.lake.table import events_to_table
from wikipulse.workers.hourly_stats import TopPage, compute_counts, handle, locate

T12 = datetime(2026, 11, 7, 12, tzinfo=UTC)


def sample_edits():
    return [
        edit_by("a1", 1, "Alpha", "Ann"),
        edit_by("a2", 2, "Alpha", "Bob"),
        edit_by("a3", 3, "Alpha", "Ann"),
        edit_by("b1", 4, "Beta", "Cat"),
        edit_by("b2", 5, "Beta", "Bot1", is_bot=True),
        edit_by("c1", 6, "Gamma", "Bot1", is_bot=True),
        edit_by("d1", 7, "Delta", "Dan"),
        edit_by("d2", 8, "Delta", "Eve"),
    ]


def test_compute_counts() -> None:
    counts = compute_counts(events_to_table(sample_edits()), top_n=3)
    assert counts.edits == 8
    assert counts.distinct_pages == 4
    assert counts.distinct_editors == 6  # Ann, Bob, Cat, Bot1, Dan, Eve
    assert counts.bot_edits == 2
    assert counts.bot_share == pytest.approx(0.25)
    # Alpha 3 edits; Beta and Delta 2 edits by 2 editors each: tie broken by title.
    assert counts.top_pages == (
        TopPage("Alpha", 3, 2),
        TopPage("Beta", 2, 2),
        TopPage("Delta", 2, 2),
    )


def test_compute_counts_of_an_empty_hour() -> None:
    counts = compute_counts(events_to_table([]))
    assert (counts.edits, counts.bot_share, counts.top_pages) == (0, 0.0, ())


def test_top_pages_do_not_depend_on_row_order() -> None:
    edits = sample_edits()
    forward = compute_counts(events_to_table(edits))
    backward = compute_counts(events_to_table(list(reversed(edits))))
    assert forward == backward


def test_locate_reads_dataset_and_hour_from_the_key() -> None:
    location = locate("wikipulse-lake", "edits/year=2026/month=11/day=07/hour=12/part-0.parquet")
    assert location is not None
    assert location.dataset == "wikipulse-lake/edits"
    assert location.hour == T12
    assert location.folder == "wikipulse-lake/edits/year=2026/month=11/day=07/hour=12"
    nested = locate("b", "history/edits/year=2026/month=11/day=07/hour=12/part-0.parquet")
    assert nested is not None and nested.dataset == "b/history/edits"
    bare = locate("b", "year=2026/month=11/day=07/hour=12/part-0.parquet")
    assert bare is not None and bare.dataset == "b"


@pytest.mark.parametrize(
    "key",
    [
        "edits/year=2026/month=11/day=07/hour=12/notes.txt",
        "edits/year=2026/month=11/day=07/hour=12/.tmp-1234.parquet",
        "edits/other.parquet",
    ],
)
def test_locate_rejects_other_objects(key: str) -> None:
    assert locate("bucket", key) is None


def test_handler_writes_the_row(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    write_hour(store, sample_edits())
    stats = MemoryStats()
    result = handle(event_for(bucket, key_of(T12)), context(stats))
    assert result.outcome == "written"
    row = stats.rows[(f"{bucket}/{PREFIX}", T12)]
    assert (row.edits, row.distinct_pages, row.distinct_editors, row.bot_edits) == (8, 4, 6, 2)
    assert row.source_files == 1
    assert row.source_size == store.file_size(T12)


def test_same_notification_twice_does_not_double_count(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    write_hour(store, sample_edits())
    stats = MemoryStats()
    ctx = context(stats)
    first = handle(event_for(bucket, key_of(T12)), ctx).stats
    second = handle(event_for(bucket, key_of(T12)), ctx).stats
    assert first == second
    assert len(stats.rows) == 1
    assert stats.rows[(f"{bucket}/{PREFIX}", T12)].edits == 8


def test_rewritten_hour_replaces_the_numbers(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    write_hour(store, sample_edits()[:3])
    stats = MemoryStats()
    ctx = context(stats)
    handle(event_for(bucket, key_of(T12)), ctx)
    store.merge_hour(T12, events_to_table(sample_edits()))  # the archiver adds rows
    handle(event_for(bucket, key_of(T12)), ctx)
    assert stats.rows[(f"{bucket}/{PREFIX}", T12)].edits == 8


def test_handler_reads_every_file_of_the_hour(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    write_hour(store, sample_edits()[:4])
    second_part = store.path(T12).replace("part-0", "part-1")
    pq.write_table(events_to_table(sample_edits()[4:]), second_part)
    stats = MemoryStats()
    handle(event_for(bucket, key_of(T12)), context(stats))
    row = stats.rows[(f"{bucket}/{PREFIX}", T12)]
    assert (row.edits, row.source_files) == (8, 2)


def test_other_objects_and_deleted_hours_are_skipped(tmp_path) -> None:
    bucket, _ = local_lake(tmp_path)
    stats = MemoryStats()
    assert handle(event_for(bucket, "edits/readme.txt"), context(stats)).outcome == "skipped"
    gone = handle(event_for(bucket, key_of(T12)), context(stats))  # no file at all
    assert gone.outcome == "skipped"
    assert stats.rows == {}


def test_a_corrupt_file_raises(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    path = tmp_path / key_of(T12)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not parquet")
    with pytest.raises(Exception, match="(?i)parquet"):
        handle(event_for(bucket, key_of(T12)), context())


def test_rows_from_newer_files_are_kept(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    write_hour(store, sample_edits())
    stats = MemoryStats()
    newer = handle(event_for(bucket, key_of(T12)), context(stats)).stats
    older = newer.__class__(**{**newer.__dict__, "edits": 1, "source_modified": T12})
    assert stats.upsert(older) is False
    assert stats.rows[(newer.dataset, T12)].edits == 8
