"""Export the frozen knowledge graph for the demo's "Explore the graph" section (spec 13a).

    uv run python scripts/export_graph.py
    uv run python scripts/export_graph.py --uri bolt://localhost:7688 --out site/data/graph.json

Runs on the laptop with the `neo4j-eval` service up (the 2026-11-05 snapshot, restored
by `scripts/snapshot_graph.py restore`); what it writes, `site/data/graph.json`, is
committed and served by GitHub Pages. In the browser, `site/graph-worker.js` loads it
into Relate (an embedded graph database compiled to WebAssembly) and runs the queries.

What the file holds:

- **`load`**: the statements that rebuild the graph, run in order. First WikiPulse's own
  schema statements (`wikipulse.graph.store.SCHEMA`, `wikipulse.qa.index.FULLTEXT_INDEXES`,
  unchanged: Relate accepts them), then per label a temporary index on `_id`, the nodes
  (`UNWIND $rows ... CREATE`, sent in batches), the relationships (both ends matched by
  label and `_id`, which the index makes fast), and finally `_id` and its indexes go.
  Datetimes travel as ISO text (JSON has no datetime) and become datetimes again with
  `datetime()` as they are created; `datetimes` lists which properties they are.
- **`queries`**: the retrieval queries of `wikipulse/qa/graph.py`, taken from its
  constants (never copied by hand), with the one change Relate needs
  (`relate_fulltext()`). The vector searches are left out: no embeddings are exported.
- **`clock`**: the graph's "now" and the start of its day and week, as the evaluation
  used them for `$now`, `$today_start` and `$week_start`.
- **`prepared`**: the 25 questions of the 12a evaluation run with the Cypher the LLM
  wrote, the rows Neo4j returns for it (`canonical()` form), and the stored answer with
  its judges' scores.

Privacy: as for `day.json`, every exported property is named (`NODE_PROPERTIES`,
`REL_PROPERTIES`); anything else, the embeddings included, is left out. The graph holds
no editor, IP address or edit summary (`Page.summary` is the article's own lead text
from enrichment); `tests/test_demo_graph.py` checks the committed file.
"""

import argparse
import json
import logging
import math
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so `wikipulse` imports when run as a script

from wikipulse.config import get_settings  # noqa: E402
from wikipulse.graph.store import SCHEMA  # noqa: E402
from wikipulse.logging_setup import setup_logging  # noqa: E402
from wikipulse.qa import graph as qa_graph  # noqa: E402
from wikipulse.qa.cypher import mask  # noqa: E402
from wikipulse.qa.index import FULLTEXT_INDEXES  # noqa: E402

logger = logging.getLogger("export_graph")

OUT_FILE = ROOT / "site" / "data" / "graph.json"
SNAPSHOT = "2026-11-05"
RUN = "12a-after"
ANSWERS_FILE = ROOT / "data" / "qa_eval" / RUN / "answers.jsonl"
RESULTS_FILE = ROOT / "evals" / "results" / f"2026-11-16_{RUN}.json"
SCHEMA_VERSION = 1
BATCH = 500  # rows per load statement, as the browser sends them
MAX_HOPS = 4  # QAGraph.paths()' default: the connection finder's bound
MAX_ROWS = 25  # the evaluation's row limit for generated queries

# Every property the export writes, by node label and relationship type.
NODE_PROPERTIES: dict[str, tuple[str, ...]] = {
    "Entity": ("key", "name", "aliases", "type", "wikidata_id", "linked", "link_method"),
    "Event": (
        "key", "number", "run_id", "title", "description", "provisional_title", "status",
        "started_at", "ended_at", "extracted_at", "page_count", "model", "prompt_version",
    ),
    "Page": ("title", "description", "summary", "wikidata_id"),
}  # fmt: skip
REL_PROPERTIES: dict[str, tuple[str, ...]] = {
    "INVOLVED_IN": (
        "type", "confidence", "source_title", "source_revision", "extracted_at", "model",
        "prompt_version",
    ),
    "PART_OF": ("revision", "score", "first_edit_at", "last_edit_at"),
    "RELATED_TO": (
        "relation", "confidence", "event_key", "source_title", "source_revision",
        "extracted_at", "model", "prompt_version",
    ),
}  # fmt: skip
# The retrieval queries shipped to the page: every constant of wikipulse/qa/graph.py
# except the two vector searches (they need embeddings and a model to embed questions).
QUERY_NAMES = (
    "CLOCK", "SEARCH_ENTITIES", "SEARCH_PAGES", "EVENTS", "EVENTS_BETWEEN", "TRENDING",
    "EVENT_BRIEFING", "PAGE_TEXT", "EVENT_OVERVIEWS", "ENTITIES_BY_KEY", "ENTITY_EVENT_COUNTS",
    "EVENTS_BY_NUMBER", "PAGE_EVENTS", "ENTITY_EVENTS", "EVENT_ENTITIES", "ENTITY_RELATIONS",
    "EVENT_RELATIONS", "PATHS",
)  # fmt: skip
VECTOR_QUERIES = ("SIMILAR_PAGES", "SIMILAR_EVENTS")
TIME_PARAMETERS = ("now", "today_start", "week_start")


