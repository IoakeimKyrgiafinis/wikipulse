"""Backfill: hours that never got a notification, and hours rewritten since."""

from datetime import timedelta

from tests.workers.helpers import (
    PREFIX,
    MemoryStats,
    context,
    edit_by,
    invoker_for,
    local_lake,
    write_hour,
)
from wikipulse.lake.table import events_to_table
from wikipulse.workers.backfill import backfill, lake_hours


def three_hours(store) -> None:
    for hour in range(3):
        write_hour(store, [edit_by(f"e{hour}-{i}", 60 * hour + i, "P", f"U{i}") for i in range(3)])


def test_lake_hours_groups_files_by_hour_and_dataset(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    _, other = local_lake(tmp_path, "history/edits")
    three_hours(store)
    three_hours(other)
    (tmp_path / PREFIX / "notes.txt").write_text("not data")
    found = lake_hours(store.fs, bucket)
    assert len(found) == 6
    assert {location.dataset for location in found} == {
        f"{bucket}/edits",
        f"{bucket}/history/edits",
    }
    assert len(lake_hours(store.fs, bucket, "history")) == 3
    assert lake_hours(store.fs, bucket, "nothing-here") == {}


def test_backfill_handles_missing_hours_once(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    three_hours(store)
    stats = MemoryStats()
    ctx = context(stats)
    invoker = invoker_for(ctx)
    report = backfill(ctx.fs, stats.stored, invoker, bucket)
    assert (report.hours, report.missing, report.stale, report.succeeded) == (3, 3, 0, 3)
    assert len(stats.rows) == 3 and all(row.edits == 3 for row in stats.rows.values())

    again = backfill(ctx.fs, stats.stored, invoker, bucket)
    assert (again.missing, again.stale, again.succeeded) == (0, 0, 0)
    assert stats.writes == 3


def test_backfill_redoes_hours_rewritten_since(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    three_hours(store)
    stats = MemoryStats()
    ctx = context(stats)
    backfill(ctx.fs, stats.stored, invoker_for(ctx), bucket)
    first_hour = min(store.hours())
    late = edit_by("late", 30, "P", "Late editor")
    store.merge_hour(first_hour, events_to_table([late]))  # written without a notification
    report = backfill(ctx.fs, stats.stored, invoker_for(ctx), bucket)
    assert (report.missing, report.stale, report.succeeded) == (0, 1, 1)
    assert stats.rows[(f"{bucket}/{PREFIX}", first_hour)].edits == 4


def test_force_recomputes_everything(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    three_hours(store)
    stats = MemoryStats()
    ctx = context(stats)
    backfill(ctx.fs, stats.stored, invoker_for(ctx), bucket)
    report = backfill(ctx.fs, stats.stored, invoker_for(ctx), bucket, force=True)
    assert report.succeeded == 3 and stats.writes == 6


def test_a_failing_hour_is_dead_lettered_and_the_rest_continue(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    three_hours(store)
    broken_hour = min(store.hours()) + timedelta(hours=5)
    broken = tmp_path / store.path(broken_hour)[len(bucket) + 1 :]
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not parquet")
    stats = MemoryStats()
    ctx = context(stats)
    invoker = invoker_for(ctx, max_attempts=2)
    report = backfill(ctx.fs, stats.stored, invoker, bucket)
    assert (report.succeeded, report.failed) == (3, 1)
    assert invoker.client.llen("test:dead") == 1
