"""The dataset build against the real S3 server and Postgres (`-m integration`).

Works under its own random prefix in the bucket `wikipulse-test` and with its own
detector run ids, and deletes all of it afterwards: the real lake, the real datasets and
the real spike runs are never touched.
"""

import uuid
from collections.abc import Iterator
from datetime import date

import psycopg
import pytest

from tests.ml.test_dataset import DAYS, burst, ticks
from wikipulse.config import get_settings
from wikipulse.db.spikes import delete_run, spikes_for_run
from wikipulse.eval.ground_truth import PortalDay
from wikipulse.lake.partitions import hour_of
from wikipulse.lake.store import LakeStore, s3_filesystem
from wikipulse.lake.table import events_to_table
from wikipulse.ml.dataset import BuildOptions, DatasetStore, build

pytestmark = pytest.mark.integration

TEST_BUCKET = "wikipulse-test"


@pytest.fixture
def settings():
    return get_settings().model_copy(
        update={"s3_bucket": TEST_BUCKET, "ml_valid_days": 1, "ml_test_days": 2}
    )


@pytest.fixture
def prefix(settings) -> Iterator[str]:
    name = f"test-{uuid.uuid4().hex}"
    yield name
    s3_filesystem(settings).delete_dir(f"{TEST_BUCKET}/{name}")


@pytest.fixture
def run_ids(settings) -> Iterator[tuple[str, str]]:
    suffix = uuid.uuid4().hex[:12]
    ids = (f"test-cand-{suffix}", f"test-rule-{suffix}")
    yield ids
    with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
        for run_id in ids:
            delete_run(conn, run_id)


def test_build_on_s3_and_postgres(settings, prefix, run_ids, tmp_path) -> None:
    lake = LakeStore.from_settings(settings, f"{prefix}/edits")
    events = [e for d in DAYS for e in ticks(d)]
    for d in range(2, 7):
        events += burst(d, f"News {d}", editors=6) + burst(d, f"Quiet {d}", editors=4, start_h=18)
    by_hour: dict = {}
    for event in events:
        by_hour.setdefault(hour_of(event.event_time), []).append(event)
    for hour, rows in by_hour.items():
        lake.merge_hour(hour, events_to_table(rows))
    datasets = DatasetStore(lake.fs, f"{TEST_BUCKET}/{prefix}/datasets")
    options = BuildOptions(
        run_id=run_ids[0],
        rule_run_id=run_ids[1],
        labels=tmp_path / "no-labels.jsonl",
        use_db=True,
        use_pages=False,
        lake=lake,
        datasets=datasets,
        portal=[PortalDay(date(2026, 11, d), [f"News {d}"], []) for d in range(1, 9)],
    )

    assert build(options, settings) == "v1"
    assert build(options, settings) == "v2"

    assert datasets.versions() == [1, 2]
    manifest = datasets.manifest("v2")
    assert manifest["rows"] == 10
    assert manifest["splits"]["test"]["days"] == ["2026-11-05", "2026-11-06"]
    assert datasets.read("v2", "train").num_rows == 4
    with psycopg.connect(settings.postgres_dsn) as conn:
        assert len(spikes_for_run(conn, run_ids[0])) == 10
        assert len(spikes_for_run(conn, run_ids[1])) == 5  # only the News bursts
