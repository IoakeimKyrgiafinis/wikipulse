"""Sources: the recording file format, FileSource replay pacing, and LiveSource over fake HTTP."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tests.stream.helpers import FIXTURE, fixture_messages
from wikipulse.stream.sources import (
    FileSource,
    LiveSource,
    event_timestamp,
    format_recording_line,
    parse_recording_line,
)


async def collect(source, last_event_id=None) -> list:
    return [message async for message in source.messages(last_event_id)]


def raw_at(seconds: float) -> str:
    """A minimal raw payload whose meta.dt is `seconds` after midnight."""
    dt = datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
    return json.dumps({"meta": {"dt": dt}})


def write_recording(path: Path, messages: list[tuple[str | None, str]]) -> None:
    received = datetime(2026, 1, 1, tzinfo=UTC)
    lines = [format_recording_line(sse_id, data, received) for sse_id, data in messages]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_recording_line_round_trip() -> None:
    line = format_recording_line('[{"a":1}]', '{"x": "ü"}', datetime(2026, 1, 1, tzinfo=UTC))
    assert parse_recording_line(line) == ('[{"a":1}]', '{"x": "ü"}')


def test_broken_recording_line_is_passed_on_as_malformed_data() -> None:
    assert parse_recording_line('{"id": "1", "da') == (None, '{"id": "1", "da')


async def test_file_source_reads_the_fixture() -> None:
    messages = await collect(FileSource(FIXTURE))
    assert messages == fixture_messages()
    assert all(sse_id for sse_id, _ in messages)


async def test_file_source_resumes_after_last_event_id(tmp_path: Path) -> None:
    path = tmp_path / "rec.jsonl"
    write_recording(path, [("a", "1"), ("b", "2"), ("c", "3")])
    assert await collect(FileSource(path), last_event_id="b") == [("c", "3")]


async def test_speed_paces_by_event_time(tmp_path: Path) -> None:
    now = [100.0]  # a fake monotonic clock that only fake_sleep advances
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    path = tmp_path / "rec.jsonl"
    events = [("1", raw_at(0)), ("2", raw_at(10)), ("3", raw_at(5)), ("4", "x"), ("5", raw_at(30))]
    write_recording(path, events)
    source = FileSource(path, speed=10, sleep=fake_sleep, clock=lambda: now[0])
    assert len(await collect(source)) == 5
    # 10 s of event time at 10x is 1 s, then 20 s more is 2 s. The out-of-order and the
    # broken message don't wait.
    assert sleeps == [pytest.approx(1.0), pytest.approx(2.0)]


async def test_speed_max_never_sleeps(tmp_path: Path) -> None:
    async def fail_sleep(seconds: float) -> None:
        raise AssertionError("should not sleep")

    path = tmp_path / "rec.jsonl"
    write_recording(path, [("1", raw_at(0)), ("2", raw_at(1000))])
    assert len(await collect(FileSource(path, speed=None, sleep=fail_sleep))) == 2


def test_invalid_speed_is_rejected() -> None:
    with pytest.raises(ValueError):
        FileSource(FIXTURE, speed=0)


def test_event_timestamp() -> None:
    assert event_timestamp(raw_at(42)) == 42
    assert event_timestamp("not json") is None
    assert event_timestamp("{}") is None


def test_live_source_request_args() -> None:
    source = LiveSource("https://example.org/stream", "TestAgent/1.0", since="2026-01-01T00:00Z")
    headers, params = source.request_args(None)
    assert headers == {"User-Agent": "TestAgent/1.0"}
    assert params == {"since": "2026-01-01T00:00Z"}
    headers, params = source.request_args("[{...}]")
    assert headers == {"User-Agent": "TestAgent/1.0", "Last-Event-ID": "[{...}]"}
    assert params == {}  # resuming by id wins over `since`


async def test_live_source_parses_server_sent_events() -> None:
    requests: list[httpx.Request] = []
    body = (
        ":ok\n\n"
        'event: message\nid: first\ndata: {"n": 1}\n\n'
        'event: message\nid: second\ndata: {"n": 2}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    source = LiveSource(
        "https://example.org/stream", "TestAgent/1.0", transport=httpx.MockTransport(handler)
    )
    assert await collect(source, last_event_id="zero") == [
        ("first", '{"n": 1}'),
        ("second", '{"n": 2}'),
    ]
    assert requests[0].headers["User-Agent"] == "TestAgent/1.0"
    assert requests[0].headers["Last-Event-ID"] == "zero"


async def test_live_source_raises_on_http_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    source = LiveSource("https://example.org/stream", "TestAgent/1.0", transport=transport)
    with pytest.raises(httpx.HTTPStatusError):
        await collect(source)
