"""The `hourly_stats` table (migration 0006): per-hour statistics of the data lake.

Rows are keyed by (dataset, hour) and always replaced whole (an *upsert*), never added
to, which is what makes the worker's handler idempotent. The `WHERE` of the upsert keeps
a row computed from newer files: if two notifications of the same hour were handled out
of order (by two workers, say), the older files can't overwrite the newer numbers.
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from wikipulse.workers.hourly_stats import HourStats

logger = logging.getLogger(__name__)

UPSERT = """
INSERT INTO hourly_stats (dataset, hour, edits, distinct_pages, distinct_editors, bot_edits,
                          bot_share, top_pages, source_files, source_size, source_modified,
                          computed_at)
VALUES (%(dataset)s, %(hour)s, %(edits)s, %(distinct_pages)s, %(distinct_editors)s,
        %(bot_edits)s, %(bot_share)s, %(top_pages)s, %(source_files)s, %(source_size)s,
        %(source_modified)s, now())
ON CONFLICT (dataset, hour) DO UPDATE SET
    edits = EXCLUDED.edits, distinct_pages = EXCLUDED.distinct_pages,
    distinct_editors = EXCLUDED.distinct_editors, bot_edits = EXCLUDED.bot_edits,
    bot_share = EXCLUDED.bot_share, top_pages = EXCLUDED.top_pages,
    source_files = EXCLUDED.source_files, source_size = EXCLUDED.source_size,
    source_modified = EXCLUDED.source_modified, computed_at = now()
WHERE hourly_stats.source_modified <= EXCLUDED.source_modified
"""


def _params(stats: HourStats) -> dict[str, Any]:
    top_pages = [
        {"title": page.title, "edits": page.edits, "editors": page.editors}
        for page in stats.top_pages
    ]
    return {
        "dataset": stats.dataset,
        "hour": stats.hour,
        "edits": stats.edits,
        "distinct_pages": stats.distinct_pages,
        "distinct_editors": stats.distinct_editors,
        "bot_edits": stats.bot_edits,
        "bot_share": stats.bot_share,
        "top_pages": json.dumps(top_pages, ensure_ascii=False),
        "source_files": stats.source_files,
        "source_size": stats.source_size,
        "source_modified": stats.source_modified,
    }


def upsert_hourly_stats(conn: psycopg.Connection, stats: HourStats) -> bool:
    """Insert or replace the row; False if the stored row came from newer files."""
    cursor = conn.execute(UPSERT, _params(stats))
    return cursor.rowcount == 1


def get_hourly_stats(conn: psycopg.Connection, dataset: str, hour: datetime) -> dict | None:
    """The stored row of (dataset, hour) as a dict, or None."""
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM hourly_stats WHERE dataset = %s AND hour = %s", (dataset, hour)
        )
        return cursor.fetchone()


def stored_sources(
    conn: psycopg.Connection, dataset_prefix: str
) -> dict[tuple[str, datetime], tuple[int, datetime]]:
    """(dataset, hour) -> (source_size, source_modified) of every row under a prefix.

    `dataset_prefix` is a bucket ('wikipulse-lake') or '<bucket>/<prefix>'.
    """
    rows = conn.execute(
        "SELECT dataset, hour, source_size, source_modified FROM hourly_stats"
        " WHERE dataset = %(p)s OR dataset LIKE %(like)s",
        {"p": dataset_prefix, "like": _escape_like(dataset_prefix) + "/%"},
    ).fetchall()
    return {(dataset, hour): (size, modified) for dataset, hour, size, modified in rows}


def delete_dataset(conn: psycopg.Connection, dataset_prefix: str) -> int:
    """Delete every row of a dataset (and of datasets under it). Returns the count."""
    cursor = conn.execute(
        "DELETE FROM hourly_stats WHERE dataset = %(p)s OR dataset LIKE %(like)s",
        {"p": dataset_prefix, "like": _escape_like(dataset_prefix) + "/%"},
    )
    return cursor.rowcount


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PostgresStats:
    """The handler's sink: upserts into `hourly_stats`, reconnecting when needed.

    A long-running worker outlives database restarts. The connection is made on first
    use and made again after it broke, so the next invocation (or the next retry of a
    failed one) works again without restarting the worker.
    """

    def __init__(self, connect: Callable[[], psycopg.Connection]) -> None:
        self._connect = connect
        self._conn: psycopg.Connection | None = None

    @classmethod
    def from_dsn(cls, dsn: str) -> "PostgresStats":
        return cls(lambda: psycopg.connect(dsn, autocommit=True, connect_timeout=10))

    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed or self._conn.broken:
            if self._conn is not None:
                logger.info("Reconnecting to Postgres")
                self._conn.close()
            self._conn = self._connect()
        return self._conn

    def upsert(self, stats: HourStats) -> bool:
        return upsert_hourly_stats(self.conn, stats)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
