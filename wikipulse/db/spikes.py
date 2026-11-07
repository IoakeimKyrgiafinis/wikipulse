"""Insert, update and query spikes in Postgres (table `spikes`, see migrations/0001).

Writes are *upserts* keyed on (run_id, title, started_at): writing the same spike twice
leaves one row with the latest values. The detector may re-process stream entries after
a crash and write a spike again, and this makes that harmless (idempotent).
"""

from collections.abc import Iterable
from dataclasses import fields
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from wikipulse.detect.detector import SpikeRecord

RECORD_COLUMNS = [f.name for f in fields(SpikeRecord)]

UPSERT = f"""
INSERT INTO spikes (run_id, {", ".join(RECORD_COLUMNS)})
VALUES (%(run_id)s, {", ".join(f"%({name})s" for name in RECORD_COLUMNS)})
ON CONFLICT (run_id, title, started_at) DO UPDATE SET
    {", ".join(f"{name} = EXCLUDED.{name}" for name in RECORD_COLUMNS)},
    updated_at = now()
"""

SELECT = f"SELECT {', '.join(RECORD_COLUMNS)} FROM spikes"


def upsert_spikes(conn: psycopg.Connection, run_id: str, spikes: Iterable[SpikeRecord]) -> int:
    """Insert new spikes and update existing ones, in one transaction. Returns the count."""
    rows = [{"run_id": run_id, **spike.__dict__} for spike in spikes]
    if not rows:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
    return len(rows)


def _records(conn: psycopg.Connection, query: str, params: tuple) -> list[SpikeRecord]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return [SpikeRecord(**row) for row in cur.fetchall()]


def active_spikes(conn: psycopg.Connection, run_id: str, limit: int = 20) -> list[SpikeRecord]:
    """Active spikes of a run, highest score first."""
    query = f"{SELECT} WHERE run_id = %s AND status = 'active' ORDER BY score DESC, title LIMIT %s"
    return _records(conn, query, (run_id, limit))


def spikes_for_run(
    conn: psycopg.Connection,
    run_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[SpikeRecord]:
    """All spikes of a run (optionally started in [since, until)), oldest first."""
    query = f"{SELECT} WHERE run_id = %s"
    params: list = [run_id]
    if since is not None:
        query += " AND started_at >= %s"
        params.append(since)
    if until is not None:
        query += " AND started_at < %s"
        params.append(until)
    return _records(conn, query + " ORDER BY started_at, title", tuple(params))


def delete_run(conn: psycopg.Connection, run_id: str) -> int:
    """Delete every spike of a run (for a fresh replay). Returns the number deleted."""
    with conn.transaction():
        return conn.execute("DELETE FROM spikes WHERE run_id = %s", (run_id,)).rowcount


def latest_edit_time(conn: psycopg.Connection, run_id: str) -> datetime | None:
    """The newest `last_edit_at` of a run: roughly how far the detector has got."""
    row = conn.execute(
        "SELECT max(last_edit_at) FROM spikes WHERE run_id = %s", (run_id,)
    ).fetchone()
    return row[0] if row else None
