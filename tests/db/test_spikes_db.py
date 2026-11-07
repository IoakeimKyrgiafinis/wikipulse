"""Integration tests against the real Postgres (and Redis) from Docker Compose.

Run with `uv run pytest -m integration`. Every test uses its own run id, schema or Redis
keys and removes them afterwards, so the live data is never touched.
"""

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
import redis

from tests.detect.helpers import fixture_events, make_edit
from wikipulse.config import Settings, get_settings
from wikipulse.db.migrate import migrate, migration_files
from wikipulse.db.spikes import (
    active_spikes,
    delete_run,
    latest_edit_time,
    spikes_for_run,
    upsert_spikes,
)
from wikipulse.detect import run as detect_run
from wikipulse.detect.detector import SpikeRecord
from wikipulse.detect.runner import DetectKeys

pytestmark = pytest.mark.integration

T = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as connection:
        migrate(connection)
        yield connection


@pytest.fixture
def run_id(conn: psycopg.Connection) -> Iterator[str]:
    rid = f"test-{uuid.uuid4().hex}"
    yield rid
    delete_run(conn, rid)


def spike(title: str, minute: int = 0, **changes) -> SpikeRecord:
    record = SpikeRecord(
        title=title,
        status="active",
        started_at=T + timedelta(minutes=minute),
        first_edit_at=T + timedelta(minutes=minute - 4),
        last_edit_at=T + timedelta(minutes=minute),
        ended_at=None,
        total_edits=5,
        distinct_editors=3,
        revert_edits=0,
        peak_edits_10m=5,
        score=6.4,
    )
    return replace(record, **changes)


def test_migrations_apply_once_in_a_fresh_schema() -> None:
    schema = f"test_{uuid.uuid4().hex}"
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            conn.execute(f'SET search_path TO "{schema}"')
            assert migrate(conn) == [path.stem for path in migration_files()]
            assert migrate(conn) == []  # second run: nothing new
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                    (schema,),
                )
            }
            assert {"spikes", "schema_migrations"} <= tables
        finally:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_insert_then_update_keeps_one_row(conn, run_id) -> None:
    upsert_spikes(conn, run_id, [spike("News")])
    ended = spike("News", status="ended", ended_at=T + timedelta(minutes=40), total_edits=30)
    upsert_spikes(conn, run_id, [ended])
    assert spikes_for_run(conn, run_id) == [ended]


def test_upsert_is_idempotent(conn, run_id) -> None:
    records = [spike("A"), spike("B", minute=5)]
    upsert_spikes(conn, run_id, records)
    upsert_spikes(conn, run_id, records)
    assert spikes_for_run(conn, run_id) == records
    assert upsert_spikes(conn, run_id, []) == 0


def test_queries(conn, run_id) -> None:
    upsert_spikes(
        conn,
        run_id,
        [
            spike("Low", score=4.0),
            spike("High", minute=1, score=9.0),
            spike("Done", minute=2, status="ended", ended_at=T + timedelta(minutes=3)),
        ],
    )
    assert [s.title for s in active_spikes(conn, run_id)] == ["High", "Low"]
    assert [s.title for s in active_spikes(conn, run_id, limit=1)] == ["High"]
    window = spikes_for_run(
        conn, run_id, since=T + timedelta(minutes=1), until=T + timedelta(minutes=2)
    )
    assert [s.title for s in window] == ["High"]
    assert latest_edit_time(conn, run_id) == T + timedelta(minutes=2)
    assert spikes_for_run(conn, "no-such-run") == []


def test_runs_are_separate(conn, run_id) -> None:
    other = f"{run_id}-other"
    upsert_spikes(conn, run_id, [spike("Shared")])
    upsert_spikes(conn, other, [spike("Shared", score=1.0)])
    try:
        assert spikes_for_run(conn, run_id)[0].score == 6.4
        assert delete_run(conn, other) == 1
    finally:
        delete_run(conn, other)


def test_detect_run_end_to_end_on_real_services(conn, run_id) -> None:
    """The CLI's run(): real Redis stream in, real Postgres rows out, twice the same."""
    settings = Settings(detect_min_edits=2, detect_min_editors=2, detect_min_ratio=2)
    client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    stream = f"wikipulse-test:{uuid.uuid4().hex}:edits"
    start = fixture_events()[0].event_time
    events = fixture_events() + [
        make_edit("Breaking", 2 + i * 0.1, user=f"U{i % 4}", start=start) for i in range(8)
    ]
    for event in events:
        client.xadd(stream, {"data": event.model_dump_json(), "title": event.title})
    argv = ["--stream-key", stream, "--run-id", run_id, "--reset", "--exit-when-idle", "0"]
    try:
        assert detect_run.run(detect_run.parse_args(argv), settings) == len(events)
        first = spikes_for_run(conn, run_id)
        assert {"Breaking", "Heaven (nightclub)"} <= {s.title for s in first}
        detect_run.run(detect_run.parse_args(argv), settings)  # --reset: from scratch again
        assert spikes_for_run(conn, run_id) == first
    finally:
        keys = DetectKeys.for_run(run_id)
        client.delete(stream, keys.state, keys.baselines)
        client.close()
