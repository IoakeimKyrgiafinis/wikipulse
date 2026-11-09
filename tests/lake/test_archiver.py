"""The archiver: buffering by event hour, acknowledging after writes, crash safety."""

import pytest

from tests.lake.helpers import (
    T0,
    add_to_stream,
    archived_ids,
    archiver,
    edit,
    fake_client,
    local_store,
)
from wikipulse.lake.partitions import hour_of

STREAM = "test:edits"
GROUP = "test:lake"


@pytest.fixture
def client():
    return fake_client()


@pytest.fixture
def store(tmp_path):
    return local_store(tmp_path / "lake")


def pending(client) -> int:
    return client.xpending(STREAM, GROUP)["pending"]


def three_hours() -> list:
    """40 edits spread over 11:00-13:59, in stream order (a few slightly out of order)."""
    events = [edit(f"e{i:02d}", -60 + i * 4.5) for i in range(40)]
    events[10], events[11] = events[11], events[10]
    return events


def test_edits_are_buffered_by_the_hour_of_their_event_time(client, store) -> None:
    events = three_hours()
    add_to_stream(client, STREAM, events)
    archiver(client, store).run(idle_exit_s=0)
    assert store.hours() == [T0.replace(hour=11), T0, T0.replace(hour=13)]
    for hour in store.hours():
        times = store.read_hour(hour).column("event_time").to_pylist()
        assert {hour_of(t) for t in times} == {hour}
    assert sorted(archived_ids(store)) == sorted(e.event_id for e in events)
    assert pending(client) == 0


def test_nothing_is_acknowledged_before_its_hour_is_written(client, store) -> None:
    add_to_stream(client, STREAM, three_hours())
    worker = archiver(client, store)
    worker.run(idle_exit_s=0, final_flush=False)
    assert pending(client) == 40
    assert store.hours() == []
    assert set(worker.buffers) == {T0.replace(hour=11), T0, T0.replace(hour=13)}


def test_failed_write_leaves_entries_pending_and_buffered(client, store, monkeypatch) -> None:
    add_to_stream(client, STREAM, three_hours())
    worker = archiver(client, store)
    worker.run(idle_exit_s=0, final_flush=False)

    def broken(hour, table):
        if hour == T0:
            raise OSError("S3 is down")
        return original(hour, table)

    original = store.write_hour
    monkeypatch.setattr(store, "write_hour", broken)
    with pytest.raises(OSError):
        worker.flush()
    # 11:00 was written and acknowledged; 12:00 and 13:00 were not.
    assert store.hours() == [T0.replace(hour=11)]
    assert pending(client) == 40 - len(store.read_hour(T0.replace(hour=11)))
    assert set(worker.buffers) == {T0, T0.replace(hour=13)}


def test_lake_outage_is_retried_without_losing_edits(client, store, monkeypatch) -> None:
    clock = [0.0]
    events = three_hours()
    add_to_stream(client, STREAM, events[:20])
    worker = archiver(client, store, flush_interval_s=10, monotonic=lambda: clock[0])
    original = store.write_hour

    def down(hour, table):
        raise OSError("connection refused")

    monkeypatch.setattr(store, "write_hour", down)
    clock[0] = 11
    worker.run(max_entries=20, final_flush=False)  # the flush fails, is logged, not raised
    assert pending(client) == 20 and worker.buffered_rows() == 20

    monkeypatch.setattr(store, "write_hour", original)
    add_to_stream(client, STREAM, events[20:])
    clock[0] = 30  # past the retry delay
    worker.run(idle_exit_s=0)
    assert sorted(archived_ids(store)) == sorted(e.event_id for e in events)
    assert pending(client) == 0


def test_crash_mid_hour_leaves_no_gaps_or_duplicates(client, store) -> None:
    events = three_hours()
    add_to_stream(client, STREAM, events)
    # First run: writes what it read after 15 entries (11:00 complete, 12:00 started)...
    archiver(client, store).run(max_entries=15)
    first_ids = set(archived_ids(store))
    assert T0 in store.hours()  # 12:00 already has a file
    # ...then a second run reads 15 more and "crashes" before writing them.
    archiver(client, store).run(max_entries=15, final_flush=False)
    assert set(archived_ids(store)) == first_ids
    assert pending(client) == 15
    # The restart re-reads its pending entries, then the rest.
    archiver(client, store).run(idle_exit_s=0)
    ids = archived_ids(store)
    assert len(ids) == len(set(ids)) == 40
    assert set(ids) == {e.event_id for e in events}
    assert pending(client) == 0


