"""The processing core against an in-memory Redis (fakeredis runs the real Lua script).

Covers writing, deduplication, checkpoints, malformed messages, reconnects and
graceful shutdown, all without the network or Docker.
"""

import asyncio
import json

import fakeredis
import pytest

from tests.stream.helpers import FakeSource, fixture_messages, fixture_raws, with_unique_ids
from wikipulse.stream.consumer import Consumer, Outcome, RedisKeys
from wikipulse.stream.filters import is_wanted
from wikipulse.stream.models import EditEvent
from wikipulse.stream.resilience import Backoff
from wikipulse.stream.sources import FileSource

WANTED_IDS = [raw["meta"]["id"] for raw in fixture_raws() if is_wanted(raw)]


def fast_backoff() -> Backoff:
    return Backoff(base_s=0.001, max_s=0.01)


async def stream_event_ids(redis: fakeredis.FakeAsyncRedis, keys: RedisKeys) -> list[str]:
    entries = await redis.xrange(keys.stream)
    return [json.loads(fields["data"])["event_id"] for _, fields in entries]


async def stats(redis: fakeredis.FakeAsyncRedis, keys: RedisKeys) -> dict[str, int]:
    return {k: int(v) for k, v in (await redis.hgetall(keys.stats)).items() if v.isdigit()}


async def process_all(consumer: Consumer, messages) -> list[Outcome]:
    return [await consumer.process(sse_id, data) for sse_id, data in messages]


async def test_fixture_is_filtered_and_written(consumer, redis_client, keys) -> None:
    messages = fixture_messages()
    outcomes = await process_all(consumer, messages)
    assert outcomes.count(Outcome.KEPT) == len(WANTED_IDS)
    assert outcomes.count(Outcome.DISCARDED) == len(messages) - len(WANTED_IDS)
    assert await stream_event_ids(redis_client, keys) == WANTED_IDS
    counters = await stats(redis_client, keys)
    assert counters["received"] == len(messages)
    assert counters["kept"] == len(WANTED_IDS)
    assert counters["discarded"] == len(messages) - len(WANTED_IDS)


async def test_stream_entry_fields(consumer, redis_client, keys) -> None:
    await process_all(consumer, fixture_messages())
    _, fields = (await redis_client.xrange(keys.stream, count=1))[0]
    event = EditEvent.model_validate_json(fields["data"])
    assert fields["title"] == event.title
    assert fields["event_time"] == event.event_time.isoformat()
    assert event.event_id == WANTED_IDS[0]
    assert (await redis_client.hgetall(keys.stats))["last_event_time"]


async def test_checkpoint_is_saved_after_every_message(consumer, redis_client, keys) -> None:
    for sse_id, data in with_unique_ids(fixture_messages())[:20]:
        await consumer.process(sse_id, data)
        assert await consumer.load_checkpoint() == sse_id  # kept or discarded alike


async def test_checkpoint_starts_empty(consumer) -> None:
    assert await consumer.load_checkpoint() is None


async def test_duplicates_are_skipped(consumer, redis_client, keys) -> None:
    messages = fixture_messages()
    await process_all(consumer, messages)
    outcomes = await process_all(consumer, messages)  # the server resends everything
    assert outcomes.count(Outcome.DUPLICATE) == len(WANTED_IDS)
    assert outcomes.count(Outcome.KEPT) == 0
    assert await stream_event_ids(redis_client, keys) == WANTED_IDS
    assert (await stats(redis_client, keys))["duplicates"] == len(WANTED_IDS)


async def test_dedup_keys_expire_after_an_hour(consumer, redis_client, keys) -> None:
    await process_all(consumer, fixture_messages()[:50])
    ttl = await redis_client.ttl(keys.seen_prefix + WANTED_IDS[0])
    assert 3500 < ttl <= 3600


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        "[1, 2, 3]",
        '{"wiki": "enwiki", "namespace": 0, "type": "edit", "meta": {"dt": "2026-01-01T00:00Z"}}',
        '{"wiki": "enwiki", "namespace": 0, "type": "edit", "meta": "oops", "title": "X"}',
    ],
)
async def test_malformed_messages_are_counted_not_fatal(
    consumer, redis_client, keys, payload
) -> None:
    assert await consumer.process("bad-1", payload) == Outcome.MALFORMED
    assert await consumer.load_checkpoint() == "bad-1"  # don't get stuck on it after a restart
    good_id, good_data = fixture_messages()[0]
    await consumer.process(good_id, good_data)  # and keep going
    counters = await stats(redis_client, keys)
    assert counters["malformed"] == 1
    assert counters["received"] == 2


async def test_consume_from_file_source(consumer, redis_client, keys) -> None:
    from tests.stream.helpers import FIXTURE

    count = await consumer.consume(FileSource(FIXTURE), asyncio.Event())
    assert count == len(fixture_messages())
    assert await stream_event_ids(redis_client, keys) == WANTED_IDS


