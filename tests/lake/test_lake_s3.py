"""The lake on the real S3 server and Redis from Docker Compose (`-m integration`).

Every test works under its own random prefix in the bucket `wikipulse-test` and on its
own Redis stream, and deletes both afterwards, so the real lake is never touched.
"""

import uuid
from collections.abc import Iterator

import pyarrow.fs as pafs
import pytest
import redis

from tests.detect.helpers import fixture_events
from tests.lake.helpers import T0, add_to_stream, archived_ids, archiver, edit
from wikipulse.config import get_settings
from wikipulse.lake import archive, query
from wikipulse.lake.store import LakeStore, s3_filesystem
from wikipulse.lake.table import events_to_table

pytestmark = pytest.mark.integration

TEST_BUCKET = "wikipulse-test"


@pytest.fixture
def settings():
    return get_settings().model_copy(update={"s3_bucket": TEST_BUCKET})


@pytest.fixture
def prefix(settings) -> Iterator[str]:
    name = f"test-{uuid.uuid4().hex}/edits"
    yield name
    filesystem = s3_filesystem(settings)
    filesystem.delete_dir(f"{TEST_BUCKET}/{name.split('/')[0]}")


@pytest.fixture
def store(settings, prefix) -> LakeStore:
    return LakeStore.from_settings(settings, prefix)


@pytest.fixture
def client() -> Iterator[redis.Redis]:
    connection = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield connection
    connection.close()


@pytest.fixture
def stream(client: redis.Redis) -> Iterator[str]:
    name = f"wikipulse-test:{uuid.uuid4().hex}:edits"
    yield name
    client.delete(name)


def test_hour_round_trip_and_atomic_replace(store) -> None:
    store.write_hour(T0, events_to_table([edit("a", 1), edit("b", 2)]))
    store.write_hour(T0, events_to_table([edit("c", 3)]))
    assert store.read_hour(T0).column("event_id").to_pylist() == ["c"]
    assert store.hours() == [T0]
    assert store.file_size(T0) > 0
    listing = store.fs.get_file_info(pafs.FileSelector(store.root, recursive=True))
    keys = [info.path for info in listing if info.is_file]
    assert keys == [store.path(T0)]


def test_archiver_survives_a_crash_mid_hour(client, stream, store) -> None:
    events = fixture_events() + [edit(f"x{i}", 60 + i) for i in range(30)]  # two hours
    add_to_stream(client, stream, events)
    archiver(client, store, stream, batch_size=50).run(max_entries=100)
    archiver(client, store, stream, batch_size=50).run(max_entries=40, final_flush=False)
    assert client.xpending(stream, "test:lake")["pending"] == 40
    archiver(client, store, stream).run(idle_exit_s=0)
    ids = archived_ids(store)
    assert len(ids) == len(set(ids)) == len({e.event_id for e in events})
    assert client.xpending(stream, "test:lake")["pending"] == 0


def test_archive_and_query_commands(client, stream, settings, prefix, capsys, monkeypatch) -> None:
    add_to_stream(client, stream, [edit("a", 1, "X"), edit("b", 2, "Y"), edit("c", 61, "X")])
    args = archive.parse_args(
        ["--stream-key", stream, "--group", "test:cli", "--prefix", prefix, "--exit-when-idle", "0"]
    )
    stats = archive.run(args, settings)
    assert (stats.entries, stats.hour_writes) == (3, 2)

    monkeypatch.setattr(query, "get_settings", lambda: settings)
    sql = "SELECT hour, count(*) AS edits FROM edits GROUP BY hour ORDER BY hour"
    assert query.main([sql, "--prefix", prefix, "--csv"]) == 0
    assert capsys.readouterr().out.splitlines() == ["hour,edits", "12,2", "13,1"]