# --- The data file's schema -----------------------------------------------------------------
class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoadStep(Strict):
    cypher: str
    rows: list[dict[str, Any]] | None = Field(
        None, description="Sent as $rows, in batches of `batch`; None: run once."
    )


class Clock(Strict):
    now: str
    today_start: str
    week_start: str


class Rows(Strict):
    columns: list[str]
    rows: list[list[Any]] = Field(description="Values in `canonical()` form.")


class Source(Strict):
    title: str
    url: str | None = None
    revision: int | None = None


class PreparedQuestion(Strict):
    id: str
    question: str
    type: str
    cypher: str = Field(description="The query the LLM wrote, as validated and run in 12a.")
    stored_row_count: int = Field(description="Rows the 12a run got (the answers file).")
    neo4j: Rows = Field(description="What Neo4j returns for `cypher` on the snapshot.")
    answer: str
    sources: list[Source]
    model: str | None = None
    correctness: dict[str, float] = Field(description="Judge model -> score 0 to 2.")


class GraphExport(Strict):
    schema_version: int
    snapshot: str
    run: str = Field(description="The evaluation run the prepared questions come from.")
    counts: dict[str, int]
    batch: int
    datetimes: dict[str, list[str]] = Field(description="Label or type -> datetime properties.")
    load: list[LoadStep]
    cleanup: list[str] = Field(description="Run after `load`: removes the temporary ids.")
    clock: Clock
    max_hops: int
    max_rows: int
    queries: dict[str, str]
    prepared: list[PreparedQuestion]


# --- Queries for Relate ---------------------------------------------------------------------
_FULLTEXT_CALL = re.compile(
    r"db\.index\.fulltext\.queryNodes\(\s*('[^']*')\s*,\s*(\$\w+)\s*,"
    r"\s*\{\s*limit\s*:\s*(\$\w+)\s*\}\s*\)"
)


def relate_fulltext(cypher: str) -> str:
    """Neo4j's 3-argument full-text call as Relate takes it: 2 arguments, then LIMIT.

    `db.index.fulltext.queryNodes('i', $query, {limit: $limit}) YIELD node RETURN ...`
    becomes `db.index.fulltext.queryNodes('i', $query) YIELD node RETURN ... LIMIT
    $limit`. Both engines yield the hits best score first, so the limit keeps the same
    top hits. Queries without a full-text call come back unchanged."""
    calls = _FULLTEXT_CALL.findall(cypher)
    if not calls:
        if "fulltext" in cypher:
            raise ValueError("a full-text call this rewrite doesn't know")
        return cypher
    body = _FULLTEXT_CALL.sub(lambda m: f"db.index.fulltext.queryNodes({m[1]}, {m[2]})", cypher)
    if len(calls) > 1 or re.search(r"\bLIMIT\b", body, re.IGNORECASE):
        raise ValueError("only one full-text call and no LIMIT of its own can be rewritten")
    limit = calls[0][2]
    return body.rstrip() + f"\nLIMIT {limit}\n"


def shipped_queries(module: Any = qa_graph, max_hops: int = MAX_HOPS) -> dict[str, str]:
    """The page's queries: `wikipulse.qa.graph`'s constants after `relate_fulltext()`."""
    queries = {}
    for name in QUERY_NAMES:
        text = getattr(module, name)
        if name == "PATHS":
            text = text % int(max_hops)
        queries[name] = relate_fulltext(text)
    return queries