async def test_reconnect_after_source_fails_mid_stream(consumer, redis_client, keys) -> None:
    messages = with_unique_ids(fixture_messages())
    source = FakeSource(messages, fail_after=100, block_at_end=True)
    stop = asyncio.Event()

    async def stop_when_done() -> None:
        while await consumer.load_checkpoint() != messages[-1][0]:
            await asyncio.sleep(0.01)
        stop.set()

    watcher = asyncio.create_task(stop_when_done())
    await asyncio.wait_for(consumer.run_live(source, stop, fast_backoff()), timeout=10)
    await watcher
    # First connection had no checkpoint; the second resumed from message 100's id.
    assert source.connections == [None, messages[99][0]]
    assert (await stats(redis_client, keys))["reconnects"] == 1
    assert await stream_event_ids(redis_client, keys) == WANTED_IDS  # nothing lost or doubled


async def test_reconnect_when_server_closes_the_stream(consumer, redis_client, keys) -> None:
    messages = with_unique_ids(fixture_messages())[:10]
    source = FakeSource(messages)  # ends without an error, like a server closing the stream
    stop = asyncio.Event()

    async def stop_after_second_connection() -> None:
        while len(source.connections) < 2:
            await asyncio.sleep(0.001)
        stop.set()

    watcher = asyncio.create_task(stop_after_second_connection())
    await asyncio.wait_for(consumer.run_live(source, stop, fast_backoff()), timeout=10)
    await watcher
    assert source.connections[:2] == [None, messages[-1][0]]
    assert (await stats(redis_client, keys))["reconnects"] >= 1


async def test_resume_from_checkpoint_after_restart(redis_client, keys) -> None:
    messages = with_unique_ids(fixture_messages())
    first = Consumer(redis_client, keys, maxlen=10_000)
    await process_all(first, messages[:120])
    # A new process: same Redis, fresh Consumer, source resumes from the checkpoint.
    second = Consumer(redis_client, keys, maxlen=10_000)
    source = FakeSource(messages)
    await second.consume(source, asyncio.Event(), await second.load_checkpoint())
    assert source.connections == [messages[119][0]]
    assert await stream_event_ids(redis_client, keys) == WANTED_IDS


async def test_graceful_shutdown_while_waiting(consumer, redis_client, keys) -> None:
    messages = with_unique_ids(fixture_messages())[:30]
    source = FakeSource(messages, block_at_end=True)  # a quiet stream: nothing more arrives
    stop = asyncio.Event()
    task = asyncio.create_task(consumer.run_live(source, stop, fast_backoff()))
    while await consumer.load_checkpoint() != messages[-1][0]:
        await asyncio.sleep(0.01)
    stop.set()  # what Ctrl+C does
    await asyncio.wait_for(task, timeout=2)  # returns promptly and cleanly
    assert (await stats(redis_client, keys)).get("reconnects", 0) == 0


async def test_graceful_shutdown_finishes_the_current_write(consumer, redis_client, keys) -> None:
    messages = with_unique_ids(fixture_messages())
    source = FakeSource(messages)
    stop = asyncio.Event()
    process = consumer.process
    stop_at = messages[10][0]

    async def process_then_signal(sse_id, data):
        if sse_id == stop_at:
            stop.set()  # Ctrl+C arrives while this message is being handled
            await asyncio.sleep(0.01)
        return await process(sse_id, data)

    consumer.process = process_then_signal
    await asyncio.wait_for(consumer.run_live(source, stop, fast_backoff()), timeout=2)
    assert await consumer.load_checkpoint() == stop_at  # that message was completed and saved
    counters = await stats(redis_client, keys)
    assert counters["received"] == 11  # and nothing after it


async def test_live_consumer_beats_its_heartbeat(consumer) -> None:
    messages = with_unique_ids(fixture_messages())[:5]
    source = FakeSource(messages, block_at_end=True)
    stop = asyncio.Event()
    beats: list[int] = []
    task = asyncio.create_task(
        consumer.run_live(source, stop, fast_backoff(), heartbeat=lambda: beats.append(1))
    )
    while await consumer.load_checkpoint() != messages[-1][0]:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(beats) == 1 + len(messages)  # one per connection attempt, one per message


async def test_with_log_edits_each_written_edit_logs_its_correlation_id(
    redis_client, keys, caplog
) -> None:
    consumer = Consumer(redis_client, keys, maxlen=10_000, log_edits=True)
    with caplog.at_level("INFO", logger="wikipulse.stream.consumer"):
        await process_all(consumer, fixture_messages())
    written = [r for r in caplog.records if r.getMessage().startswith("Edit written")]
    assert [r.correlation_id for r in written] == WANTED_IDS


async def test_without_log_edits_nothing_is_logged_per_edit(consumer, caplog) -> None:
    with caplog.at_level("INFO", logger="wikipulse.stream.consumer"):
        await process_all(consumer, fixture_messages())
    assert not [r for r in caplog.records if r.getMessage().startswith("Edit written")]
