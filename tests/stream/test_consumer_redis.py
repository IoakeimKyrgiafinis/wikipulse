"""Integration tests against the real Redis from Docker Compose (`-m integration`).

Each test uses its own key namespace and deletes it afterwards, so it never touches
the live consumer's data.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis

from tests.stream.helpers import FakeSource, fixture_messages, fixture_raws, with_unique_ids
from wikipulse.config import get_settings
from wikipulse.stream.consumer import Consumer, Outcome, RedisKeys
from wikipulse.stream.filters import is_wanted
from wikipulse.stream.models import EditEvent

pytestmark = pytest.mark.integration

WANTED_IDS = [raw["meta"]["id"] for raw in fixture_raws() if is_wanted(raw)]


@pytest.fixture
async def real_redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def test_keys(real_redis: aioredis.Redis) -> AsyncIterator[RedisKeys]:
    namespace = f"wikipulse-test:{uuid.uuid4().hex}"
    yield RedisKeys.for_namespace(f"{namespace}:edits", namespace)
    async for key in real_redis.scan_iter(match=f"{namespace}:*", count=1000):
        await real_redis.delete(key)


async def stream_event_ids(client: aioredis.Redis, keys: RedisKeys) -> list[str]:
    entries = await client.xrange(keys.stream)
    return [json.loads(fields["data"])["event_id"] for _, fields in entries]


async def test_writes_to_redis_stream(real_redis, test_keys) -> None:
    consumer = Consumer(real_redis, test_keys, maxlen=10_000)
    for sse_id, data in fixture_messages():
        await consumer.process(sse_id, data)
    assert await real_redis.xlen(test_keys.stream) == len(WANTED_IDS)
    _, fields = (await real_redis.xrange(test_keys.stream, count=1))[0]
    assert set(fields) == {"data", "title", "event_time"}
    event = EditEvent.model_validate_json(fields["data"])
    assert (fields["title"], fields["event_time"]) == (event.title, event.event_time.isoformat())
    stats = await real_redis.hgetall(test_keys.stats)
    assert int(stats["kept"]) == len(WANTED_IDS)
    assert await consumer.load_checkpoint() == fixture_messages()[-1][0]


async def test_duplicates_are_skipped_in_real_redis(real_redis, test_keys) -> None:
    consumer = Consumer(real_redis, test_keys, maxlen=10_000)
    for _ in range(2):
        for sse_id, data in fixture_messages():
            await consumer.process(sse_id, data)
    assert await stream_event_ids(real_redis, test_keys) == WANTED_IDS
    assert int(await real_redis.hget(test_keys.stats, "duplicates")) == len(WANTED_IDS)


async def test_stream_is_trimmed_approximately(real_redis, test_keys) -> None:
    maxlen = 20
    consumer = Consumer(real_redis, test_keys, maxlen=maxlen)
    template = json.loads(
        next(data for _, data in fixture_messages() if is_wanted(json.loads(data)))
    )
    for i in range(1000):
        template["meta"]["id"] = f"trim-{i}"
        assert await consumer.process(f"id-{i}", json.dumps(template)) == Outcome.KEPT
    length = await real_redis.xlen(test_keys.stream)
    # "~" lets Redis trim whole internal nodes (100 entries by default), so the length
    # stays near maxlen instead of exactly at it, but far below the 1000 written.
    assert maxlen <= length <= maxlen + 100
    newest = await real_redis.xrevrange(test_keys.stream, count=1)
    assert json.loads(newest[0][1]["data"])["event_id"] == "trim-999"


async def test_resume_from_checkpoint_after_crash(real_redis, test_keys) -> None:
    messages = with_unique_ids(fixture_messages())
    first = Consumer(real_redis, test_keys, maxlen=10_000)
    for sse_id, data in messages[:150]:
        await first.process(sse_id, data)
    # "Crash": throw the consumer away. A new one resumes from the checkpoint in Redis.
    # The server resends a few messages from before the checkpoint, as a timestamp-based
    # Last-Event-ID can; deduplication must absorb them.
    second = Consumer(real_redis, test_keys, maxlen=10_000)
    checkpoint = await second.load_checkpoint()
    assert checkpoint == messages[149][0]
    resend_from = messages[139][0]
    source = FakeSource(messages)
    await second.consume(source, asyncio.Event(), last_event_id=resend_from)
    assert await stream_event_ids(real_redis, test_keys) == WANTED_IDS
    assert await second.load_checkpoint() == messages[-1][0]
