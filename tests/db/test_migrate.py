"""Migration file ordering (no database needed)."""

from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from wikipulse.db.migrate import MIGRATIONS_DIR, migration_files, with_host


def test_migrations_are_applied_in_name_order(tmp_path: Path) -> None:
    for name in ("0002_b.sql", "0010_c.sql", "0001_a.sql"):
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
    assert [p.name for p in migration_files(tmp_path)] == ["0001_a.sql", "0002_b.sql", "0010_c.sql"]


def test_the_spikes_migration_is_shipped() -> None:
    assert [p.name for p in migration_files(MIGRATIONS_DIR)][0] == "0001_create_spikes.sql"


def test_with_host_replaces_only_the_host() -> None:
    dsn = "postgresql://wikipulse:pw@postgres:5432/wikipulse?sslmode=disable"
    assert conninfo_to_dict(with_host(dsn, "host.docker.internal")) == {
        "user": "wikipulse",
        "password": "pw",
        "host": "host.docker.internal",
        "port": "5432",
        "dbname": "wikipulse",
        "sslmode": "disable",
    }
    assert with_host(dsn, None) == dsn
    assert with_host(dsn, "") == dsn
