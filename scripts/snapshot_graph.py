"""Freeze the knowledge graph for evaluations: dump it, and restore it into a separate
Neo4j server (spec 05b).

    uv run python scripts/snapshot_graph.py dump [--name 2026-11-05]
    uv run python scripts/snapshot_graph.py restore [--name 2026-11-05]
    uv run python scripts/snapshot_graph.py info [--name 2026-11-05]

`dump` writes `data/snapshots/<name>/neo4j.dump` with Neo4j's own tool
(`neo4j-admin database dump`) and a `manifest.json` with what the graph held: nodes and
relationships by type, the events' time range, the indexes, the dump's SHA-256. The name
defaults to the UTC day of the newest event (the "day" the snapshot freezes). Neo4j
Community can only dump a stopped database, so the `neo4j` container is stopped for the
dump (about a minute) and started again afterwards, also when the dump fails.

`restore` loads a dump into the `neo4j-eval` service of docker-compose.yml (profile
"eval", bolt port 7688, its own volume), starts it, waits for its indexes and checks
that it holds exactly what the manifest says. The live graph is never written:
evaluations ask the copy, so new events can't change the answers to old questions.

Docker Compose is called through `subprocess` (no shell), so this runs the same on
Windows and Linux.
"""

import argparse
import hashlib
import json
import logging
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from wikipulse.config import Settings, get_settings
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger("snapshot_graph")

LIVE_SERVICE = "neo4j"
EVAL_SERVICE = "neo4j-eval"
DATABASE = "neo4j"  # Community's one user database
DUMP_FILE = f"{DATABASE}.dump"
MANIFEST = "manifest.json"
CONTAINER_DIR = "/snapshots"
COMPOSE_TIMEOUT_S = 600

COUNT_QUERIES = {
    "nodes": "MATCH (n) UNWIND labels(n) AS label RETURN label AS key, count(*) AS count",
    "relationships": "MATCH ()-[r]->() RETURN type(r) AS key, count(*) AS count",
}
EVENT_RANGE = """
MATCH (ev:Event)
RETURN min(ev.started_at) AS first_start, max(ev.started_at) AS last_start,
       max(coalesce(ev.ended_at, ev.started_at)) AS last_end, count(ev) AS events
"""
INDEXES = "SHOW INDEXES YIELD name, type, state RETURN name, type, state ORDER BY name"
EMBEDDED = "MATCH (n) WHERE n.embedding IS NOT NULL RETURN count(n) AS count"


def snapshot_dir(data_dir: Path, name: str) -> Path:
    return data_dir / "snapshots" / name


def compose_command(*args: str) -> list[str]:
    """`docker compose` with the eval profile, so the neo4j-eval service is known."""
    return ["docker", "compose", "--profile", "eval", *args]


def admin_command(service: str, host_dir: Path, *admin_args: str) -> list[str]:
    """A one-off container of `service` (its volumes, no ports, no dependencies) running
    `neo4j-admin`, with `host_dir` mounted at /snapshots."""
    mount = f"{host_dir.resolve()}:{CONTAINER_DIR}"
    return compose_command(
        "run", "--rm", "--no-deps", "-v", mount, service, "neo4j-admin", *admin_args
    )


def dump_args() -> list[str]:
    return ["database", "dump", DATABASE, f"--to-path={CONTAINER_DIR}"]


def load_args() -> list[str]:
    return [
        "database",
        "load",
        DATABASE,
        f"--from-path={CONTAINER_DIR}",
        "--overwrite-destination=true",
    ]


def run(command: Sequence[str]) -> None:
    logger.info("$ %s", " ".join(command))
    subprocess.run(list(command), check=True, timeout=COMPOSE_TIMEOUT_S)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _plain(value: Any) -> Any:
    return value.iso_format() if hasattr(value, "iso_format") else value


def describe_graph(uri: str, settings: Settings) -> dict[str, Any]:
    """What a graph holds: counts by label and type, the events' time range, indexes."""
    auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
    with GraphDatabase.driver(uri, auth=auth) as driver:
        result: dict[str, Any] = {}
        for name, query in COUNT_QUERIES.items():
            records = driver.execute_query(query, database_=DATABASE).records
            result[name] = {r["key"]: r["count"] for r in sorted(records, key=lambda r: r["key"])}
        row = driver.execute_query(EVENT_RANGE, database_=DATABASE).records[0]
        result["events"] = {key: _plain(row[key]) for key in row.keys()}
        result["embedded_nodes"] = driver.execute_query(EMBEDDED, database_=DATABASE).records[0][
            "count"
        ]
        indexes = driver.execute_query(INDEXES, database_=DATABASE).records
        result["indexes"] = {r["name"]: r["type"] for r in indexes}
        result["index_states"] = sorted({r["state"] for r in indexes})
    return result


