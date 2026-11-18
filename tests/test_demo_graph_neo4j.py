"""Integration: the browser's database gives the same answers as Neo4j (spec 13a).

Run with `uv run pytest -m integration`; needs the frozen snapshot in `neo4j-eval`
(`docker compose --profile eval up -d`, `scripts/snapshot_graph.py restore`) and is
skipped when that server isn't running (as in CI, which has no snapshot).

The committed `site/data/graph.json` is loaded into Relate (`relate-db`, the same
version as the WebAssembly build the page runs) exactly as the page loads it. Then every
shipped query, with many parameter sets, and every prepared question's Cypher run on
both engines. Results must match, row order ignored where the query leaves it open
(`export_graph.compare_rows`). Rows that differ only in which tied rows a LIMIT kept
count as a "tie" only when it is proved: the query is run again on both engines,
returning its ORDER BY keys, with its own LIMIT and without one (`prove_tie`,
`export_graph.tie_at_limit`). What may still differ is listed in `KNOWN_DIFFERENCES`
with the reason, and the test checks that it still differs exactly that way.
"""

import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable

from wikipulse.config import get_settings
from wikipulse.qa import graph as qa_graph

relate = pytest.importorskip("relate")

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
GRAPH_FILE = ROOT / "site" / "data" / "graph.json"
spec = importlib.util.spec_from_file_location("export_graph", ROOT / "scripts" / "export_graph.py")
assert spec is not None and spec.loader is not None
export_graph = importlib.util.module_from_spec(spec)
sys.modules["export_graph"] = export_graph
spec.loader.exec_module(export_graph)

BIG_LIMIT = 10_000  # "no limit", to compare the full results behind a LIMIT
TIME_NAMES = ("start", "end", "since")  # datetime parameters of EVENTS_BETWEEN, TRENDING
# (query name, the full-text query) -> why the engines differ there.
KNOWN_DIFFERENCES = {
    ("SEARCH_ENTITIES", "california OR governor"): (
        "Lucene's standard tokenizer keeps \"California's\" as one word, Relate splits it "
        'at the apostrophe, so only Relate finds "California\'s death row" for "california".'
    ),
}


