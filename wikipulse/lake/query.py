"""Ad-hoc SQL over the archive: `uv run python -m wikipulse.lake.query "<SQL>"`.

The archive is available as the table `edits`: every column of the Parquet files
(event_id, event_time, title, user, is_bot, ...) plus the partition columns year, month,
day and hour, read from the folder names. Examples:

    uv run python -m wikipulse.lake.query "SELECT day, hour, count(*) AS edits FROM edits
        GROUP BY ALL ORDER BY ALL"
    uv run python -m wikipulse.lake.query "SELECT title, count(*) AS n FROM edits
        WHERE day = 7 GROUP BY title ORDER BY n DESC LIMIT 10"

DuckDB runs the SQL; pyarrow reads the files from S3 and hands DuckDB only the columns
and partitions the query needs (a filter on year/month/day/hour skips whole folders).
"""

import argparse
import csv
import logging
import sys
import time

import duckdb

from wikipulse.config import get_settings
from wikipulse.lake.store import LakeStore
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

TABLE_NAME = "edits"


def connect(store: LakeStore) -> duckdb.DuckDBPyConnection:
    """An in-memory DuckDB connection with the archive registered as `edits`."""
    connection = duckdb.connect()
    connection.execute("SET TimeZone = 'UTC'")  # show times in UTC, like the partitions
    connection.register(TABLE_NAME, store.dataset())
    return connection


def write_csv(relation: duckdb.DuckDBPyRelation) -> None:
    """Print a query result as CSV (header first)."""
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(relation.columns)
    writer.writerows(relation.fetchall())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sql", help=f"the query; the archive is the table `{TABLE_NAME}`")
    parser.add_argument("--prefix", help="key prefix in the bucket (default: LAKE_PREFIX)")
    parser.add_argument("--csv", action="store_true", help="print CSV instead of a table")
    parser.add_argument("--max-rows", type=int, default=100, help="rows shown (default 100)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    sys.stdout.reconfigure(encoding="utf-8")  # titles are Unicode, even when piped
    connection = connect(LakeStore.from_settings(settings, args.prefix))
    started = time.perf_counter()
    try:
        relation = connection.sql(args.sql)
        if relation is None:  # a statement without a result
            return 0
        if args.csv:
            write_csv(relation.limit(args.max_rows))
        else:
            relation.show(max_rows=args.max_rows, max_width=200)
    except duckdb.Error as exc:
        print(f"Query failed: {exc}", file=sys.stderr)
        return 1
    logger.info("Query took %.2f s", time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