def bind_times(query: str, names: Sequence[str] = TIME_PARAMETERS) -> str:
    """Each `$now`, `$today_start`, `$week_start` outside strings and comments becomes
    `datetime($now)` etc., so the ISO text a JSON parameter carries is compared as a
    datetime. The browser does the same (`bindTimes()` in site/cypher-check.js)."""
    masked = mask(query)
    out, start = [], 0
    for found in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*)", masked):
        if found[1] not in names:
            continue
        out.append(query[start : found.start()] + f"datetime({found[0]})")
        start = found.end()
    return "".join(out) + query[start:]


# --- Values: one comparable form for both engines -------------------------------------------
_ISO_TIME = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})(\[[^\]]+\])?$"
)


def iso_text(moment: datetime) -> str:
    """A datetime as compact ISO text in UTC ("2026-11-05T16:00:36.573Z")."""
    text = moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return re.sub(r"\.?0+Z$", "Z", text) if "." in text else text


def canonical_time(text: str) -> str:
    """ISO datetime text from either engine as UTC with six fraction digits; other text
    unchanged. (Neo4j writes nanoseconds and "+00:00", Relate milliseconds and "Z".)"""
    found = _ISO_TIME.match(text)
    if not found:
        return text
    base, fraction, offset = found[1], (found[2] or ""), found[3]
    if offset not in ("Z", "+00:00", "-00:00"):
        moment = datetime.fromisoformat(f"{base}.{(fraction + '000000')[:6]}{offset}")
        base = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{(fraction + '000000')[:6]}Z"


def canonical(value: Any) -> Any:
    """A query result value as plain JSON in which both engines' results compare.

    Works on the Neo4j driver's values and on Relate's (`relate-db`), by shape: nodes
    become {"labels", "properties"}, relationships {"type", "properties"}, paths
    {"nodes", "relationships"} (embeddings left out); datetimes UTC ISO text with six
    fraction digits; integral floats integers (JSON from WebAssembly writes 1.0 as 1);
    map keys sorted. The browser's `canonical()` (site/graph-db.js) does the same to
    the WebAssembly build's JSON."""
    if hasattr(value, "element_id") and hasattr(value, "labels"):
        return {"labels": sorted(value.labels), "properties": _props(dict(value.items()))}
    if hasattr(value, "element_id") and hasattr(value, "type"):
        return {"type": value.type, "properties": _props(dict(value.items()))}
    if hasattr(value, "nodes") and hasattr(value, "relationships"):
        return {
            "nodes": [canonical(n) for n in value.nodes],
            "relationships": [canonical(r) for r in value.relationships],
        }
    if hasattr(value, "iso_format"):  # the driver's DateTime, Date, Time, Duration
        return canonical_time(value.iso_format())
    if hasattr(value, "text") and hasattr(value, "nanosecond"):  # relate.Temporal
        return canonical_time(value.text)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else value
    if isinstance(value, str):
        return canonical_time(value)
    if isinstance(value, datetime):
        return canonical_time(value.isoformat())
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [canonical(v) for v in value]
    if isinstance(value, Mapping):
        return {k: canonical(value[k]) for k in sorted(value)}
    if isinstance(value, int):
        return value
    return str(value)  # e.g. relate.Duration: its ISO text


def _props(props: Mapping[str, Any]) -> dict[str, Any]:
    hidden = qa_graph.HIDDEN_PROPERTIES
    return {k: canonical(props[k]) for k in sorted(props) if k not in hidden}


def canonical_rows(columns: Sequence[str], records: Iterable[Mapping[str, Any]]) -> Rows:
    return Rows(columns=list(columns), rows=[[canonical(r[c]) for c in columns] for r in records])


# --- Comparing results (the same rules as compareRows() in site/graph-db.js) ----------------
def _text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def unordered(value: Any) -> Any:
    """Every list sorted, recursively: for lists whose order the query leaves open, such
    as `collect()` without a full ORDER BY before it."""
    if isinstance(value, list):
        return sorted((unordered(v) for v in value), key=_text)
    if isinstance(value, dict):
        return {k: unordered(v) for k, v in value.items()}
    return value


def same_multiset(a: Sequence[Any], b: Sequence[Any]) -> bool:
    return sorted(map(_text, a)) == sorted(map(_text, b))


