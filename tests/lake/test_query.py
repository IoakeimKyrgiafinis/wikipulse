"""SQL over the archive with DuckDB, and the command-line arguments."""

from tests.lake.helpers import T0, edit, local_store
from wikipulse.lake import archive, query
from wikipulse.lake.table import events_to_table


def test_sql_sees_rows_and_partition_columns(tmp_path) -> None:
    store = local_store(tmp_path)
    store.write_hour(T0, events_to_table([edit("a", 1, "X"), edit("b", 2, "Y", is_bot=True)]))
    store.write_hour(T0.replace(hour=13), events_to_table([edit("c", 61, "X")]))
    connection = query.connect(store)
    per_hour = connection.sql(
        "SELECT hour, count(*), avg(is_bot::int) FROM edits GROUP BY hour ORDER BY hour"
    ).fetchall()
    assert per_hour == [(12, 2, 0.5), (13, 1, 0.0)]
    top = connection.sql("SELECT title, count(*) n FROM edits GROUP BY 1 ORDER BY n DESC")
    assert top.fetchall()[0] == ("X", 2)
    only_13 = connection.sql("SELECT event_id FROM edits WHERE hour = 13").fetchall()
    assert only_13 == [("c",)]


def test_sql_over_an_empty_lake_returns_no_rows(tmp_path) -> None:
    connection = query.connect(local_store(tmp_path / "empty"))
    assert connection.sql("SELECT count(*) FROM edits").fetchall() == [(0,)]


def test_archive_arguments() -> None:
    args = archive.parse_args(
        ["--stream-key", "s", "--prefix", "p", "--exit-when-idle", "5", "--reset"]
    )
    assert (args.stream_key, args.prefix, args.exit_when_idle, args.reset) == ("s", "p", 5, True)
    assert archive.parse_args([]).start_id == "0"


def test_query_arguments() -> None:
    args = query.parse_args(["SELECT 1", "--csv", "--max-rows", "5"])
    assert (args.sql, args.csv, args.max_rows) == ("SELECT 1", True, 5)
