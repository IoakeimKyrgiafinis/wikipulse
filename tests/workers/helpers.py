"""Shared helpers for worker tests: a local lake, an in-memory stats table, events."""

import json
from pathlib import Path

import fakeredis
import pyarrow.fs as pafs

from tests.lake.helpers import edit
from wikipulse.lake.partitions import part_path
from wikipulse.lake.store import LakeStore
from wikipulse.lake.table import events_to_table
from wikipulse.workers.hourly_stats import HandlerContext, HourStats, handle
from wikipulse.workers.invoker import Invoker
from wikipulse.workers.notifications import ObjectRef

PREFIX = "edits"


class MemoryStats:
    """The `hourly_stats` upsert, in a dict (same rule: newer files are never replaced)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, object], HourStats] = {}
        self.writes = 0

    def upsert(self, stats: HourStats) -> bool:
        key = (stats.dataset, stats.hour)
        stored = self.rows.get(key)
        if stored is not None and stored.source_modified > stats.source_modified:
            return False
        self.rows[key] = stats
        self.writes += 1
        return True

    def stored(self, dataset_prefix: str):
        return {
            key: (row.source_size, row.source_modified)
            for key, row in self.rows.items()
            if key[0] == dataset_prefix or key[0].startswith(dataset_prefix + "/")
        }


def edit_by(
    event_id: str, minute: float, title: str = "Page", user: str = "Alice", is_bot: bool = False
):
    """An edit `minute` minutes after 12:00 UTC on 7 November 2026, by `user`."""
    return edit(event_id, minute, title, is_bot).model_copy(update={"user": user})


def local_lake(tmp_path: Path, prefix: str = PREFIX) -> tuple[str, LakeStore]:
    """A local folder standing in for a bucket, and a LakeStore for `<bucket>/<prefix>`."""
    bucket = tmp_path.as_posix()
    return bucket, LakeStore(pafs.LocalFileSystem(), f"{bucket}/{prefix}")


def write_hour(store: LakeStore, edits) -> None:
    store.write_hour(edits[0].event_time, events_to_table(edits))


def key_of(hour, prefix: str = PREFIX) -> str:
    return part_path(prefix, hour)


def context(stats: MemoryStats | None = None, top_n: int = 10) -> HandlerContext:
    return HandlerContext(fs=pafs.LocalFileSystem(), stats=stats or MemoryStats(), top_n=top_n)


def invoker_for(ctx: HandlerContext, client=None, **kwargs) -> Invoker:
    kwargs.setdefault("sleep", lambda seconds: None)
    return Invoker(
        lambda event: handle(event, ctx),
        client or fakeredis.FakeRedis(decode_responses=True),
        "test:dead",
        **kwargs,
    )


def s3_message(bucket: str, key: str, event_name: str = "s3:ObjectCreated:Put") -> str:
    """A notification as RustFS publishes it (key URL-encoded, like S3 does)."""
    encoded = key.replace("/", "%2F").replace("=", "%3D")
    return json.dumps(
        {
            "EventName": event_name,
            "Key": f"{bucket}/{key}",
            "Records": [
                {
                    "eventVersion": "2.1",
                    "eventSource": "rustfs:s3",
                    "eventTime": "2026-11-09T15:56:12.375Z",
                    "eventName": event_name,
                    "s3": {
                        "bucket": {"name": bucket},
                        "object": {"key": encoded, "size": 492, "eTag": "e7d5-1"},
                    },
                }
            ],
        }
    )


def event_for(bucket: str, key: str) -> ObjectRef:
    return ObjectRef(bucket=bucket, key=key)
