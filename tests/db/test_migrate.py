"""Migration file ordering (no database needed)."""

from pathlib import Path

from wikipulse.db.migrate import MIGRATIONS_DIR, migration_files


def test_migrations_are_applied_in_name_order(tmp_path: Path) -> None:
    for name in ("0002_b.sql", "0010_c.sql", "0001_a.sql"):
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
    assert [p.name for p in migration_files(tmp_path)] == ["0001_a.sql", "0002_b.sql", "0010_c.sql"]


def test_the_spikes_migration_is_shipped() -> None:
    assert [p.name for p in migration_files(MIGRATIONS_DIR)][0] == "0001_create_spikes.sql"