def _monotonic(values: list[Any]) -> bool:
    numbers = all(isinstance(v, int | float) and not isinstance(v, bool) for v in values)
    if not (numbers or all(isinstance(v, str) for v in values)):
        return False
    pairs = list(zip(values, values[1:], strict=False))
    return all(a >= b for a, b in pairs) or all(a <= b for a, b in pairs)


def tie_column(live: Sequence[list], stored: Sequence[list]) -> int | None:
    """The column on which two equally long, equally sorted results differ only among
    the rows that tie with the last row (which tied rows a LIMIT keeps is up to the
    engine); None if there is none."""
    if len(live) != len(stored) or not live:
        return None
    for c in range(len(live[0])):
        a, b = [row[c] for row in live], [row[c] for row in stored]
        if not (_monotonic(a) and _monotonic(b)) or _text(a[-1]) != _text(b[-1]):
            continue
        boundary = _text(a[-1])
        if sum(1 for v in a if _text(v) == boundary) < 2:
            continue
        before_a = [unordered(r) for r in live if _text(r[c]) != boundary]
        before_b = [unordered(r) for r in stored if _text(r[c]) != boundary]
        if same_multiset(before_a, before_b):
            return c
    return None


def compare_rows(live: Sequence[list], stored: Sequence[list]) -> str:
    """ "identical", "other order", "list order", "tie" (see `tie_column`) or "different"."""
    if _text(list(live)) == _text(list(stored)):
        return "identical"
    if same_multiset(live, stored):
        return "other order"
    if same_multiset([unordered(r) for r in live], [unordered(r) for r in stored]):
        return "list order"
    if tie_column(live, stored) is not None:
        return "tie"
    return "different"


# --- Building the export (pure functions) ---------------------------------------------------
def to_plain_property(value: Any) -> tuple[Any, bool]:
    """A stored property as JSON, and whether it was a datetime."""
    from neo4j.time import DateTime

    if isinstance(value, DateTime):
        if value.nanosecond % 1000:
            raise ValueError(f"{value} has nanoseconds that ISO microseconds would lose")
        return iso_text(value.to_native()), True
    if isinstance(value, datetime):
        return iso_text(value), True
    return value, False


def select_properties(
    kind: str, props: Mapping[str, Any], allowed: Mapping[str, tuple[str, ...]]
) -> tuple[dict[str, Any], set[str], set[str]]:
    """The allowed properties as JSON, the datetime names among them, the left-out names."""
    names = allowed[kind]
    out, datetimes = {}, set()
    for name in names:
        if props.get(name) is None:
            continue
        out[name], is_time = to_plain_property(props[name])
        if is_time:
            datetimes.add(name)
    return out, datetimes, set(props) - set(names)


def _note_time(datetimes: dict[str, set[str]], kind: str, names: set[str]) -> None:
    datetimes.setdefault(kind, set()).update(names)


