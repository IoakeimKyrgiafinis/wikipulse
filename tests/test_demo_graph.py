"""The demo's graph export (scripts/export_graph.py, site/data/graph.json; spec 13a).

Everything here reads committed files; no Neo4j, no Relate, no network. The comparison
with Neo4j is tests/test_demo_graph_neo4j.py (integration); the browser's code runs in
tests/test_demo_graph_js.py.
"""

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wikipulse.qa import graph as qa_graph
from wikipulse.qa.cypher import validate_cypher

ROOT = Path(__file__).resolve().parents[1]
GRAPH_FILE = ROOT / "site" / "data" / "graph.json"
CASES_FILE = ROOT / "tests" / "fixtures" / "cypher_cases.json"
spec = importlib.util.spec_from_file_location("export_graph", ROOT / "scripts" / "export_graph.py")
assert spec is not None and spec.loader is not None
export_graph = importlib.util.module_from_spec(spec)
sys.modules["export_graph"] = export_graph
spec.loader.exec_module(export_graph)

# As in tests/test_demo_site.py, without "summary": here it is a Page's article lead
# (enrichment, spec 03a), never an edit summary; it may appear only on Page nodes.
PERSONAL_KEYS = {
    "user", "username", "user_id", "user_text", "editor", "editors", "ip", "ip_address",
    "comment", "parsedcomment", "edit_summary", "correlation_id", "event_id", "meta",
}  # fmt: skip
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6 = re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b")


@pytest.fixture(scope="module")
def graph() -> dict:
    return json.loads(GRAPH_FILE.read_text(encoding="utf-8"))


def node_steps(graph: dict) -> dict[str, list[dict]]:
    """Label -> node rows of the load steps."""
    out = {}
    for step in graph["load"]:
        found = re.match(r"UNWIND \$rows AS row CREATE \(n:(\w+)\)", step["cypher"])
        if found:
            out[found[1]] = step["rows"]
    return out


def rel_steps(graph: dict) -> dict[str, list[dict]]:
    """Relationship type -> rows of the load steps."""
    out: dict[str, list[dict]] = {}
    for step in graph["load"]:
        found = re.search(r"CREATE \(a\)-\[r:(\w+)\]->\(b\)", step["cypher"])
        if found:
            out.setdefault(found[1], []).extend(step["rows"])
    return out


# --- Schema and counts ----------------------------------------------------------------------
def test_the_export_matches_its_schema(graph) -> None:
    export = export_graph.GraphExport.model_validate(graph)  # extra="forbid" everywhere
    assert export.schema_version == export_graph.SCHEMA_VERSION
    assert export.snapshot == "2026-11-05" and export.prepared


def test_the_counts_agree_with_the_rows(graph) -> None:
    nodes, rels = node_steps(graph), rel_steps(graph)
    counts = graph["counts"]
    assert counts["nodes"] == sum(len(r) for r in nodes.values()) == 1761
    assert counts["relationships"] == sum(len(r) for r in rels.values()) == 2534
    for kind, rows in {**nodes, **rels}.items():
        assert counts[kind] == len(rows), kind
    ids = [row["_id"] for rows in nodes.values() for row in rows]
    assert sorted(ids) == list(range(len(ids)))  # compact and unique


def test_every_property_is_named_and_no_embedding_is_exported(graph) -> None:
    for label, rows in node_steps(graph).items():
        allowed = set(export_graph.NODE_PROPERTIES[label]) | {"_id"}
        for row in rows:
            assert set(row) <= allowed, set(row) - allowed
    for rtype, rows in rel_steps(graph).items():
        for row in rows:
            assert set(row) == {"s", "e", "p"}
            assert set(row["p"]) <= set(export_graph.REL_PROPERTIES[rtype])
    assert "embedding" not in json.dumps(graph["load"])