def test_crash_after_write_before_ack_creates_no_duplicates(client, store, monkeypatch) -> None:
    events = three_hours()
    add_to_stream(client, STREAM, events)
    crashing = archiver(client, store)

    def crash(*ids):
        raise SystemExit("killed between the write and XACK")

    monkeypatch.setattr(crashing, "_ack", crash)
    with pytest.raises(SystemExit):
        crashing.run(idle_exit_s=0)
    assert len(store.hours()) == 1 and pending(client) == 40

    archiver(client, store).run(idle_exit_s=0)
    ids = archived_ids(store)
    assert len(ids) == len(set(ids)) == 40
    assert pending(client) == 0


def test_late_edit_joins_its_already_archived_hour(client, store) -> None:
    add_to_stream(client, STREAM, three_hours())
    archiver(client, store).run(idle_exit_s=0)
    before = store.read_hour(T0.replace(hour=11)).num_rows
    late = edit("late", -30)  # 11:30, arriving after 13:00 was archived
    add_to_stream(client, STREAM, [late])
    archiver(client, store).run(idle_exit_s=0)
    table = store.read_hour(T0.replace(hour=11))
    assert table.num_rows == before + 1
    assert "late" in table.column("event_id").to_pylist()


def test_rerunning_the_whole_stream_changes_nothing(client, store) -> None:
    add_to_stream(client, STREAM, three_hours() + three_hours()[:5])  # 5 duplicates too
    archiver(client, store).run(idle_exit_s=0)
    first = {hour: store.read_hour(hour) for hour in store.hours()}
    client.xgroup_destroy(STREAM, GROUP)
    archiver(client, store).run(idle_exit_s=0)  # the same entries, all over again
    assert {hour: store.read_hour(hour) for hour in store.hours()}.keys() == first.keys()
    for hour, table in first.items():
        assert store.read_hour(hour).equals(table)
    assert len(archived_ids(store)) == 40


def test_entries_without_valid_data_are_skipped_and_acknowledged(client, store) -> None:
    add_to_stream(client, STREAM, [edit("good", 1)])
    client.xadd(STREAM, {"data": "{not json"})
    client.xadd(STREAM, {"title": "no data field"})
    stats = archiver(client, store).run(idle_exit_s=0)
    assert stats.entries == 3 and stats.skipped == 2
    assert archived_ids(store) == ["good"]
    assert pending(client) == 0


def test_a_full_buffer_forces_a_write(client, store) -> None:
    add_to_stream(client, STREAM, three_hours())
    worker = archiver(client, store, max_buffer_rows=10, batch_size=5)
    worker.run(max_entries=12, final_flush=False)
    assert worker.stats.flushes == 1  # at 10 buffered edits
    assert pending(client) == 2 and worker.buffered_rows() == 2


def test_the_flush_interval_writes_periodically(client, store) -> None:
    clock = [0.0]
    add_to_stream(client, STREAM, three_hours())
    worker = archiver(client, store, flush_interval_s=60, batch_size=10, monotonic=lambda: clock[0])
    worker.run(max_entries=10, final_flush=False)
    assert worker.stats.flushes == 0
    clock[0] = 61
    worker.run(max_entries=20, final_flush=False)
    assert worker.stats.flushes == 1 and pending(client) == 0


def test_another_consumer_group_reads_independently(client, store) -> None:
    add_to_stream(client, STREAM, three_hours())
    client.xgroup_create(STREAM, "detect:live", id="0")
    client.xreadgroup("detect:live", "detector-1", {STREAM: ">"}, count=100)  # never acked
    archiver(client, store).run(idle_exit_s=0)
    assert len(archived_ids(store)) == 40
    assert client.xpending(STREAM, "detect:live")["pending"] == 40  # untouched


def test_restart_under_another_consumer_name_claims_old_pending_entries(client, store) -> None:
    events = three_hours()
    add_to_stream(client, STREAM, events)
    archiver(client, store, consumer="archiver-old").run(max_entries=25, final_flush=False)
    assert pending(client) == 25  # all held by "archiver-old"
    archiver(client, store, consumer="archiver-new").run(idle_exit_s=0)
    ids = archived_ids(store)
    assert len(ids) == len(set(ids)) == 40
    assert pending(client) == 0
    consumers = {c["name"] for c in client.xinfo_consumers(STREAM, GROUP)}
    assert consumers == {"archiver-new"}  # the old name is removed once its entries moved


def test_claiming_works_in_batches(client, store) -> None:
    add_to_stream(client, STREAM, three_hours())
    archiver(client, store, consumer="a", batch_size=7).run(max_entries=30, final_flush=False)
    archiver(client, store, consumer="b", batch_size=7).run(idle_exit_s=0)
    assert sorted(archived_ids(store)) == sorted(e.event_id for e in three_hours())
    assert pending(client) == 0
