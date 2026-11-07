"""The consumer-group runner, on an in-memory Redis (fakeredis).

The stream is filled by replaying the recorded fixture through the stage-1 `Consumer`,
plus a few synthetic bursts, exactly as `replay` would fill it.
"""

import json

import fakeredis
import pytest

from tests.detect.helpers import config, fixture_events, make_edit
from tests.stream.helpers import fixture_messages
from wikipulse.detect.detector import SpikeDetector, SpikeRecord
from wikipulse.detect.runner import DetectionRunner, DetectKeys, entry_id_key, reset_run
from wikipulse.stream.consumer import Consumer, RedisKeys

STREAM = "test:edits"
KEYS = DetectKeys.for_run("test")
LOW = {"min_edits": 2, "min_editors": 2, "min_ratio": 2}  # the fixture is only 2 minutes


class ListSink:
    """Collects written spikes; `final` keeps the last version of each, like an upsert."""

    def __init__(self) -> None:
        self.writes: list[list[SpikeRecord]] = []

    def write(self, spikes: list[SpikeRecord]) -> None:
        self.writes.append(spikes)

    @property
    def final(self) -> list[SpikeRecord]:
        latest = {(s.title, s.started_at): s for batch in self.writes for s in batch}
        return sorted(latest.values(), key=lambda s: (s.started_at, s.title))


@pytest.fixture
def server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def client(server: fakeredis.FakeServer) -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(server=server, decode_responses=True)


async def replay_fixture(server: fakeredis.FakeServer) -> None:
    """Fill STREAM like `replay` does: the fixture through the stage-1 consumer."""
    redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    consumer = Consumer(redis, RedisKeys.for_namespace(STREAM, "test:replay"), maxlen=10_000)
    for sse_id, data in fixture_messages():
        await consumer.process(sse_id, data)
    start = fixture_events()[0].event_time
    for i in range(8):  # a synthetic burst at the end of the fixture
        event = make_edit("Breaking", 2 + i * 0.1, user=f"U{i % 4}", start=start)
        await redis.xadd(STREAM, {"data": event.model_dump_json(), "title": event.title})
    await redis.aclose()


def make_runner(client, sink: ListSink, **kwargs) -> DetectionRunner:
    runner = DetectionRunner(
        client, STREAM, KEYS, SpikeDetector(config(**LOW)), sink, block_ms=1, **kwargs
    )
    runner.ensure_group("0")
    runner.restore()
    return runner


async def test_detects_spikes_in_a_replayed_fixture(server, client) -> None:
    await replay_fixture(server)
    sink = ListSink()
    processed = make_runner(client, sink).run(idle_exit_s=0)
    assert processed == client.xlen(STREAM)
    titles = {s.title for s in sink.final}
    assert {"Breaking", "Heaven (nightclub)"} <= titles
    assert client.xpending(STREAM, KEYS.group)["pending"] == 0  # everything acknowledged
    state = json.loads(client.get(KEYS.state))
    assert state["last_id"] == client.xrevrange(STREAM, count=1)[0][0]


async def test_bots_are_excluded_in_a_replay(server, client) -> None:
    await replay_fixture(server)
    runner = make_runner(client, ListSink())
    runner.run(idle_exit_s=0)
    assert runner.detector.clock.stats["bots"] == sum(e.is_bot for e in fixture_events())


async def test_crash_and_restart_give_the_same_spikes(server, client) -> None:
    await replay_fixture(server)
    reference = ListSink()
    reference_runner = DetectionRunner(
        client,
        STREAM,
        DetectKeys.for_run("reference"),
        SpikeDetector(config(**LOW)),
        reference,
        block_ms=1,
    )
    reference_runner.ensure_group("0")
    reference_runner.run(idle_exit_s=0)

    sink = ListSink()
    make_runner(client, sink).run(max_entries=60)  # stops gracefully, snapshot at 60
    crashing = make_runner(client, sink)
    crashing.snapshot = lambda: None  # dies before it can save anything
    crashing.run(max_entries=50)
    assert client.xpending(STREAM, KEYS.group)["pending"] > 0  # unacknowledged work

    resumed = make_runner(client, sink)
    assert resumed.last_id is not None
    resumed.run(idle_exit_s=0)
    assert resumed.processed == client.xlen(STREAM) - 60  # redid the crashed part
    assert sink.final == reference.final
    assert client.xpending(STREAM, KEYS.group)["pending"] == 0


async def test_restart_continues_with_new_entries_only(server, client) -> None:
    await replay_fixture(server)
    first = make_runner(client, ListSink())
    first.run(idle_exit_s=0)
    second = make_runner(client, ListSink())
    assert second.run(idle_exit_s=0) == 0
    event = make_edit("New page", 0, start=fixture_events()[-1].event_time)
    client.xadd(STREAM, {"data": event.model_dump_json()})
    assert make_runner(client, ListSink()).run(idle_exit_s=0) == 1


def test_malformed_and_trimmed_entries_are_skipped_and_acknowledged(client) -> None:
    client.xadd(STREAM, {"data": "not json"})
    client.xadd(STREAM, {"other": "field"})
    good = make_edit("Page", 0)
    client.xadd(STREAM, {"data": good.model_dump_json()})
    runner = make_runner(client, ListSink())
    assert runner.run(idle_exit_s=0) == 3
    assert runner.detector.clock.stats["events"] == 1
    assert client.xpending(STREAM, KEYS.group)["pending"] == 0


def test_ensure_group_is_idempotent_and_reset_forgets_the_run(client) -> None:
    client.xadd(STREAM, {"data": make_edit("Page", 0).model_dump_json()})
    runner = make_runner(client, ListSink())
    runner.ensure_group("0")  # second time: no error
    runner.run(idle_exit_s=0)
    assert client.exists(KEYS.state)
    reset_run(client, STREAM, KEYS)
    assert not client.exists(KEYS.state)
    assert all(group["name"] != KEYS.group for group in client.xinfo_groups(STREAM))


def test_entry_ids_compare_in_stream_order() -> None:
    assert entry_id_key("1700000000000-10") > entry_id_key("1700000000000-9")
    assert entry_id_key("1700000000001-0") > entry_id_key("1700000000000-99")
