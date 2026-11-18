"""Why the read-only check refuses GQL's INSERT and NEXT (spec 13a, review problem 1).

Relate, the engine the demo runs in the browser, speaks GQL as well as Cypher. Every
shared validator case marked `relate_writes` (tests/fixtures/cypher_cases.json) is run
here *without* the check, on a small in-memory Relate graph (`relate-db`, the same
version as the WebAssembly build), and must change that graph. So each refusal guards
against a real write, not a hypothetical one. On the server, Neo4j's READ transaction
would stop these too; in the browser the validator is the only layer.

No Docker and no network: `relate.connect(":memory:")` is a library in this process.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from wikipulse.qa.cypher import FORBIDDEN_WORDS, UnsafeCypherError, validate_cypher

relate = pytest.importorskip("relate")

CASES_FILE = Path(__file__).resolve().parent / "fixtures" / "cypher_cases.json"
CASES = json.loads(CASES_FILE.read_text(encoding="utf-8"))["validate"]
WRITES = [case["query"] for case in CASES if case.get("relate_writes")]
SETUP = (
    "CREATE (:Entity {name: 'Ueno Bank'})-[:RELATED_TO {relation: 'based in'}]->"
    "(:Entity {name: 'Paraguay'})",
    "CREATE (:Entity {name: 'Asunción'})",  # unconnected: NODETACH DELETE may remove it
)
STATE = (
    "MATCH (n) OPTIONAL MATCH (n)-[r]->() "
    "RETURN labels(n) AS labels, properties(n) AS props, type(r) AS rel ORDER BY n.name, rel"
)


def state(conn: Any) -> str:
    rows = [dict(r) for r in conn.execute(STATE).fetchall()]
    return json.dumps(rows, sort_keys=True, default=str)


@pytest.mark.parametrize("query", WRITES, ids=[str(i) for i in range(len(WRITES))])
def test_each_refused_gql_write_really_writes_in_relate(query: str) -> None:
    with pytest.raises(UnsafeCypherError, match="not a read-only query"):
        validate_cypher(query)
    conn = relate.connect(":memory:")
    try:
        for statement in SETUP:
            conn.execute(statement)
        before = state(conn)
        conn.execute(query).fetchall()  # unchecked, as if the validator had let it through
        assert state(conn) != before, "this query did not write; it needs no relate_writes mark"
    finally:
        conn.close()


def test_the_write_cases_cover_gqls_write_and_chaining_words() -> None:
    assert {"INSERT", "NEXT", "NODETACH"} <= set(FORBIDDEN_WORDS)
    words = " ".join(WRITES).upper()
    assert all(word in words for word in ("INSERT", "NEXT", "NODETACH"))
