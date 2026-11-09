"""Backfill: handle lake hours that never got (or lost) their notification.

Notifications can be missed: files written before notifications were set up, files
written while the worker was stopped (a Redis pub/sub message reaches only the
subscribers listening at that moment), or a worker that crashed mid-invocation. The
backfill finds them by comparing the lake with the table instead of trusting the
notifications: it lists every hour's files and invokes the handler for each hour that

- has no `hourly_stats` row ("missing"), or
- has a row computed from other files than the ones there now ("stale": the stored
  size or last-modified time differs, so the hour was rewritten since).

It goes through the same `Invoker` as live notifications (same retries, same
dead-letter list), and because the handler is idempotent it is always safe to run.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import pyarrow.fs as pafs

from wikipulse.workers.hourly_stats import HourLocation, SourceFiles, locate
from wikipulse.workers.invoker import Invoker
from wikipulse.workers.notifications import ObjectRef

logger = logging.getLogger(__name__)

BACKFILL_EVENT = "wikipulse:Backfill"

StoredLookup = Callable[[str], dict[tuple[str, datetime], tuple[int, datetime]]]


def lake_hours(
    fs: pafs.FileSystem, bucket: str, prefix: str = ""
) -> dict[HourLocation, SourceFiles]:
    """Every hour with data files under `<bucket>/<prefix>`, with its files."""
    root = f"{bucket}/{prefix.strip('/')}" if prefix.strip("/") else bucket
    if fs.get_file_info(root).type != pafs.FileType.Directory:
        return {}
    grouped: dict[HourLocation, list[pafs.FileInfo]] = {}
    for info in fs.get_file_info(pafs.FileSelector(root, recursive=True)):
        if info.type != pafs.FileType.File:
            continue
        location = locate(bucket, info.path[len(bucket) + 1 :])
        if location is not None:
            grouped.setdefault(location, []).append(info)
    return {
        location: SourceFiles(
            paths=tuple(sorted(info.path for info in infos)),
            size=sum(info.size for info in infos),
            modified=max(info.mtime for info in infos),
        )
        for location, infos in grouped.items()
    }


@dataclass
class BackfillReport:
    hours: int = 0  # hours found in the lake
    missing: int = 0
    stale: int = 0
    succeeded: int = 0
    failed: int = 0  # dead-lettered
    processed: list[HourLocation] = field(default_factory=list)


def backfill(
    fs: pafs.FileSystem,
    stored: StoredLookup,
    invoker: Invoker,
    bucket: str,
    prefix: str = "",
    force: bool = False,
) -> BackfillReport:
    """Invoke the handler for every missing or stale hour (every hour with `force`)."""
    found = lake_hours(fs, bucket, prefix)
    dataset_prefix = f"{bucket}/{prefix.strip('/')}" if prefix.strip("/") else bucket
    rows = stored(dataset_prefix)
    report = BackfillReport(hours=len(found))
    for location in sorted(found, key=lambda loc: (loc.dataset, loc.hour)):
        files = found[location]
        row = rows.get((location.dataset, location.hour))
        if row is None:
            report.missing += 1
        elif row != (files.size, files.modified):
            report.stale += 1
        elif not force:
            continue
        key = files.paths[0][len(bucket) + 1 :]
        event = ObjectRef(bucket=bucket, key=key, event_name=BACKFILL_EVENT, size=files.size)
        if invoker.invoke(event):
            report.succeeded += 1
        else:
            report.failed += 1
        report.processed.append(location)
    logger.info(
        "Backfill of %s: %d hours in the lake, %d missing, %d stale, %d handled, %d failed",
        dataset_prefix,
        report.hours,
        report.missing,
        report.stale,
        report.succeeded,
        report.failed,
    )
    return report
