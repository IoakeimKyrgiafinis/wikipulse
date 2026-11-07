"""The runner's crash recovery on the real Redis from Docker Compose (`-m integration`)."""

import uuid
from collections.abc import Iterator

import pytest
import redis

from tests.detect.helpers import config, fixture_events, make_edit
from tests.detect.test_runner import LOW, ListSink
from wikipulse.config import get_settings
from wikipulse.detect.detector import SpikeDetector
from wikipulse.detect.runner import DetectionRunner, DetectKeys, reset_run

pytestmark = pytest.mark.integration


@pytest.fixture
def client() -> Iterator[redis.Redis]:
    connection = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield connection
    connection.close()


@pytest.fixture
def stream(client: redis.Redis) -> Iterator[str]:
    name = f"wikipulse-test:{uuid.uuid4().hex}:edits"
    start = fixture_events()[0].event_time
    events = fixture_events() + [
        make_edit("Breaking", 2 + i * 0.1, user=f"U{i % 4}", start=start) for i in range(8)
    ]
    for event in events:
        client.xadd(name, {"data": event.model_dump_json(), "title": event.title})
    yield name
    client.delete(name)


def runner_for(client, stream: str, keys: DetectKeys, sink: ListSink) -> DetectionRunner:
    runner = DetectionRunner(client, stream, keys, SpikeDetector(config(**LOW)), sink, block_ms=1)
    runner.ensure_group("0")
    runner.restore()
    return runner


def test_crash_between_snapshots_loses_and_doubles_nothing(client, stream) -> None:
    ref_keys = DetectKeys.for_run(f"test-ref-{uuid.uuid4().hex}")
    keys = DetectKeys.for_run(f"test-crash-{uuid.uuid4().hex}")
    try:
        reference = ListSink()
        runner_for(client, stream, ref_keys, reference).run(idle_exit_s=0)

        sink = ListSink()
        runner_for(client, stream, keys, sink).run(max_entries=40)  # snapshot at 40
        crashing = runner_for(client, stream, keys, sink)
        crashing.snapshot = lambda: None  # killed before its next snapshot
        crashing.run(max_entries=70)
        assert client.xpending(stream, keys.group)["pending"] >= 70

        resumed = runner_for(client, stream, keys, sink)
        resumed.run(idle_exit_s=0)
        assert resumed.processed == client.xlen(stream) - 40
        assert client.xpending(stream, keys.group)["pending"] == 0
        assert sink.final == reference.final
        assert "Breaking" in {s.title for s in sink.final}
    finally:
        for k in (ref_keys, keys):
            reset_run(client, stream, k)