def test_datetimes_travel_as_iso_text_and_are_converted_on_load(graph) -> None:
    nodes, rels = node_steps(graph), rel_steps(graph)
    for kind, names in graph["datetimes"].items():
        rows = nodes.get(kind) or [r["p"] for r in rels[kind]]
        creates = (f"CREATE (n:{kind})", f"[r:{kind}]")
        steps = [s for s in graph["load"] if any(c in s["cypher"] for c in creates)]
        assert steps, kind
        for name, step in ((n, s) for n in names for s in steps):
            assert f".{name} = datetime(" in step["cypher"], (kind, name)
            for row in rows:
                if name in row:
                    datetime.fromisoformat(row[name])  # parses
                    assert row[name].endswith("Z")
    for value in graph["clock"].values():
        assert datetime.fromisoformat(value).tzinfo is not None


def test_the_temporary_ids_are_removed_after_the_load(graph) -> None:
    assert "MATCH (n) REMOVE n._id" in graph["cleanup"]
    created = [s["cypher"] for s in graph["load"] if "load_" in s["cypher"]]
    for statement in created:
        name = re.search(r"INDEX (load_\w+)", statement)[1]
        assert f"DROP INDEX {name}" in graph["cleanup"]


# --- Privacy --------------------------------------------------------------------------------
def walk(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield path, key, item
            yield from walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from walk(item, f"{path}[{i}]")


def test_no_personal_fields_are_exported(graph) -> None:
    keys = {key for _, key, _ in walk(graph)}
    assert not keys & PERSONAL_KEYS, keys & PERSONAL_KEYS
    for label, rows in node_steps(graph).items():
        if label != "Page":
            assert all("summary" not in row for row in rows), label


def test_no_addresses_or_user_pages_in_the_text(graph) -> None:
    for _, _, value in walk(graph):
        if isinstance(value, str):
            assert not IPV4.search(value), value[:200]
            assert not IPV6.search(value), value[:200]
            assert not re.search(r"\bUser(?: talk)?:", value), value[:200]


# --- The queries ----------------------------------------------------------------------------
def test_the_fulltext_call_becomes_two_arguments_and_a_limit() -> None:
    rewritten = export_graph.relate_fulltext(qa_graph.SEARCH_ENTITIES)
    assert "db.index.fulltext.queryNodes('qa_entity_names', $query)" in rewritten
    assert "{limit" not in rewritten
    assert rewritten.rstrip().endswith("LIMIT $limit")
    assert rewritten.count("LIMIT") == 1
    pages = export_graph.relate_fulltext(qa_graph.SEARCH_PAGES)
    assert "queryNodes('qa_page_titles', $query)" in pages and pages.rstrip().endswith(
        "LIMIT $limit"
    )


def test_queries_without_a_fulltext_call_are_unchanged_and_odd_ones_refused() -> None:
    assert export_graph.relate_fulltext(qa_graph.EVENTS) == qa_graph.EVENTS
    with pytest.raises(ValueError):
        export_graph.relate_fulltext(
            "CALL db.index.fulltext.queryNodes('i', 'x') YIELD node RETURN node"
        )
    with pytest.raises(ValueError):
        export_graph.relate_fulltext(qa_graph.SEARCH_ENTITIES + " LIMIT 5")


def test_the_shipped_queries_are_exactly_the_python_constants_after_the_rewrite(graph) -> None:
    assert graph["queries"] == export_graph.shipped_queries()
    for name, text in graph["queries"].items():
        original = getattr(qa_graph, name)
        if name == "PATHS":
            original = original % graph["max_hops"]
        assert text == export_graph.relate_fulltext(original), name
    assert not set(graph["queries"]) & set(export_graph.VECTOR_QUERIES)


def test_the_bound_of_the_connection_finder_is_the_retrievals(graph) -> None:
    assert f"*..{graph['max_hops']}]" in graph["queries"]["PATHS"]
    assert graph["max_hops"] <= 6  # the validator's MAX_HOPS


def test_prepared_questions_are_validated_queries_with_neo4js_rows(graph) -> None:
    ids = [q["id"] for q in graph["prepared"]]
    assert len(ids) == len(set(ids)) == 25
    for question in graph["prepared"]:
        cypher = question["cypher"]
        assert validate_cypher(cypher, graph["max_rows"]) == cypher, question["id"]
        assert len(question["neo4j"]["rows"]) == question["stored_row_count"], question["id"]
        assert question["correctness"], question["id"]
        for source in question["sources"]:
            if source["url"]:
                assert source["url"].endswith(f"oldid={source['revision']}")


def test_the_time_parameters_are_wrapped_like_the_browser_does() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))["bind_times"]
    for case in cases:
        assert export_graph.bind_times(case["query"]) == case["expected"]


