"""Shared helpers for lake tests: hand-made edits, a fake stream, a local lake."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeredis
import pyarrow.fs as pafs

from wikipulse.lake.archiver import Archiver
from wikipulse.lake.store import LakeStore
from wikipulse.stream.models import EditEvent

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
RECEIVED = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)  # fixed, so rows are reproducible


def edit(event_id: str, minute: float, title: str = "Page", is_bot: bool = False) -> EditEvent:
    """An edit `minute` minutes after T0 (12:00 UTC); negative minutes are earlier hours."""
    return EditEvent(
        event_id=event_id,
        event_time=T0 + timedelta(minutes=minute),
        received_at=RECEIVED,
        title=title,
        type="edit",
        user="Alice",
        is_bot=is_bot,
        is_minor=False,
        rev_old=1,
        rev_new=2,
        len_old=100,
        len_new=110,
        comment="copyedit",
    )


def add_to_stream(client, stream: str, events: list[EditEvent]) -> list[str]:
    """XADD each edit the way the stage 1 consumer does; returns the entry ids."""
    return [
        client.xadd(
            stream,
            {
                "data": event.model_dump_json(),
                "title": event.title,
                "event_time": event.event_time.isoformat(),
            },
        )
        for event in events
    ]


def local_store(root: Path) -> LakeStore:
    """A lake in a local folder (the same code as S3, through pyarrow's filesystem API)."""
    return LakeStore(pafs.LocalFileSystem(), root.as_posix())


def fake_client() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis(decode_responses=True)


def archiver(client, store: LakeStore, stream: str = "test:edits", **kwargs) -> Archiver:
    """An archiver that never waits: no blocking reads, no wall-clock flushes."""
    kwargs.setdefault("flush_interval_s", 1e9)
    kwargs.setdefault("block_ms", 1)
    worker = Archiver(client, stream, "test:lake", store, **kwargs)
    worker.ensure_group("0")
    return worker


def archived_ids(store: LakeStore) -> list[str]:
    """Every event_id in the lake, in file order (duplicates kept, to detect them)."""
    ids: list[str] = []
    for hour in store.hours():
        table = store.read_hour(hour)
        assert table is not None
        ids.extend(table.column("event_id").to_pylist())
    return ids
