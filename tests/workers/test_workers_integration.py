"""The worker against the real RustFS, Redis and Postgres (`-m integration`).

Writing a file to the bucket must make the object store publish a notification, and the
listening worker must turn it into an `hourly_stats` row. Every test uses its own bucket
(`wikipulse-test-worker`, which only these tests configure), its own random prefix,
dead-letter key and dataset rows, and deletes them afterwards.
"""

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.workers.helpers import edit_by
from wikipulse.config import get_settings
from wikipulse.db.hourly_stats import (
    delete_dataset,
    get_hourly_stats,
    stored_sources,
    upsert_hourly_stats,
)
from wikipulse.db.migrate import migrate
from wikipulse.lake.store import LakeStore, s3_filesystem
from wikipulse.lake.table import events_to_table
from wikipulse.workers.hourly_stats import HourStats, TopPage
from wikipulse.workers.invoker import dead_letters
from wikipulse.workers.listener import NotificationListener
from wikipulse.workers.run import Worker, build_worker
from wikipulse.workers.setup import setup_notifications

pytestmark = pytest.mark.integration

TEST_BUCKET = "wikipulse-test-worker"
T12 = datetime(2026, 11, 7, 12, tzinfo=UTC)


@pytest.fixture
def settings():
    return get_settings().model_copy(
        update={"s3_bucket": TEST_BUCKET, "worker_retry_delay_s": 0.1, "worker_max_attempts": 2}
    )


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as connection:
        migrate(connection)
        yield connection


@pytest.fixture
def prefix(settings, conn) -> Iterator[str]:
    name = f"test-{uuid.uuid4().hex}/edits"
    yield name
    top = name.split("/")[0]
    filesystem = s3_filesystem(settings)
    if filesystem.get_file_info(f"{TEST_BUCKET}/{top}").type.name != "NotFound":
        filesystem.delete_dir(f"{TEST_BUCKET}/{top}")
    delete_dataset(conn, f"{TEST_BUCKET}/{top}")


@pytest.fixture
def worker(settings, prefix) -> Iterator[Worker]:
    setup_notifications(settings, TEST_BUCKET)
    dead_key = f"wikipulse-test:{uuid.uuid4().hex}:dead"
    built = build_worker(settings, TEST_BUCKET, dead_key)
    yield built
    built.client.delete(dead_key)
    built.close()


@pytest.fixture
def store(settings, prefix) -> LakeStore:
    return LakeStore.from_settings(settings, prefix)


def edits_at(hour: datetime, n: int, title: str = "Page") -> list:
    minutes = (hour - T12).total_seconds() / 60
    return [
        edit_by(f"{title}-{hour:%H%M}-{i}", minutes + i, title, f"User{i % 3}") for i in range(n)
    ]


def wait_for(condition: Callable[[], object], timeout_s: float = 20.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("condition not met in time")


class RunningListener:
    """The worker's listener in a background thread, subscribed before the test writes."""

    def __init__(self, worker: Worker, settings) -> None:
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.listener = NotificationListener(
            worker.client,
            settings.lake_notify_channel,
            worker.invoker,
            worker.bucket,
            catch_up=self.ready.set,
            poll_timeout_s=0.2,
        )
        self.thread = threading.Thread(target=self.listener.run, args=(self.stop.is_set,))

    def __enter__(self) -> "RunningListener":
        self.thread.start()
        assert self.ready.wait(10), "listener did not subscribe"
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join(10)


def test_writing_a_file_triggers_the_stats_row(worker, settings, store, conn, prefix) -> None:
    dataset = f"{TEST_BUCKET}/{prefix}"
    with RunningListener(worker, settings):
        store.write_hour(T12, events_to_table(edits_at(T12, 6)))
        row = wait_for(lambda: get_hourly_stats(conn, dataset, T12))
        assert (row["edits"], row["distinct_pages"], row["distinct_editors"]) == (6, 1, 3)
        assert row["top_pages"] == [{"title": "Page", "edits": 6, "editors": 3}]
        assert row["source_size"] == store.file_size(T12)

        # The archiver rewrites the hour with more rows: the row follows.
        store.merge_hour(T12, events_to_table(edits_at(T12 + timedelta(minutes=30), 4, "Other")))
        row = wait_for(
            lambda: (r := get_hourly_stats(conn, dataset, T12)) and r["edits"] == 10 and r
        )
        assert row["distinct_pages"] == 2
    assert worker.invoker.stats.dead_lettered == 0


def test_a_failing_handler_ends_up_in_the_dead_letter_list(worker, settings, prefix) -> None:
    key = f"{prefix}/year=2026/month=11/day=07/hour=13/part-0.parquet"
    with RunningListener(worker, settings):
        with worker.context.fs.open_output_stream(f"{TEST_BUCKET}/{key}") as out:
            out.write(b"deliberately not a Parquet file")
        letters = wait_for(lambda: dead_letters(worker.client, worker.invoker.dead_letter_key))
    [letter] = letters
    assert letter.event.bucket == TEST_BUCKET and letter.event.key == key
    assert letter.event.event_name.startswith("s3:ObjectCreated:")
    assert letter.attempts == 2
    assert "parquet" in letter.error.lower()


def test_backfill_handles_files_that_never_got_a_notification(worker, store, conn, prefix) -> None:
    # Nobody listens: the notifications of these writes are lost.
    for hour in (T12, T12 + timedelta(hours=1)):
        store.write_hour(hour, events_to_table(edits_at(hour, 3)))
    top = prefix.split("/")[0]
    report = worker.backfill(top)
    assert (report.hours, report.missing, report.succeeded) == (2, 2, 2)
    assert len(stored_sources(conn, f"{TEST_BUCKET}/{top}")) == 2
    assert worker.backfill(top).succeeded == 0  # nothing left to do


def test_upsert_is_idempotent_and_keeps_newer_files(conn, prefix) -> None:
    dataset = f"{TEST_BUCKET}/{prefix}"
    newer = datetime(2026, 11, 9, 16, tzinfo=UTC)
    stats = HourStats(dataset, T12, 5, 2, 3, 1, 0.2, (TopPage("Ü", 4, 2),), 1, 1234, newer)
    assert upsert_hourly_stats(conn, stats) is True
    assert upsert_hourly_stats(conn, stats) is True  # same row again: same result
    row = get_hourly_stats(conn, dataset, T12)
    assert (row["edits"], row["top_pages"]) == (5, [{"title": "Ü", "edits": 4, "editors": 2}])
    older = HourStats(dataset, T12, 1, 1, 1, 0, 0.0, (), 1, 99, newer - timedelta(minutes=5))
    assert upsert_hourly_stats(conn, older) is False
    assert get_hourly_stats(conn, dataset, T12)["edits"] == 5
    count = conn.execute(
        "SELECT count(*) FROM hourly_stats WHERE dataset = %s", (dataset,)
    ).fetchone()[0]
    assert count == 1