@pytest.fixture(scope="module")
def data() -> dict:
    return json.loads(GRAPH_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def neo4j() -> Iterator[Any]:
    settings = get_settings()
    auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
    driver = GraphDatabase.driver(settings.eval_qa_neo4j_uri, auth=auth)
    try:
        driver.verify_connectivity()
    except (ServiceUnavailable, AuthError, OSError):
        driver.close()
        pytest.skip("the snapshot server (neo4j-eval) is not running")
    yield driver
    driver.close()


@pytest.fixture(scope="module")
def db(data: dict) -> Iterator[Any]:
    """The graph in Relate, loaded with the file's own statements, as the page does."""
    conn = relate.connect(":memory:")
    for step in data["load"]:
        if step["rows"] is None:
            conn.execute(step["cypher"])
            continue
        for i in range(0, len(step["rows"]), data["batch"]):
            conn.execute(step["cypher"], {"rows": step["rows"][i : i + data["batch"]]})
    for statement in data["cleanup"]:
        conn.execute(statement)
    yield conn
    conn.close()


def run_relate(db: Any, query: str, params: dict) -> list[list]:
    cursor = db.execute(query, params)
    records = cursor.fetchall()
    columns = list(records[0].keys()) if records else []
    return [[export_graph.canonical(r[c]) for c in columns] for r in records]


def run_neo4j(driver: Any, query: str, params: dict) -> list[list]:
    return export_graph.run_neo4j(driver, query, params).rows


def neo4j_params(params: dict) -> dict:
    """Neo4j gets datetimes as datetimes; Relate gets their ISO text (as JSON would)."""
    return {
        k: datetime.fromisoformat(v.replace("Z", "+00:00")) if k in TIME_NAMES else v
        for k, v in params.items()
    }


def original(name: str, data: dict) -> str:
    text = getattr(qa_graph, name)
    return text % data["max_hops"] if name == "PATHS" else text


def prove_tie(
    relate_query: str,
    neo4j_query: str,
    params: tuple[dict, dict],
    widen: Any,
    rows: tuple[list[list], list[list]],
    data_db_neo4j: tuple[Any, Any],
) -> bool:
    """True when Relate's and Neo4j's rows (`rows`) differ only among rows tied at the
    LIMIT: both queries are rewritten to return their ORDER BY keys, then run on both
    engines with their own LIMIT and with `widen` (the query and parameters with a much
    larger LIMIT); the full results must agree and `tie_at_limit` must hold."""
    keyed_relate, keyed_neo4j = (
        export_graph.order_keys(relate_query),
        export_graph.order_keys(neo4j_query),
    )
    if keyed_relate is None or keyed_neo4j is None:
        return False
    db, neo4j = data_db_neo4j
    relate_params, neo4j_params_ = params
    keys = len(keyed_neo4j.order_by)
    live = run_relate(db, keyed_relate.cypher, relate_params)
    stored = run_neo4j(neo4j, keyed_neo4j.cypher, neo4j_params_)
    full_relate, full_relate_params = widen(keyed_relate.cypher, relate_params)
    full_neo4j, full_neo4j_params = widen(keyed_neo4j.cypher, neo4j_params_)
    full_live = run_relate(db, full_relate, full_relate_params)
    full_stored = run_neo4j(neo4j, full_neo4j, full_neo4j_params)
    # The keyed queries must give the rows the comparison was about ...
    plain_live, plain_stored = rows
    if not (
        export_graph.same_multiset(export_graph.drop_keys(live, keys), plain_live)
        and export_graph.same_multiset(export_graph.drop_keys(stored, keys), plain_stored)
    ):
        return False
    # ... the full results must agree, row for row ...
    if export_graph.compare_rows(full_live, full_stored) == "different":
        return False
    # ... and only rows tied with the last one at the cut-off may differ.
    full_keys = [row[len(row) - keys :] for row in full_stored]
    return export_graph.tie_at_limit(live, stored, full_live, full_keys, keys)


def compare_query(name: str, params: dict, data: dict, db: Any, neo4j: Any) -> str:
    """The comparison of one shipped query, "tie" when it is proved (`prove_tie`)."""
    relate_query = export_graph.bind_times(data["queries"][name], TIME_NAMES)
    neo4j_query = original(name, data)
    live = run_relate(db, relate_query, params)
    stored = run_neo4j(neo4j, neo4j_query, neo4j_params(params))
    verdict = export_graph.compare_rows(live, stored)
    if verdict != "different" or "limit" not in params:
        return verdict

    def widen(query: str, query_params: dict) -> tuple[str, dict]:
        return query, {**query_params, "limit": BIG_LIMIT}

    both = (params, neo4j_params(params))
    proved = prove_tie(relate_query, neo4j_query, both, widen, (live, stored), (db, neo4j))
    return "tie" if proved else "different"


def parameter_sets(neo4j: Any) -> list[tuple[str, dict]]:
    """Every shipped query with realistic parameters from the snapshot itself."""
    one = lambda q: [r[0] for r in run_neo4j(neo4j, q, {})]  # noqa: E731
    entities = one(
        "MATCH (n:Entity)-[r]-() RETURN n.key AS k, count(r) AS c ORDER BY c DESC, k LIMIT 12"
    )
    events = one("MATCH (e:Event) RETURN e.key AS k ORDER BY e.page_count DESC, e.key LIMIT 12")
    titles = one("MATCH (p:Page) RETURN p.title AS t ORDER BY t LIMIT 40")
    pena, ueno = one(
        "MATCH (n:Entity) WHERE n.name IN ['Santiago Peña', 'Ueno Bank'] "
        "RETURN n.key AS k ORDER BY n.name"
    )
    sets: list[tuple[str, dict]] = [("CLOCK", {})]
    for words in ("pena", "ueno OR bank", "trump", "california OR governor", "election"):
        sets += [
            ("SEARCH_ENTITIES", {"query": words, "limit": 100}),
            ("SEARCH_PAGES", {"query": words, "limit": 50}),
        ]
    sets += [
        ("EVENTS", {"keys": events}),
        ("EVENT_OVERVIEWS", {"keys": events}),
        ("ENTITIES_BY_KEY", {"keys": entities}),
        ("ENTITY_EVENT_COUNTS", {"keys": entities}),
        ("EVENTS_BY_NUMBER", {"numbers": [int(k.rsplit(":", 1)[1]) for k in events]}),
        ("PAGE_EVENTS", {"titles": titles}),
        ("EVENTS_BETWEEN", {"start": "2026-11-05T18:00:00Z", "end": "2026-11-05T20:00:00Z",
                            "limit": 15}),
        ("TRENDING", {"since": "2026-11-05T22:00:00Z", "limit": 10}),
    ]  # fmt: skip
    for key in events[:8]:
        sets += [
            ("EVENT_BRIEFING", {"key": key}),
            ("EVENT_ENTITIES", {"key": key, "limit": 25}),
            ("EVENT_RELATIONS", {"key": key, "limit": 20}),
        ]
    for key in [*entities[:10], pena, ueno]:
        sets += [
            ("ENTITY_EVENTS", {"key": key, "limit": 10}),
            ("ENTITY_RELATIONS", {"key": key, "limit": 15}),
        ]
    for title in ("Ueno Bank", "ueno bank", "Santiago Peña", "No such page"):
        sets += [
            ("PAGE_TEXT", {"title": title, "event": None}),
            ("PAGE_TEXT", {"title": title, "event": events[1]}),
        ]
    for a, b in [(pena, ueno), (entities[0], entities[3]), (entities[1], entities[2])]:
        sets.append(("PATHS", {"a": a, "b": b, "limit": 3}))
    return sets


def test_the_file_holds_the_snapshot(data: dict, neo4j: Any, db: Any) -> None:
    nodes, rels, now = export_graph.read_graph(neo4j)
    steps, cleanup, datetimes, counts = export_graph.build_load(nodes, rels)
    assert [s.model_dump() for s in steps] == data["load"]  # re-exporting changes nothing
    assert counts == data["counts"] and datetimes == data["datetimes"]
    assert export_graph.iso_text(now) == data["clock"]["now"]
    in_relate = run_relate(db, "MATCH (n) RETURN count(n) AS n", {})[0][0]
    rels_in_relate = run_relate(db, "MATCH ()-[r]->() RETURN count(r) AS n", {})[0][0]
    assert (in_relate, rels_in_relate) == (counts["nodes"], counts["relationships"])
    assert run_relate(db, "MATCH (n) WHERE n._id IS NOT NULL RETURN count(n) AS n", {}) == [[0]]


def test_every_shipped_query_gives_neo4js_answer(data: dict, neo4j: Any, db: Any) -> None:
    sets = parameter_sets(neo4j)
    assert {name for name, _ in sets} == set(data["queries"])  # every shipped query runs
    verdicts: dict[str, int] = {}
    unexpected = []
    for name, params in sets:
        verdict = compare_query(name, params, data, db, neo4j)
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        known = (name, params.get("query")) in KNOWN_DIFFERENCES
        if (verdict == "different") != known:
            unexpected.append((name, params, verdict))
    print(f"\n{len(sets)} shipped query runs: {verdicts}")
    assert not unexpected, unexpected


def test_the_known_difference_is_only_the_tokenizer(data: dict, neo4j: Any, db: Any) -> None:
    params = {"query": "california OR governor", "limit": 100}
    live = run_relate(db, data["queries"]["SEARCH_ENTITIES"], params)
    stored = run_neo4j(neo4j, qa_graph.SEARCH_ENTITIES, params)
    extra = [r for r in live if r not in stored]
    assert [r[1] for r in extra] == ["California's death row"]
    assert all(r in live for r in stored)


def test_every_prepared_question_gives_neo4js_rows(data: dict, neo4j: Any, db: Any) -> None:
    clock = data["clock"]
    neo4j_clock = {k: datetime.fromisoformat(v.replace("Z", "+00:00")) for k, v in clock.items()}
    verdicts = {}

    def widen(query: str, params: dict) -> tuple[str, dict]:
        return export_graph.with_final_limit(query, BIG_LIMIT), params

    for question in data["prepared"]:
        qid, cypher = question["id"], question["cypher"]
        fresh = run_neo4j(neo4j, cypher, neo4j_clock)
        assert fresh == question["neo4j"]["rows"], f"{qid}: graph.json is stale"
        tie = question["tie"]
        if tie is not None:  # the browser's evidence is Neo4j's current answer too
            assert run_neo4j(neo4j, tie["cypher"], neo4j_clock) == tie["neo4j"]["rows"], qid
            full = run_neo4j(neo4j, tie["full_cypher"], neo4j_clock)
            keys = len(tie["order_by"])
            assert [row[len(row) - keys :] for row in full] == tie["neo4j_full_keys"], qid
        relate_query = export_graph.bind_times(cypher)
        live = run_relate(db, relate_query, clock)
        verdict = export_graph.compare_rows(live, fresh)
        if verdict == "different":
            both = (clock, neo4j_clock)
            proved = prove_tie(relate_query, cypher, both, widen, (live, fresh), (db, neo4j))
            verdict = "tie" if proved else "different"
            assert tie is not None or not proved, f"{qid}: a tie the browser can't prove"
        verdicts[qid] = verdict
    print(f"\nPrepared questions: {verdicts}")
    assert "different" not in verdicts.values(), verdicts