def build_load(
    nodes: Sequence[Mapping[str, Any]], relationships: Sequence[Mapping[str, Any]]
) -> tuple[list[LoadStep], list[str], dict[str, list[str]], dict[str, int]]:
    """The load steps, cleanup statements, datetime properties and counts.

    `nodes` are {"id", "labels", "props"} (one label each), `relationships` {"start",
    "end", "type", "props"}, both as read from Neo4j. Nodes get compact ids 0..n-1 in a
    stable order, so re-exporting an unchanged graph gives the same file."""
    by_label: dict[str, list[tuple[Any, Mapping[str, Any]]]] = {}
    for node in nodes:
        if len(node["labels"]) != 1:
            raise ValueError(f"node with labels {node['labels']}: one label each expected")
        by_label.setdefault(node["labels"][0], []).append((node["id"], node["props"]))
    unknown_labels = set(by_label) - set(NODE_PROPERTIES)
    if unknown_labels:
        raise ValueError(f"labels the export doesn't know: {sorted(unknown_labels)}")

    datetimes: dict[str, set[str]] = {}
    left_out: set[str] = set()
    ids: dict[Any, int] = {}
    label_of: dict[Any, str] = {}
    node_rows: dict[str, list[dict[str, Any]]] = {}
    for label in sorted(by_label):
        members = sorted(by_label[label], key=lambda item: _node_sort_key(item[1]))
        for element_id, props in members:
            row, times, dropped = select_properties(label, props, NODE_PROPERTIES)
            _note_time(datetimes, label, times)
            left_out |= {f"{label}.{d}" for d in dropped}
            ids[element_id] = len(ids)
            label_of[element_id] = label
            node_rows.setdefault(label, []).append({"_id": ids[element_id], **row})

    rel_rows: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for rel in relationships:
        rtype = rel["type"]
        if rtype not in REL_PROPERTIES:
            raise ValueError(f"relationship type the export doesn't know: {rtype}")
        row, times, dropped = select_properties(rtype, rel["props"], REL_PROPERTIES)
        _note_time(datetimes, rtype, times)
        left_out |= {f"{rtype}.{d}" for d in dropped}
        key = (rtype, label_of[rel["start"]], label_of[rel["end"]])
        rel_rows.setdefault(key, []).append(
            {"s": ids[rel["start"]], "e": ids[rel["end"]], "p": row}
        )
    if left_out:
        logger.info("Properties left out: %s", ", ".join(sorted(left_out)))

    steps = [LoadStep(cypher=s) for s in [*SCHEMA, *FULLTEXT_INDEXES]]
    labels = sorted(node_rows)
    steps += [LoadStep(cypher=_id_index(label)) for label in labels]
    for label in labels:
        create = f"UNWIND $rows AS row CREATE (n:{label}) SET n = row"
        cypher = _with_times(create, "n", "row", datetimes.get(label))
        steps.append(LoadStep(cypher=cypher, rows=node_rows[label]))
    for (rtype, start, end), rows in sorted(rel_rows.items()):
        rows.sort(key=lambda r: (r["s"], r["e"], json.dumps(r["p"], sort_keys=True)))
        create = (
            f"UNWIND $rows AS row MATCH (a:{start} {{_id: row.s}}) MATCH (b:{end} {{_id: row.e}}) "
            f"CREATE (a)-[r:{rtype}]->(b) SET r = row.p"
        )
        steps.append(
            LoadStep(cypher=_with_times(create, "r", "row.p", datetimes.get(rtype)), rows=rows)
        )
    cleanup = ["MATCH (n) REMOVE n._id"] + [
        f"DROP INDEX load_{label.lower()}_id" for label in labels
    ]
    counts = {"nodes": len(ids), "relationships": sum(len(r) for r in rel_rows.values())}
    counts |= {label: len(rows) for label, rows in node_rows.items()}
    for (rtype, _, _), rows in sorted(rel_rows.items()):
        counts[rtype] = counts.get(rtype, 0) + len(rows)
    return steps, cleanup, {k: sorted(v) for k, v in sorted(datetimes.items()) if v}, counts


def _node_sort_key(props: Mapping[str, Any]) -> str:
    return str(props.get("key") or props.get("title") or "")


def _id_index(label: str) -> str:
    return f"CREATE INDEX load_{label.lower()}_id FOR (n:{label}) ON (n._id)"


def _with_times(cypher: str, var: str, source: str, names: Iterable[str] | None) -> str:
    """`cypher` plus `SET var.p = datetime(source.p)` for each datetime property."""
    names = sorted(names or [])
    if not names:
        return cypher
    sets = ", ".join(f"{var}.{n} = datetime({source}.{n})" for n in names)
    return f"{cypher} SET {sets}"


def parse_citation(text: str) -> Source:
    """'Ueno Bank, revision 1383953174' -> a source with a permanent link (as day.json)."""
    title, sep, revision = text.rpartition(", revision ")
    if sep and revision.isdigit():
        url = f"https://en.wikipedia.org/w/index.php?oldid={revision}"
        return Source(title=title, url=url, revision=int(revision))
    return Source(title=text)


def build_prepared(
    answers: Iterable[Mapping[str, Any]],
    results: Mapping[str, Any],
    neo4j_rows: Mapping[str, Rows],
) -> list[PreparedQuestion]:
    """One prepared question per stored answer whose generated query ran."""
    scores = {q["id"]: q.get("correctness") or {} for q in results["questions"]}
    out = []
    for answer in answers:
        qid = answer["question_id"]
        if not (answer.get("cypher_ran") and answer.get("cypher") and qid in neo4j_rows):
            continue
        out.append(
            PreparedQuestion(
                id=qid,
                question=answer["question"],
                type=answer.get("type") or "",
                cypher=answer["cypher"],
                stored_row_count=int(answer.get("cypher_rows") or 0),
                neo4j=neo4j_rows[qid],
                answer=answer.get("text") or "",
                sources=[parse_citation(c) for c in answer.get("citations", [])],
                model=answer.get("model"),
                correctness={k: float(v) for k, v in scores.get(qid, {}).items()},
            )
        )
    return out


