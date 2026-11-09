"""Backfilling the lake from the stream's history (no network: a fake source)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.lake.helpers import fake_client, local_store
from wikipulse.lake.backfill import Backfill, HourCollector, parse_kept_edit
from wikipulse.lake.lock import LockLostError, WriterLock, writer_lock_key
from wikipulse.stream.resilience import Backoff

T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


def raw(n: int, minute: float, wiki: str = "enwiki", namespace: int = 0) -> str:
    """A raw `recentchange` message `minute` minutes after T0."""
    moment = (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
    return json.dumps(
        {
            "meta": {"id": f"id-{n}", "dt": moment, "domain": "en.wikipedia.org"},
            "wiki": wiki,
            "namespace": namespace,
            "type": "edit",
            "title": f"Page {n % 3}",
            "user": "Alice",
            "bot": False,
            "minor": False,
            "revision": {"old": n, "new": n + 1},
            "length": {"old": 10, "new": 20},
            "comment": "c",
        }
    )


class FakeSource:
    """Yields the messages after `last_event_id`; fails once after `fail_after` messages."""

    def __init__(self, messages: list[str], fail_after: int | None = None) -> None:
        self.items = messages
        self.fail_after = fail_after
        self.connections: list[str | None] = []

    async def messages_from(self, start: int):
        for i in range(start, len(self.items)):
            if self.fail_after is not None and i == self.fail_after:
                self.fail_after = None
                raise ConnectionError("network blip")
            yield str(i), self.items[i]

    def messages(self, last_event_id: str | None = None):
        self.connections.append(last_event_id)
        start = 0 if last_event_id is None else int(last_event_id) + 1
        return self.messages_from(start)


def test_parse_kept_edit_applies_the_stage_1_filter():
    assert parse_kept_edit(raw(1, 0)).event_id == "id-1"
    assert parse_kept_edit(raw(1, 0, wiki="dewiki")) is None
    assert parse_kept_edit(raw(1, 0, namespace=1)) is None
    assert parse_kept_edit('{"wiki": "enwiki", broken') is None


def test_hour_collector_releases_an_hour_after_it_settled():
    collector = HourCollector(until=T0 + timedelta(hours=3))
    collector.add(parse_kept_edit(raw(1, 30)))
    collector.add(parse_kept_edit(raw(2, 62)))
    assert collector.ready() == []  # 10:00 is over, but only 2 minutes ago
    collector.add(parse_kept_edit(raw(3, 66)))
    assert collector.ready() == [T0]
    collector.add(parse_kept_edit(raw(4, 200)))  # after `until`: not kept, but moves time
    assert collector.done()
    assert sorted(collector.buffers) == [T0, T0 + timedelta(hours=1)]


def run_backfill(tmp_path, source, until):
    store = local_store(tmp_path / "lake")
    lock = WriterLock(fake_client(), writer_lock_key(store.root), ttl_s=60)
    assert lock.acquire()
    backfill = Backfill(source, store, lock, until)

    async def go():
        stop = asyncio.Event()
        backoff = Backoff(base_s=0.001, max_s=0.001)
        while not stop.is_set():  # what run_with_reconnect does, without the waiting
            try:
                await backfill.read_once(stop, backoff)
            except ConnectionError:
                continue
        backfill.write_ready(everything=True)

    asyncio.run(go())
    return store, backfill


def test_backfill_writes_hours_until_the_end_and_resumes_after_a_failure(tmp_path):
    messages = [raw(n, n * 2) for n in range(150)]  # 10:00 to about 15:00
    messages.insert(40, raw(999, 0, wiki="dewiki"))  # another wiki: skipped
    source = FakeSource(messages, fail_after=70)
    store, backfill = run_backfill(tmp_path, source, until=T0 + timedelta(hours=3))
    assert source.connections == [None, "69"]  # resumed after the last message it had
    assert store.hours() == [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)]
    assert [store.read_hour(h).num_rows for h in store.hours()] == [30, 30, 30]
    assert backfill.collector.done()


def test_running_the_same_backfill_twice_adds_no_duplicates(tmp_path):
    messages = [raw(n, n * 2) for n in range(100)]
    until = T0 + timedelta(hours=2)
    run_backfill(tmp_path, FakeSource(messages), until)
    store, _ = run_backfill(tmp_path, FakeSource(messages), until)
    assert [store.read_hour(h).num_rows for h in store.hours()] == [30, 30]


def test_backfill_refuses_to_write_when_another_writer_holds_the_lake(tmp_path):
    store = local_store(tmp_path / "lake")
    client = fake_client()
    key = writer_lock_key(store.root)
    other = WriterLock(client, key, ttl_s=60)
    assert other.acquire()
    mine = WriterLock(client, key, ttl_s=60)
    backfill = Backfill(FakeSource([]), store, mine, T0)
    with pytest.raises(LockLostError):
        backfill.write_hour(T0, [parse_kept_edit(raw(1, 0))])
