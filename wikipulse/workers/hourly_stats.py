"""The hourly statistics handler: one lake hour in, one `hourly_stats` row out.

This is the "function" of the serverless pattern. It is invoked with one event (an
`ObjectRef`: "this object was created") and a context holding the clients it needs
(the object store and the statistics table), and it keeps no state of its own between
invocations. Everything it knows comes from the event and from the files, so any number
of copies could run, be restarted or be replaced at any time.

It is *idempotent*: it never adds to counts. It reads every file of the hour the object
belongs to, computes the hour's numbers from scratch and upserts them under
(dataset, hour). The same notification delivered twice, a backfill running over an
hour that was already handled, or the archiver rewriting the hour ten times all end in
the same row.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from wikipulse.lake.partitions import parse_partition, partition_path
from wikipulse.lake.table import EDIT_SCHEMA, conform
from wikipulse.workers.notifications import ObjectRef

logger = logging.getLogger(__name__)

# Where the hour partition starts in a key: '<prefix>/year=2026/...' or 'year=2026/...'.
_PARTITION_START = re.compile(r"(?:^|/)year=\d{4}/")


@dataclass(frozen=True)
class HourLocation:
    """An hour of one archive: `<bucket>/<prefix>/year=.../hour=HH/`."""

    bucket: str
    prefix: str
    hour: datetime

    @property
    def dataset(self) -> str:
        """'<bucket>/<prefix>', the archive this hour belongs to (e.g. 'wikipulse-lake/edits')."""
        return f"{self.bucket}/{self.prefix}" if self.prefix else self.bucket

    @property
    def folder(self) -> str:
        """The partition folder as a filesystem path (bucket included)."""
        return partition_path(self.dataset, self.hour)


def locate(bucket: str, key: str) -> HourLocation | None:
    """The hour a Parquet key belongs to, or None if it isn't an hourly data file."""
    name = key.rsplit("/", 1)[-1]
    if not name.endswith(".parquet") or name.startswith("."):
        return None
    hour = parse_partition(key)
    match = _PARTITION_START.search(key)
    if hour is None or match is None:
        return None
    prefix = key[: match.start()].strip("/")
    return HourLocation(bucket=bucket, prefix=prefix, hour=hour)


@dataclass(frozen=True)
class SourceFiles:
    """The data files of one hour, as the handler found them."""

    paths: tuple[str, ...]
    size: int  # bytes, all files
    modified: datetime  # the newest file's last-modified time


def hour_files(fs: pafs.FileSystem, folder: str) -> SourceFiles | None:
    """Every data file in an hour's folder, or None if there is none (any more)."""
    if fs.get_file_info(folder).type != pafs.FileType.Directory:
        return None
    infos = [
        info
        for info in fs.get_file_info(pafs.FileSelector(folder, recursive=False))
        if info.type == pafs.FileType.File
        and info.base_name.endswith(".parquet")
        and not info.base_name.startswith(".")
    ]
    if not infos:
        return None
    infos.sort(key=lambda info: info.path)
    return SourceFiles(
        paths=tuple(info.path for info in infos),
        size=sum(info.size for info in infos),
        modified=max(info.mtime for info in infos),
    )


@dataclass(frozen=True)
class TopPage:
    title: str
    edits: int
    editors: int


@dataclass(frozen=True)
class HourStats:
    """One row of `hourly_stats`."""

    dataset: str
    hour: datetime
    edits: int
    distinct_pages: int
    distinct_editors: int
    bot_edits: int
    bot_share: float
    top_pages: tuple[TopPage, ...]
    source_files: int
    source_size: int
    source_modified: datetime


@dataclass(frozen=True)
class Counts:
    """The numbers computed from an hour's edits (no file metadata)."""

    edits: int
    distinct_pages: int
    distinct_editors: int
    bot_edits: int
    bot_share: float
    top_pages: tuple[TopPage, ...]


def compute_counts(table: pa.Table, top_n: int = 10) -> Counts:
    """Edits, distinct pages and editors, bot share and the most edited pages.

    Top pages are ordered by edits, then by distinct editors, then by title, so the
    same rows always give the same list.
    """
    table = conform(table)
    edits = table.num_rows
    if edits == 0:
        return Counts(0, 0, 0, 0, 0.0, ())
    bot_edits = int(pc.sum(table["is_bot"].cast(pa.int64())).as_py())
    per_page = table.group_by("title", use_threads=False).aggregate(
        [("title", "count"), ("user", "count_distinct")]
    )
    ranked = per_page.sort_by(
        [
            ("title_count", "descending"),
            ("user_count_distinct", "descending"),
            ("title", "ascending"),
        ]
    ).slice(0, top_n)
    top_pages = tuple(
        TopPage(title=row["title"], edits=row["title_count"], editors=row["user_count_distinct"])
        for row in ranked.to_pylist()
    )
    return Counts(
        edits=edits,
        distinct_pages=per_page.num_rows,
        distinct_editors=int(pc.count_distinct(table["user"]).as_py()),
        bot_edits=bot_edits,
        bot_share=bot_edits / edits,
        top_pages=top_pages,
    )


def read_files(fs: pafs.FileSystem, files: SourceFiles) -> pa.Table:
    """All rows of the hour's files, in the archive's schema."""
    tables = [conform(pq.read_table(path, filesystem=fs)) for path in files.paths]
    return pa.concat_tables(tables) if tables else EDIT_SCHEMA.empty_table()


class StatsSink(Protocol):
    """Where the handler writes its row (Postgres in production, a dict in unit tests)."""

    def upsert(self, stats: HourStats) -> bool:
        """Insert or replace the row of (dataset, hour); False if the stored row is newer."""
        ...


@dataclass
class HandlerContext:
    """What every invocation gets besides its event: clients built once, outside it."""

    fs: pafs.FileSystem
    stats: StatsSink
    top_n: int = 10


Outcome = Literal["written", "kept-newer", "skipped"]


@dataclass(frozen=True)
class HandlerResult:
    outcome: Outcome
    location: HourLocation | None = None
    stats: HourStats | None = None


def handle(event: ObjectRef, context: HandlerContext) -> HandlerResult:
    """Recompute the statistics of the hour `event`'s object belongs to.

    Objects that aren't hourly data files, and hours whose files are gone by now, are
    skipped (retrying couldn't change that). Any other problem raises, so the caller can
    retry and finally dead-letter the event.
    """
    location = locate(event.bucket, event.key)
    if location is None:
        logger.debug("Not an hourly data file, skipped: %s/%s", event.bucket, event.key)
        return HandlerResult("skipped")
    files = hour_files(context.fs, location.folder)
    if files is None:
        logger.info("No files left for %s (deleted?), skipped", location.folder)
        return HandlerResult("skipped", location)
    counts = compute_counts(read_files(context.fs, files), context.top_n)
    stats = HourStats(
        dataset=location.dataset,
        hour=location.hour,
        **counts.__dict__,
        source_files=len(files.paths),
        source_size=files.size,
        source_modified=files.modified,
    )
    written = context.stats.upsert(stats)
    logger.info(
        "%s %s: %d edits, %d pages, %d editors, %.0f%% bots%s",
        location.dataset,
        f"{location.hour:%Y-%m-%d %H}:00",
        stats.edits,
        stats.distinct_pages,
        stats.distinct_editors,
        100 * stats.bot_share,
        "" if written else " (kept the stored row: it is from newer files)",
    )
    return HandlerResult("written" if written else "kept-newer", location, stats)
