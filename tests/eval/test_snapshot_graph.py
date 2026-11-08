"""scripts/snapshot_graph.py: the commands it runs and how it compares graphs (no Docker)."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "snapshot_graph.py"
spec = importlib.util.spec_from_file_location("snapshot_graph", SCRIPT)
assert spec is not None and spec.loader is not None
snapshot_graph = importlib.util.module_from_spec(spec)
sys.modules["snapshot_graph"] = snapshot_graph
spec.loader.exec_module(snapshot_graph)


def test_dump_runs_neo4j_admin_in_a_one_off_container(tmp_path: Path) -> None:
    command = snapshot_graph.admin_command("neo4j", tmp_path, *snapshot_graph.dump_args())
    assert command[:4] == ["docker", "compose", "--profile", "eval"]
    assert command[4:7] == ["run", "--rm", "--no-deps"]
    assert command[7:9] == ["-v", f"{tmp_path.resolve()}:/snapshots"]
    assert command[9:] == [
        "neo4j",
        "neo4j-admin",
        "database",
        "dump",
        "neo4j",
        "--to-path=/snapshots",
    ]


def test_restore_loads_into_the_eval_service_only(tmp_path: Path) -> None:
    command = snapshot_graph.admin_command("neo4j-eval", tmp_path, *snapshot_graph.load_args())
    assert "neo4j-eval" in command and "neo4j" not in command[: command.index("neo4j-eval")]
    assert command[-2:] == ["--from-path=/snapshots", "--overwrite-destination=true"]


def test_default_name_is_the_newest_events_day() -> None:
    description = {"events": {"last_start": "2026-11-05T22:54:26.288000000+00:00"}}
    assert snapshot_graph.default_name(description) == "2026-11-05"
    with pytest.raises(SystemExit):
        snapshot_graph.default_name({"events": {"last_start": None}})


def test_same_content_lists_differences_but_not_index_states() -> None:
    expected = {
        "nodes": {"Event": 2},
        "relationships": {},
        "events": {},
        "embedded_nodes": 2,
        "indexes": {"a": "RANGE"},
        "index_states": ["ONLINE"],
    }
    actual = {**expected, "index_states": ["POPULATING"]}
    assert snapshot_graph.same_content(expected, actual) == []
    changed = {**expected, "nodes": {"Event": 3}}
    assert snapshot_graph.same_content(expected, changed) == [
        "nodes: expected {'Event': 2}, found {'Event': 3}"
    ]


def test_newest_snapshot_and_sha(tmp_path: Path) -> None:
    for name in ("2026-11-04", "2026-11-05"):
        folder = tmp_path / "snapshots" / name
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
    assert snapshot_graph.newest_snapshot(tmp_path) == "2026-11-05"
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"abc")
    assert snapshot_graph.sha256(dump).startswith("ba7816bf")
