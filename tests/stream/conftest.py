"""Fixtures for stream tests: an in-memory fake Redis and a consumer using it."""

import fakeredis
import pytest

from wikipulse.stream.consumer import Consumer, RedisKeys


@pytest.fixture
def fake_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def redis_client(fake_server: fakeredis.FakeServer) -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True)


@pytest.fixture
def keys() -> RedisKeys:
    return RedisKeys.for_namespace("test:edits", "test:consumer")


@pytest.fixture
def consumer(redis_client: fakeredis.FakeAsyncRedis, keys: RedisKeys) -> Consumer:
    return Consumer(redis_client, keys, maxlen=10_000)
