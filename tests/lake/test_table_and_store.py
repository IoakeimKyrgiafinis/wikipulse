"""Arrow tables of edits, merging without duplicates, and the local-folder lake."""

from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq

from tests.lake.helpers import T0, edit, local_store
from wikipulse.lake.table import EDIT_SCHEMA, events_to_table, merge_edits
from wikipulse.stream.models import EditEvent


def test_events_to_table_keeps_every_field() -> None:
    event = edit("a", 5, title="Ünïcode – title")
    table = events_to_table([event])
    assert table.schema == EDIT_SCHEMA
    row = table.to_pylist()[0]
    assert EditEvent(**row) == event


def test_events_to_table_handles_new_pages_without_old_revision() -> None:
    new_page = edit("n", 1).model_copy(update={"type": "new", "rev_old": None, "len_old": None})
    row = events_to_table([new_page]).to_pylist()[0]
    assert row["rev_old"] is None and row["len_old"] is None


def test_merge_removes_duplicates_and_sorts_by_event_time() -> None:
    first = events_to_table([edit("b", 20), edit("a", 10)])
    second = events_to_table([edit("a", 10), edit("c", 5)])
    merged = merge_edits(first, second)
    assert merged.column("event_id").to_pylist() == ["c", "a", "b"]


def test_merge_keeps_the_first_copy_of_an_edit() -> None:
    old = edit("a", 10)
    replayed = old.model_copy(update={"received_at": datetime(2026, 11, 9, tzinfo=UTC)})
    merged = merge_edits(events_to_table([old]), events_to_table([replayed]))
    assert merged.to_pylist()[0]["received_at"] == old.received_at


def test_merge_result_does_not_depend_on_arrival_order() -> None:
    events = [edit(f"e{i}", (i * 7) % 50) for i in range(30)]
    in_order = merge_edits(events_to_table(events))
    shuffled = merge_edits(events_to_table(events[15:]), events_to_table(events[:15]))
    assert in_order.equals(shuffled)


def test_merge_of_nothing_is_empty() -> None:
    assert merge_edits(events_to_table([])).num_rows == 0


def test_store_writes_and_reads_an_hour(tmp_path) -> None:
    store = local_store(tmp_path / "lake")
    table = events_to_table([edit("a", 1), edit("b", 2)])
    store.write_hour(T0, table)
    path = tmp_path / "lake/year=2026/month=11/day=07/hour=12/part-0.parquet"
    assert path.is_file()
    assert pq.read_table(path).num_rows == 2
    assert store.read_hour(T0).equals(table)
    assert store.hours() == [T0]


def test_store_replaces_an_hour_without_leftovers(tmp_path) -> None:
    store = local_store(tmp_path)
    store.write_hour(T0, events_to_table([edit("a", 1), edit("b", 2)]))
    store.write_hour(T0, events_to_table([edit("c", 3)]))
    assert store.read_hour(T0).column("event_id").to_pylist() == ["c"]
    folder = tmp_path / "year=2026/month=11/day=07/hour=12"
    assert sorted(p.name for p in folder.iterdir()) == ["part-0.parquet"]  # no temp files


def test_merge_hour_adds_rows_once(tmp_path) -> None:
    store = local_store(tmp_path)
    assert store.merge_hour(T0, events_to_table([edit("a", 1), edit("b", 2)])) == 2
    assert store.merge_hour(T0, events_to_table([edit("b", 2), edit("c", 3)])) == 3
    assert store.merge_hour(T0, events_to_table([edit("c", 3)])) == 3
    assert store.read_hour(T0).column("event_id").to_pylist() == ["a", "b", "c"]


def test_missing_hour_reads_as_none(tmp_path) -> None:
    store = local_store(tmp_path)
    assert store.read_hour(T0) is None
    assert store.file_size(T0) is None
    assert store.hours() == []


def test_dataset_adds_partition_columns(tmp_path) -> None:
    store = local_store(tmp_path)
    store.write_hour(T0, events_to_table([edit("a", 1)]))
    store.write_hour(T0.replace(hour=13), events_to_table([edit("b", 61), edit("c", 62)]))
    table = store.dataset().to_table(columns=["event_id", "day", "hour"])
    rows = sorted(table.to_pylist(), key=lambda row: row["event_id"])
    assert rows == [
        {"event_id": "a", "day": 7, "hour": 12},
        {"event_id": "b", "day": 7, "hour": 13},
        {"event_id": "c", "day": 7, "hour": 13},
    ]


def test_dataset_of_an_empty_lake_has_the_full_schema(tmp_path) -> None:
    dataset = local_store(tmp_path / "nothing-yet").dataset()
    assert dataset.to_table().num_rows == 0
    assert {"event_id", "event_time", "year", "hour"} <= set(dataset.schema.names)


def test_files_read_back_with_extra_columns_are_conformed(tmp_path) -> None:
    store = local_store(tmp_path)
    table = events_to_table([edit("a", 1)]).append_column("extra", pa.array([1]))
    store.write_hour(T0, table)
    assert store.read_hour(T0).schema == EDIT_SCHEMA