# --- Building the export (pure functions) ---------------------------------------------------
def test_build_load_names_ids_drops_unknown_properties_and_marks_datetimes() -> None:
    moment = datetime(2026, 11, 5, 16, 0, 36, 573000, tzinfo=UTC)
    event = {"key": "r:1", "started_at": moment, "embedding": [0.1]}
    nodes = [
        {"id": "4:x:2", "labels": ["Event"], "props": event},
        {"id": "4:x:1", "labels": ["Entity"], "props": {"key": "Q1", "name": "Peña", "secret": 1}},
    ]
    rels = [{"start": "4:x:1", "end": "4:x:2", "type": "INVOLVED_IN", "props": {"type": "person"}}]
    steps, cleanup, datetimes, counts = export_graph.build_load(nodes, rels)
    assert datetimes == {"Event": ["started_at"]}
    assert counts == {"nodes": 2, "relationships": 1, "Entity": 1, "Event": 1, "INVOLVED_IN": 1}
    event = next(s for s in steps if "CREATE (n:Event)" in s.cypher)
    assert event.rows == [{"_id": 1, "key": "r:1", "started_at": "2026-11-05T16:00:36.573Z"}]
    assert "n.started_at = datetime(row.started_at)" in event.cypher
    entity = next(s for s in steps if "CREATE (n:Entity)" in s.cypher)
    assert entity.rows == [{"_id": 0, "key": "Q1", "name": "Peña"}]  # "secret" left out
    rel = next(s for s in steps if "INVOLVED_IN" in s.cypher)
    assert "MATCH (a:Entity {_id: row.s}) MATCH (b:Event {_id: row.e})" in rel.cypher
    assert rel.rows == [{"s": 0, "e": 1, "p": {"type": "person"}}]
    assert cleanup[0] == "MATCH (n) REMOVE n._id"
    with pytest.raises(ValueError):
        export_graph.build_load([{"id": 1, "labels": ["Editor"], "props": {}}], [])


def test_iso_text_and_canonical_time() -> None:
    assert export_graph.iso_text(datetime(2026, 11, 5, 0, 0, tzinfo=UTC)) == "2026-11-05T00:00:00Z"
    neo4j = "2026-11-05T16:00:36.573000000+00:00"
    relate = "2026-11-05T16:00:36.573Z"
    assert export_graph.canonical_time(neo4j) == export_graph.canonical_time(relate)
    assert export_graph.canonical_time(relate) == "2026-11-05T16:00:36.573000Z"
    assert export_graph.canonical_time("2026-11-05T18:00:36+02:00") == "2026-11-05T16:00:36.000000Z"
    assert export_graph.canonical_time("not a time") == "not a time"


def test_canonical_values() -> None:
    assert export_graph.canonical(1.0) == 1 and export_graph.canonical(0.5) == 0.5
    assert export_graph.canonical({"b": [1.0], "a": None}) == {"a": None, "b": [1]}
    assert list(export_graph.canonical({"b": 1, "a": 2})) == ["a", "b"]


def test_compare_rows() -> None:
    compare = export_graph.compare_rows
    assert compare([[1], [2]], [[1], [2]]) == "identical"
    assert compare([[2], [1]], [[1], [2]]) == "other order"
    assert compare([["x", [1, 2]]], [["x", [2, 1]]]) == "list order"
    # ORDER BY pages DESC LIMIT 3: rows above the cut-off agree, the tied ones differ.
    live = [["a", 5], ["b", 1], ["c", 1]]
    stored = [["a", 5], ["d", 1], ["e", 1]]
    assert compare(live, stored) == "tie"
    assert compare([["a", 5], ["b", 1]], [["z", 5], ["b", 1]]) == "different"
    assert compare([["a"]], [["b"]]) == "different"
