"""The writer lease: only one live consumer writes a stream at a time (fake Redis)."""

import asyncio
import json
import logging

import pytest

from tests.stream.helpers import FakeSource, fixture_messages, with_unique_ids
from wikipulse.stream.consume import consume_holding
from wikipulse.stream.consumer import Consumer, Outcome
from wikipulse.stream.filters import is_wanted
from wikipulse.stream.lease import LeaseLost, StreamLease, lease_key
from wikipulse.stream.resilience import Backoff


def fast_backoff() -> Backoff:
    return Backoff(base_s=0.001, max_s=0.01)


def make_lease(redis_client, keys, owner: str, ttl_s: float = 30) -> StreamLease:
    return StreamLease(redis_client, keys.stream, ttl_s, owner=owner)


def first_wanted(messages):
    """The index of the first message the consumer keeps (writes to the stream)."""
    return next(i for i, (_, data) in enumerate(messages) if is_wanted(json.loads(data)))


def test_lease_key_is_next_to_the_stream() -> None:
    assert lease_key("wiki:edits") == "wiki:edits:writer"


async def test_only_one_owner_holds_the_lease(redis_client, keys) -> None:
    first, second = make_lease(redis_client, keys, "a"), make_lease(redis_client, keys, "b")
    assert await first.try_acquire()
    assert await first.try_acquire()  # holding it already is fine
    assert not await second.try_acquire()
    assert await second.holder() == "a"
    await second.release()  # not its lease: nothing happens
    assert await first.holder() == "a"
    await first.release()
    assert await second.try_acquire()
    assert await redis_client.pttl(second.key) > 0  # always with a TTL


async def test_a_killed_holders_lease_expires_and_a_waiting_consumer_takes_over(
    redis_client, keys, caplog
) -> None:
    killed = make_lease(redis_client, keys, "old-pod", ttl_s=0.3)
    assert await killed.try_acquire()  # then it "dies": no renewal, no release
    waiting = make_lease(redis_client, keys, "new-pod", ttl_s=0.3)
    stop = asyncio.Event()
    beats = []
    with caplog.at_level(logging.WARNING):
        got = await asyncio.wait_for(
            waiting.acquire(stop, retry_s=0.05, on_wait=lambda: beats.append(1)), timeout=5
        )
    assert got
    assert await waiting.holder() == "new-pod"
    assert len(beats) > 1  # it stayed healthy while waiting
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # said once, not on every check
    assert "old-pod" in warnings[0] and "waiting" in warnings[0]


async def test_waiting_ends_when_stopped(redis_client, keys) -> None:
    assert await make_lease(redis_client, keys, "a").try_acquire()
    stop = asyncio.Event()
    stop.set()
    assert not await make_lease(redis_client, keys, "b").acquire(stop, retry_s=0.01)


async def test_keep_renews_and_stops_when_the_lease_is_taken(redis_client, keys) -> None:
    lease = make_lease(redis_client, keys, "a", ttl_s=0.3)
    assert await lease.try_acquire()
    stop = asyncio.Event()
    keeper = asyncio.create_task(lease.keep(stop))
    await asyncio.sleep(0.5)  # longer than the TTL: only renewals keep it
    assert await lease.holder() == "a"
    await redis_client.set(lease.key, "b")  # someone took it (e.g. after a long stall)
    assert not await asyncio.wait_for(keeper, timeout=2)
    assert stop.is_set()


async def test_a_consumer_without_the_lease_writes_nothing(redis_client, keys) -> None:
    lease = make_lease(redis_client, keys, "a")
    consumer = Consumer(redis_client, keys, maxlen=10_000, lease=lease)
    messages = with_unique_ids(fixture_messages())
    kept = first_wanted(messages)
    with pytest.raises(LeaseLost):  # never acquired
        await consumer.process(*messages[kept])
    assert await lease.try_acquire()
    assert await consumer.process(*messages[kept]) is Outcome.KEPT
    await redis_client.set(lease.key, "b")  # another consumer holds it now
    for message in (messages[kept + 1], ("id-x", "not json")):  # a write and a skip
        with pytest.raises(LeaseLost):
            await consumer.process(*message)
    assert await redis_client.xlen(keys.stream) == 1
    assert await consumer.load_checkpoint() == messages[kept][0]  # nothing moved


async def test_consume_holding_stops_when_another_consumer_takes_the_stream(
    redis_client, keys
) -> None:
    lease = make_lease(redis_client, keys, "a")
    assert await lease.try_acquire()
    consumer = Consumer(redis_client, keys, maxlen=10_000, lease=lease)
    messages = with_unique_ids(fixture_messages())
    source = FakeSource(messages)
    process = consumer.process
    taken_at = messages[50][0]

    async def take_over_mid_stream(sse_id, data):
        if sse_id == taken_at:
            await redis_client.set(lease.key, "b")
        return await process(sse_id, data)

    consumer.process = take_over_mid_stream
    stop = asyncio.Event()
    held = await asyncio.wait_for(
        consume_holding(lease, consumer, source, stop, fast_backoff()), timeout=5
    )
    assert not held
    assert source.connections == [None]  # stopped, no reconnect
    assert await consumer.load_checkpoint() == messages[49][0]
    assert await lease.holder() == "b"  # not released: it isn't ours


async def test_consume_holding_releases_the_lease_on_a_clean_stop(redis_client, keys) -> None:
    lease = make_lease(redis_client, keys, "a")
    assert await lease.try_acquire()
    consumer = Consumer(redis_client, keys, maxlen=10_000, lease=lease)
    messages = with_unique_ids(fixture_messages())[:30]
    source = FakeSource(messages, block_at_end=True)
    stop = asyncio.Event()
    task = asyncio.create_task(consume_holding(lease, consumer, source, stop, fast_backoff()))
    while await consumer.load_checkpoint() != messages[-1][0]:
        await asyncio.sleep(0.01)
    stop.set()
    assert await asyncio.wait_for(task, timeout=2)
    assert await lease.holder() is None  # a waiting consumer can start at once
