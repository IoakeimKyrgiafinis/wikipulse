"""The Prometheus metrics: each service updates them where the work happens.

The metrics live in the client library's global registry, so other tests in the same
process move them too: every test compares a value before and after (`delta`).
"""

import json
import urllib.request
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from prometheus_client import REGISTRY

from tests.detect.test_runner import KEYS, STREAM, ListSink, make_runner, replay_fixture
from tests.llm.helpers import ScriptedClient
from tests.stream.helpers import fixture_messages, fixture_raws
from wikipulse.config import Settings
from wikipulse.llm.client import ChatRequest
from wikipulse.llm.errors import LLMError
from wikipulse.metrics.server import start_metrics_server
from wikipulse.stream.consumer import Consumer, RedisKeys
from wikipulse.stream.filters import is_wanted
from wikipulse.stream.lag import ReplayLag, WallClockLag


def value(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Delta:
    """`with Delta(name, **labels) as d: ...` then `d.value` = how much it moved."""

    def __init__(self, name: str, **labels: str) -> None:
        self.name, self.labels = name, labels

    def __enter__(self) -> "Delta":
        self.before = value(self.name, **self.labels)
        return self

    def __exit__(self, *exc: object) -> None:
        self.value = value(self.name, **self.labels) - self.before


T0 = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)


# --- lag ---


def test_wall_clock_lag_is_now_minus_the_newest_event() -> None:
    now = [T0]
    lag = WallClockLag(now=lambda: now[0])
    assert lag.seconds() != lag.seconds()  # NaN before the first event
    lag.observe(T0 - timedelta(seconds=30))
    lag.observe(T0 - timedelta(seconds=90))  # an older event doesn't move it back
    assert lag.seconds() == 30
    now[0] = T0 + timedelta(minutes=5)  # nothing arrives: the lag grows by itself
    assert lag.seconds() == 330


def test_replay_lag_runs_on_the_replays_event_time_clock() -> None:
    clock = [100.0]
    lag = ReplayLag(speed=10, monotonic=lambda: clock[0])
    lag.observe(T0)  # a week-old recording is not "a week behind"
    assert lag.seconds() == 0
    clock[0] += 3  # 3 s of wall time = 30 s of replay time
    lag.observe(T0 + timedelta(seconds=25))
    assert lag.seconds() == pytest.approx(5)
    clock[0] += 60  # the replay stopped writing: it falls behind its own clock
    assert lag.seconds() == pytest.approx(605)


def test_replay_at_max_speed_has_no_lag() -> None:
    lag = ReplayLag(speed=None)
    lag.observe(T0)
    assert lag.seconds() == 0


# --- the consumer (and so the replay, which uses the same class) ---


async def test_consumer_counts_messages_by_outcome_and_tracks_the_newest_event() -> None:
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    consumer = Consumer(redis, RedisKeys.for_namespace("m:edits", "m:consumer"), 10_000)
    messages = fixture_messages()
    wanted = sum(is_wanted(raw) for raw in fixture_raws())
    name = "wikipulse_consumer_messages_total"
    with Delta(name, outcome="kept") as kept, Delta(name, outcome="discarded") as discarded:
        for sse_id, data in messages:
            await consumer.process(sse_id, data)
    with Delta(name, outcome="duplicates") as duplicates, Delta(name, outcome="malformed") as bad:
        await consumer.process(*messages[-1])  # the same message again
        await consumer.process(None, "not json")
    assert (kept.value, discarded.value) == (wanted, len(messages) - wanted)
    assert duplicates.value == (1 if is_wanted(json.loads(messages[-1][1])) else 0)
    assert bad.value == 1
    newest = max(
        datetime.fromisoformat(entry["event_time"]).timestamp()
        for _, entry in await redis.xrange("m:edits")
    )
    assert value("wikipulse_consumer_last_event_timestamp_seconds") == pytest.approx(newest)
    assert value("wikipulse_consumer_lag_seconds") == pytest.approx(consumer.lag.seconds(), abs=5)


