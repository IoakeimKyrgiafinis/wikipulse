"""Database migrations: `uv run python -m wikipulse.db.migrate`.

A migration is a plain SQL file in `wikipulse/db/migrations/`, named
`<number>_<description>.sql` (e.g. `0001_create_spikes.sql`). Files are applied in
name order. Each one runs in its own transaction together with a row in
`schema_migrations`, so a migration is either fully applied and recorded, or not at all.
Running the command again applies only the files that are new.

A Postgres advisory lock makes two migrators started at the same time take turns.

`--host` connects to another host than POSTGRES_DSN names, with the same user, password
and database. The Helm chart's migration hook needs it: it runs before the release's
Service `postgres` (a DNS alias) exists, so it connects to the database host directly.
"""

import argparse
import logging
import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from wikipulse.config import get_settings
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
LOCK_ID = 7_202_602  # any constant; identifies "WikiPulse migrations" to pg_advisory_lock

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text        PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    """All migration files, in the order they must be applied."""
    return sorted(directory.glob("*.sql"), key=lambda path: path.name)


def applied_versions(conn: psycopg.Connection) -> set[str]:
    """The versions of the migrations already applied."""
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every migration not applied yet. Returns the versions applied now.

    `conn` must be in autocommit mode; each migration gets its own transaction.
    """
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
    try:
        conn.execute(CREATE_TABLE)
        done = applied_versions(conn)
        applied: list[str] = []
        for path in migration_files(directory):
            version = path.stem
            if version in done:
                continue
            logger.info("Applying migration %s", path.name)
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
        return applied
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


def with_host(dsn: str, host: str | None) -> str:
    """The connection string with its host replaced (unchanged when `host` is empty)."""
    return make_conninfo(dsn, host=host) if host else dsn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", help="connect to this host instead of POSTGRES_DSN's")
    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging()
    dsn = with_host(settings.postgres_dsn, args.host)
    with psycopg.connect(dsn, autocommit=True) as conn:
        applied = migrate(conn)
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Database is up to date; nothing to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
