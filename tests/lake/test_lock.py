"""The single-writer lock: one archiver per lake, waiting instead of overwriting."""

import pytest

from tests.lake.helpers import T0, add_to_stream, archived_ids, archiver, edit, fake_client
from tests.lake.helpers import local_store as make_store
from wikipulse.lake.lock import LockLostError, WriterLock, writer_lock_key

STREAM = "test:edits"
GROUP = "test:lake"


@pytest.fixture
def client():
    return fake_client()


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path / "lake")


def test_only_one_owner_at_a_time(client) -> None:
    first, second = WriterLock(client, "k", 60), WriterLock(client, "k", 60)
    assert first.acquire()
    assert not second.acquire()
    assert first.renew()
    first.release()
    assert client.get("k") is None
    assert second.acquire()


def test_the_lock_has_a_ttl(client) -> None:
    lock = WriterLock(client, "k", 30)
    lock.acquire()
    assert 0 < client.pttl("k") <= 30_000


def test_a_lost_lock_is_neither_renewed_nor_deleted(client) -> None:
    lock = WriterLock(client, "k", 60)
    lock.acquire()
    client.set("k", "someone-else")  # expired, then taken by another archiver
    assert not lock.renew()
    assert not lock.owned
    lock.release()
    assert client.get("k") == "someone-else"


def test_lock_key_is_per_lake(store) -> None:
    assert writer_lock_key(store.root) == f"lake:writer:{store.root}"


def test_a_second_archiver_waits_and_writes_nothing(client, store) -> None:
    add_to_stream(client, STREAM, [edit("a", 1), edit("b", 2)])
    holder = WriterLock(client, writer_lock_key(store.root), 60)
    assert holder.acquire()  # the first archiver is running
    sleeps: list[float] = []
    second = archiver(client, store, sleep=sleeps.append)
    stats = second.run(should_stop=lambda: len(sleeps) >= 3)
    assert len(sleeps) == 3  # it kept waiting...
    assert stats.entries == 0 and stats.hour_writes == 0  # ...without reading or writing
    assert store.hours() == []
    assert client.xpending(STREAM, GROUP)["pending"] == 0
    assert holder.renew()  # and never touched the first one's lock


def test_the_waiting_archiver_takes_over_when_the_lock_is_free(client, store) -> None:
    add_to_stream(client, STREAM, [edit("a", 1), edit("b", 2)])
    holder = WriterLock(client, writer_lock_key(store.root), 60)
    holder.acquire()
    second = archiver(client, store, sleep=lambda _: holder.release())  # the first one stops
    second.run(idle_exit_s=0)
    assert sorted(archived_ids(store)) == ["a", "b"]


def test_the_lock_is_released_when_the_archiver_stops(client, store) -> None:
    add_to_stream(client, STREAM, [edit("a", 1)])
    archiver(client, store).run(idle_exit_s=0)
    assert client.get(writer_lock_key(store.root)) is None


def test_losing_the_lock_stops_before_writing(client, store) -> None:
    add_to_stream(client, STREAM, [edit("a", 1), edit("b", 61)])
    worker = archiver(client, store)

    def steal(hour, table):
        client.set(writer_lock_key(store.root), "another-archiver")  # after the first hour
        return original(hour, table)

    original = store.merge_hour
    store.merge_hour = steal
    with pytest.raises(LockLostError):
        worker.run(idle_exit_s=0)
    assert store.hours() == [T0]  # the second hour was not written...
    assert client.xpending(STREAM, GROUP)["pending"] == 1  # ...nor acknowledged
    assert client.get(writer_lock_key(store.root)) == "another-archiver"


def test_the_lock_is_renewed_while_running(client, store) -> None:
    clock = [0.0]
    add_to_stream(client, STREAM, [edit(f"e{i}", i) for i in range(10)])
    worker = archiver(client, store, lock_ttl_s=30, batch_size=2, monotonic=lambda: clock[0])
    renewals: list[bool] = []
    renew = worker.lock.renew

    def counting_renew() -> bool:
        renewals.append(renew())
        return renewals[-1]

    worker.lock.renew = counting_renew

    def tick() -> bool:
        clock[0] += 11  # more than a third of the TTL per loop
        return False

    worker.run(should_stop=tick, max_entries=6, final_flush=False)
    assert renewals and all(renewals)