async def test_reconnects_are_counted() -> None:
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    consumer = Consumer(redis, RedisKeys.for_namespace("m:edits", "m:consumer"), 10_000)
    with Delta("wikipulse_consumer_reconnects_total") as reconnects:
        await consumer.count_reconnect()
    assert reconnects.value == 1


# --- the detector ---


async def test_detector_counts_entries_spikes_and_its_queue() -> None:
    server = fakeredis.FakeServer()
    await replay_fixture(server)
    client = fakeredis.FakeRedis(server=server, decode_responses=True)
    with (
        Delta("wikipulse_detector_entries_total") as entries,
        Delta("wikipulse_spikes_started_total", window="short") as short,
        Delta("wikipulse_spikes_started_total", window="long") as long,
    ):
        runner = make_runner(client, ListSink())
        runner.run(idle_exit_s=0)
    assert entries.value == client.xlen(STREAM)
    started = short.value + long.value
    assert started > 0
    assert started == len({(s.title, s.started_at) for s in runner_spikes(runner)})
    assert value("wikipulse_spikes_active") == len(runner.detector.active)
    assert value("wikipulse_detector_pending_entries") == 0  # all acknowledged at the end
    assert value("wikipulse_detector_stream_lag_entries") == 0  # nothing left to deliver
    assert client.xinfo_groups(STREAM)[0]["name"] == KEYS.group


def runner_spikes(runner) -> list:
    """Every spike the run started: the active ones plus those already ended."""
    return [*runner.sink.final]


# --- the LLM client ---


def test_llm_requests_latency_and_tokens_are_recorded() -> None:
    client = ScriptedClient(["hello"], model="metrics-test")
    labels = {"backend": "fake", "model": "metrics-test"}
    with (
        Delta("wikipulse_llm_requests_total", **labels, outcome="ok") as ok,
        Delta("wikipulse_llm_tokens_total", **labels, direction="input") as tokens_in,
        Delta("wikipulse_llm_tokens_total", **labels, direction="output") as tokens_out,
        Delta("wikipulse_llm_request_duration_seconds_count", **labels) as timed,
    ):
        client.chat("hi")
    assert (ok.value, tokens_in.value, tokens_out.value, timed.value) == (1, 10, 5, 1)


def test_llm_errors_are_counted_and_raised() -> None:
    class Broken(ScriptedClient):
        def _send(self, request: ChatRequest):
            raise LLMError("model not found")

    client = Broken([], model="metrics-broken")
    labels = {"backend": "fake", "model": "metrics-broken", "outcome": "error"}
    with Delta("wikipulse_llm_requests_total", **labels) as errors:
        with pytest.raises(LLMError):
            client.chat("hi")
    assert errors.value == 1


# --- the metrics server ---


def test_no_port_no_server() -> None:
    assert start_metrics_server(Settings(_env_file=None, metrics_port=None)) is None


def test_the_server_serves_the_metrics_on_loopback() -> None:
    settings = Settings(_env_file=None, metrics_port=1, metrics_host="127.0.0.1")
    # Port 0 = any free port; the setting must be 1..65535, so set it after validation.
    settings.metrics_port = 0
    server = start_metrics_server(settings)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as reply:
            text = reply.read().decode()
        assert "wikipulse_consumer_messages_total" in text
        assert "_created" not in text  # disable_created_metrics() in the catalog
    finally:
        server.shutdown()
        server.server_close()


def test_a_consumer_exports_every_outcome_from_the_start() -> None:
    from prometheus_client import generate_latest

    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    Consumer(redis, RedisKeys.for_namespace("m:edits", "m:consumer"), 10_000)
    text = generate_latest().decode()
    for outcome in ("kept", "duplicates", "discarded", "malformed"):
        assert f'wikipulse_consumer_messages_total{{outcome="{outcome}"}}' in text
