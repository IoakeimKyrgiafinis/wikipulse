"""Size report: one hour of the lake as Parquet compared with the same edits as JSON.

    uv run python scripts/lake_size_report.py [--prefix history/edits] [--hour 2026-11-05T14]

Reads the hour's Parquet file from the lake (default: the hour with the most edits) and
measures
- the stored Parquet file (zstd, as the archiver writes it), and the same rows as Parquet
  with snappy and without compression,
- the same edits as JSON Lines, exactly as they sit in the Redis Stream (`data` field),
  plain and gzipped,
- how long DuckDB takes for the same query over the JSON and over the Parquet copy
  (local files under data/lake-size/, median of 5 runs).

Prints a Markdown table and writes it, with the raw numbers, to
evals/results/<today>_lake-size.md and .json.
"""

import argparse
import gzip
import json
import logging
import statistics
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from wikipulse.config import get_settings
from wikipulse.lake.store import LakeStore
from wikipulse.logging_setup import setup_logging
from wikipulse.stream.models import EditEvent

logger = logging.getLogger("lake_size_report")

WORK_DIR = Path("data") / "lake-size"
RESULTS_DIR = Path("evals") / "results"
QUERY = "SELECT title, count(*) AS edits, avg(is_bot::int) AS bot_share FROM {source} "
QUERY += "GROUP BY title ORDER BY edits DESC, title LIMIT 10"
RUNS = 5


def parquet_bytes(table: pa.Table, compression: str | None) -> int:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression=compression or "none")
    return sink.getvalue().size


def json_lines(table: pa.Table) -> bytes:
    """The rows as the stream holds them: one EditEvent JSON document per line."""
    lines = [EditEvent(**row).model_dump_json() for row in table.to_pylist()]
    return ("\n".join(lines) + "\n").encode("utf-8")


def median_seconds(connection: duckdb.DuckDBPyConnection, sql: str) -> float:
    timings = []
    for _ in range(RUNS):
        started = time.perf_counter()
        connection.sql(sql).fetchall()
        timings.append(time.perf_counter() - started)
    return statistics.median(timings)


def pick_hour(store: LakeStore, wanted: str | None) -> datetime:
    if wanted:
        return datetime.strptime(wanted, "%Y-%m-%dT%H").replace(tzinfo=UTC)
    sizes = {hour: store.file_size(hour) or 0 for hour in store.hours()}
    if not sizes:
        raise SystemExit(f"The lake at {store.root} is empty; run the archiver first")
    return max(sizes, key=sizes.get)


def measure(store: LakeStore, hour: datetime) -> dict:
    table = store.read_hour(hour)
    stored = store.file_size(hour)
    raw_json = json_lines(table)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    json_path, parquet_path = WORK_DIR / "hour.jsonl", WORK_DIR / "hour.parquet"
    json_path.write_bytes(raw_json)
    pq.write_table(table, parquet_path, compression="zstd")
    connection = duckdb.connect()
    json_sql = QUERY.format(source=f"read_json_auto('{json_path.as_posix()}')")
    parquet_sql = QUERY.format(source=f"read_parquet('{parquet_path.as_posix()}')")
    if connection.sql(json_sql).fetchall() != connection.sql(parquet_sql).fetchall():
        logger.warning("The JSON and Parquet queries disagree")
    return {
        "hour": hour.isoformat(),
        "rows": table.num_rows,
        "bytes": {
            "json": len(raw_json),
            "json_gzip": len(gzip.compress(raw_json)),
            "parquet_zstd_stored": stored,
            "parquet_snappy": parquet_bytes(table, "snappy"),
            "parquet_uncompressed": parquet_bytes(table, None),
        },
        "query_seconds": {
            "json": median_seconds(connection, json_sql),
            "parquet": median_seconds(connection, parquet_sql),
        },
    }


def markdown(result: dict) -> str:
    sizes, base = result["bytes"], result["bytes"]["json"]
    lines = [
        f"One hour: {result['hour']} ({result['rows']} edits)",
        "",
        "| Format | Size | vs. JSON |",
        "|---|---:|---:|",
    ]
    for name, size in sizes.items():
        lines.append(f"| {name} | {size / 1024:,.0f} KiB | {size / base:.1%} |")
    times = result["query_seconds"]
    lines += [
        "",
        f"DuckDB top-10 titles query (median of {RUNS}): JSON {times['json'] * 1000:.0f} ms, "
        f"Parquet {times['parquet'] * 1000:.0f} ms",
    ]
    return "\n".join(lines)


def write_lf(path: Path, text: str) -> None:
    """Write text with LF line endings on every platform."""
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prefix", help="key prefix in the bucket (default: LAKE_PREFIX)")
    parser.add_argument("--hour", help="the hour as YYYY-MM-DDTHH (default: the largest)")
    args = parser.parse_args()
    setup_logging()
    sys.stdout.reconfigure(encoding="utf-8")
    store = LakeStore.from_settings(get_settings(), args.prefix)
    result = measure(store, pick_hour(store, args.hour))
    result["lake"] = store.root
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = RESULTS_DIR / f"{date.today().isoformat()}_lake-size"  # the date only names the file
    write_lf(stem.with_suffix(".json"), json.dumps(result, indent=2))
    write_lf(stem.with_suffix(".md"), markdown(result))
    print(markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