def time_parameters(now: datetime) -> dict[str, datetime]:
    """$now, $today_start, $week_start exactly as the evaluation computed them."""
    from wikipulse.qa.retrieve import time_parameters as qa_times

    return qa_times(now).as_params()


def to_json(export: GraphExport) -> str:
    """Compact JSON (UTF-8 text, no extra whitespace)."""
    data = export.model_dump(mode="json")
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"


# --- Reading Neo4j (the frozen snapshot) ----------------------------------------------------
NODES = "MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props"
RELATIONSHIPS = """
MATCH (a)-[r]->(b)
RETURN elementId(a) AS start, elementId(b) AS end, type(r) AS type, properties(r) AS props
"""


def read_graph(driver: Any) -> tuple[list[dict], list[dict], datetime]:
    """All nodes and relationships, and the graph's clock, in a READ session."""
    from neo4j import READ_ACCESS

    with driver.session(default_access_mode=READ_ACCESS) as session:
        nodes = [r.data() for r in session.run(NODES)]
        relationships = [r.data() for r in session.run(RELATIONSHIPS)]
        clock = session.run(qa_graph.CLOCK).single()
    times = [t.to_native() for t in (clock["started"], clock["ended"]) if t is not None]
    return nodes, relationships, max(times)


def run_neo4j(driver: Any, cypher: str, params: Mapping[str, Any]) -> Rows:
    """A query's rows in canonical form, in a READ session with a timeout."""
    from neo4j import READ_ACCESS, Query

    with driver.session(default_access_mode=READ_ACCESS) as session:
        result = session.run(Query(cypher, timeout=10), dict(params))
        records = list(result)
        columns = list(result.keys())
    return canonical_rows(columns, records)


def read_answers() -> tuple[list[dict], dict]:
    if not ANSWERS_FILE.exists() or not RESULTS_FILE.exists():
        raise FileNotFoundError(f"the {RUN} evaluation files are missing ({ANSWERS_FILE})")
    lines = ANSWERS_FILE.read_text(encoding="utf-8").splitlines()
    answers = [json.loads(line) for line in lines if line.strip()]
    return answers, json.loads(RESULTS_FILE.read_text(encoding="utf-8"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uri", help="The snapshot's Neo4j (default: EVAL_QA_NEO4J_URI).")
    parser.add_argument("--out", type=Path, default=OUT_FILE)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Read the snapshot and the 12a answers, build the export, write it."""
    from neo4j import GraphDatabase

    args = parse_args(argv)
    setup_logging()
    settings = get_settings()
    uri = args.uri or settings.eval_qa_neo4j_uri
    answers, results = read_answers()
    auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
    with GraphDatabase.driver(uri, auth=auth) as driver:
        nodes, relationships, now = read_graph(driver)
        params = time_parameters(now)
        neo4j_rows = {
            a["question_id"]: run_neo4j(driver, a["cypher"], params)
            for a in answers
            if a.get("cypher_ran") and a.get("cypher")
        }
    steps, cleanup, datetimes, counts = build_load(nodes, relationships)
    prepared = build_prepared(answers, results, neo4j_rows)
    for question in prepared:
        if len(question.neo4j.rows) != question.stored_row_count:
            logger.warning(
                "%s: Neo4j now returns %d rows, the %s run got %d",
                question.id, len(question.neo4j.rows), RUN, question.stored_row_count,
            )  # fmt: skip
    export = GraphExport(
        schema_version=SCHEMA_VERSION,
        snapshot=SNAPSHOT,
        run=RUN,
        counts=counts,
        batch=BATCH,
        datetimes=datetimes,
        load=steps,
        cleanup=cleanup,
        clock=Clock(**{k: iso_text(v) for k, v in params.items()}),
        max_hops=MAX_HOPS,
        max_rows=MAX_ROWS,
        queries=shipped_queries(),
        prepared=prepared,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = to_json(export)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    logger.info(
        "Wrote %s (%.0f KB): %s, %d queries, %d prepared questions",
        args.out, len(text.encode("utf-8")) / 1024, counts, len(export.queries), len(prepared),
    )  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