def default_name(description: dict[str, Any]) -> str:
    """The UTC day of the newest event: "2026-11-05"."""
    last = description["events"].get("last_start")
    if not last:
        raise SystemExit("The graph has no events; nothing to snapshot.")
    return str(last)[:10]


def same_content(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Differences between two descriptions (index states are not compared)."""
    keys = ["nodes", "relationships", "events", "embedded_nodes", "indexes"]
    return [
        f"{key}: expected {expected.get(key)!r}, found {actual.get(key)!r}"
        for key in keys
        if expected.get(key) != actual.get(key)
    ]


def wait_until_ready(uri: str, settings: Settings, timeout_s: float = 180) -> None:
    """Wait until the server answers and every index is online."""
    auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with GraphDatabase.driver(uri, auth=auth) as driver:
                driver.verify_connectivity()
                with driver.session(database=DATABASE) as session:
                    session.run("CALL db.awaitIndexes($seconds)", seconds=int(timeout_s)).consume()
            return
        except (ServiceUnavailable, Neo4jError, OSError) as error:
            if time.monotonic() > deadline:
                raise TimeoutError(f"{uri} is not ready after {timeout_s:.0f} s") from error
            logger.info("Waiting for %s (%s)", uri, type(error).__name__)
            time.sleep(5)


def dump(name: str | None, settings: Settings) -> Path:
    description = describe_graph(settings.neo4j_uri, settings)
    name = name or default_name(description)
    folder = snapshot_dir(settings.data_dir, name)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / DUMP_FILE).unlink(missing_ok=True)
    logger.info("Stopping %s for the dump (Community dumps only stopped databases)", LIVE_SERVICE)
    run(compose_command("stop", LIVE_SERVICE))
    try:
        run(admin_command(LIVE_SERVICE, folder, *dump_args()))
    finally:
        run(compose_command("start", LIVE_SERVICE))
        wait_until_ready(settings.neo4j_uri, settings)
    path = folder / DUMP_FILE
    manifest = {
        "name": name,
        "dumped_at": datetime.now(UTC).isoformat(),  # wall clock, metadata only
        "source": settings.neo4j_uri,
        "dump": DUMP_FILE,
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "graph": description,
    }
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def read_manifest(folder: Path) -> dict[str, Any]:
    path = folder / MANIFEST
    if not path.exists():
        raise SystemExit(f"No snapshot in {folder} (run `dump` first).")
    return json.loads(path.read_text(encoding="utf-8"))


def newest_snapshot(data_dir: Path) -> str:
    folders = sorted(p.parent.name for p in (data_dir / "snapshots").glob(f"*/{MANIFEST}"))
    if not folders:
        raise SystemExit("No snapshots yet (run `dump` first).")
    return folders[-1]


def restore(name: str | None, settings: Settings) -> list[str]:
    name = name or newest_snapshot(settings.data_dir)
    folder = snapshot_dir(settings.data_dir, name)
    manifest = read_manifest(folder)
    if sha256(folder / DUMP_FILE) != manifest["sha256"]:
        raise SystemExit(f"{folder / DUMP_FILE} does not match its manifest (changed?).")
    run(compose_command("stop", EVAL_SERVICE))
    run(admin_command(EVAL_SERVICE, folder, *load_args()))
    run(compose_command("up", "-d", EVAL_SERVICE))
    wait_until_ready(settings.eval_qa_neo4j_uri, settings)
    return same_content(manifest["graph"], describe_graph(settings.eval_qa_neo4j_uri, settings))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["dump", "restore", "info"])
    parser.add_argument("--name", help="snapshot name (default: newest event's day / newest)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging()
    logging.getLogger("neo4j").setLevel(logging.WARNING)
    if args.command == "dump":
        path = dump(args.name, settings)
        print(f"Dumped the graph to {path} ({path.stat().st_size / 1e6:.1f} MB).")
        return 0
    if args.command == "restore":
        differences = restore(args.name, settings)
        if differences:
            print("The restored graph differs from the manifest:\n" + "\n".join(differences))
            return 1
        print(f"Restored into {settings.eval_qa_neo4j_uri}; it matches the manifest.")
        return 0
    name = args.name or newest_snapshot(settings.data_dir)
    print(json.dumps(read_manifest(snapshot_dir(settings.data_dir, name)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
