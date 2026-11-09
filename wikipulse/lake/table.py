"""Edits as Arrow tables: the archive's columns, and merging an hour without duplicates.

Arrow is an in-memory *columnar* format: a table is a set of columns, each one array of
values of one type. Parquet is its on-disk counterpart. Storing column by column puts
similar values next to each other (all the titles, all the booleans), which compresses
far better than JSON's row-by-row text, and lets a query read only the columns it uses.
"""

from collections.abc import Iterable

import pyarrow as pa

from wikipulse.stream.models import EditEvent

# The columns of the archive, in order. Partition columns (year, month, day, hour) are
# not stored in the files: they are encoded in the folder names.
EDIT_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("event_time", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("received_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("title", pa.string(), nullable=False),
        pa.field("type", pa.string(), nullable=False),
        pa.field("user", pa.string(), nullable=False),
        pa.field("is_bot", pa.bool_(), nullable=False),
        pa.field("is_minor", pa.bool_(), nullable=False),
        pa.field("rev_old", pa.int64()),
        pa.field("rev_new", pa.int64()),
        pa.field("len_old", pa.int64()),
        pa.field("len_new", pa.int64()),
        pa.field("comment", pa.string(), nullable=False),
    ]
)

_SORT_KEYS = [("event_time", "ascending"), ("event_id", "ascending")]


def events_to_table(events: Iterable[EditEvent]) -> pa.Table:
    """An Arrow table with one row per edit, in EDIT_SCHEMA."""
    events = list(events)
    columns = {
        field.name: [getattr(event, field.name) for event in events] for field in EDIT_SCHEMA
    }
    return pa.Table.from_pydict(columns, schema=EDIT_SCHEMA)


def conform(table: pa.Table) -> pa.Table:
    """`table` with exactly EDIT_SCHEMA's columns and types (e.g. a file read back)."""
    return table.select(EDIT_SCHEMA.names).cast(EDIT_SCHEMA)


def merge_edits(*tables: pa.Table) -> pa.Table:
    """Concatenate tables, keep the first row of every `event_id`, sort by event time.

    Earlier tables win, so rows already in the archive are kept as they are. The order
    (event_time, then event_id) depends only on the rows, not on how or when they
    arrived, so the same edits always produce the same file.
    """
    combined = pa.concat_tables([conform(table) for table in tables])
    if combined.num_rows == 0:
        return combined
    numbered = combined.append_column("_row", pa.array(range(combined.num_rows), pa.int64()))
    firsts = numbered.group_by("event_id", use_threads=False).aggregate([("_row", "min")])
    unique = combined.take(firsts["_row_min"])  # row numbers of the first copies
    return unique.sort_by(_SORT_KEYS)
