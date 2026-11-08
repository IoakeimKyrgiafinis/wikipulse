"""Generated Cypher: only read queries run, always with a result limit."""

from datetime import UTC, datetime

import pytest

from tests.llm.helpers import ScriptedClient
from wikipulse.qa.cypher import (
    MAX_HOPS,
    CypherWriter,
    TimeParameters,
    UnsafeCypherError,
    blank_out,
    validate_cypher,
)

READ = "MATCH (ev:Event) WHERE ev.started_at >= $today_start RETURN ev.key AS event_key"


def test_a_read_query_gets_a_limit() -> None:
    assert validate_cypher(READ, max_rows=25) == READ + "\nLIMIT 25"


def test_a_small_limit_stays_and_a_large_one_is_lowered() -> None:
    assert validate_cypher(READ + " LIMIT 10", 25).endswith("LIMIT 10")
    assert validate_cypher(READ + " LIMIT 1000", 25).endswith("LIMIT 25")
    assert validate_cypher(READ + " LIMIT $n", 25).endswith("LIMIT 25")


def test_a_limit_inside_the_query_does_not_count_for_the_result() -> None:
    query = "MATCH (n) WITH n LIMIT 5 MATCH (n)--(m) RETURN m.name"
    assert validate_cypher(query, 25).endswith("RETURN m.name\nLIMIT 25")


@pytest.mark.parametrize(
    "query",
    [
        "CREATE (n:Entity {name: 'x'}) RETURN n",
        "MATCH (n) DETACH DELETE n RETURN 1",
        "MATCH (n) DELETE n",
        "MATCH (n:Entity) SET n.name = 'x' RETURN n",
        "MATCH (n:Entity) REMOVE n.name RETURN n",
        "MERGE (n:Entity {key: 'k'}) RETURN n",
        "MATCH (n) CALL apoc.do.it(n) RETURN n",
        "CALL db.labels() YIELD label RETURN label",
        "MATCH (n) CALL { WITH n DELETE n } IN TRANSACTIONS RETURN 1",
        "LOAD CSV FROM 'file:///x' AS row RETURN row",
        "MATCH (n) FOREACH (x IN [1] | SET n.y = x) RETURN n",
        "DROP INDEX entity_name",
        "match (n) set n.x = 1 return n",  # lower case too
        "MATCH (n) /* hidden */ CREATE (m) RETURN n",
    ],
)
def test_anything_that_writes_or_calls_procedures_is_rejected(query: str) -> None:
    with pytest.raises(UnsafeCypherError):
        validate_cypher(query)


def test_words_inside_strings_comments_and_property_names_are_fine() -> None:
    query = (
        "MATCH (n:Entity) // CREATE nothing\n"
        "WHERE n.name = 'Set It Off' OR n.name CONTAINS \"delete\" OR n.set = 1 "
        "RETURN n.name AS `create`"
    )
    assert validate_cypher(query).startswith("MATCH (n:Entity)")


def test_blank_out_keeps_the_structure_but_not_the_text() -> None:
    assert blank_out("MATCH (n {name: 'CREATE'}) // SET\nRETURN n") == (
        "MATCH (n {name: ''})  \nRETURN n"
    )


@pytest.mark.parametrize(
    "query, reason",
    [
        ("", "empty"),
        ("MATCH (n) RETURN n; MATCH (m) DELETE m", "one statement"),
        ("MATCH (n:Entity)", "no RETURN"),
        ("EXPLAIN MATCH (n) RETURN n", "starts with"),
        ("MATCH p = (a)-[*]-(b) RETURN p", "upper bound"),
        ("MATCH p = (a)-[:RELATED_TO*2..]-(b) RETURN p", "upper bound"),
        ("MATCH p = (a)-[:RELATED_TO*1..9]-(b) RETURN p", "longer than"),
        ("MATCH p = ((a)-->(b))+ RETURN p", "quantified"),
    ],
)
def test_rejections_say_why(query: str, reason: str) -> None:
    with pytest.raises(UnsafeCypherError, match=reason):
        validate_cypher(query)


def test_a_limit_inside_a_comment_or_string_does_not_count() -> None:
    query = "MATCH (n) RETURN n.name // LIMIT 1000"
    assert validate_cypher(query, 25) == query + "\nLIMIT 25"
    query = "MATCH (n) RETURN n.name /* LIMIT 3 */"
    assert validate_cypher(query, 25).endswith("\nLIMIT 25")
    query = "MATCH (n) RETURN 'LIMIT 3' AS x"
    assert validate_cypher(query, 25) == query + "\nLIMIT 25"
    # A large limit followed by a comment is lowered (the comment goes).
    assert validate_cypher("MATCH (n) RETURN n LIMIT 1000 // ok", 25) == (
        "MATCH (n) RETURN n LIMIT 25"
    )


def test_every_branch_of_a_union_gets_a_limit() -> None:
    query = "MATCH (n) RETURN n.name AS name LIMIT 1000 UNION MATCH (m) RETURN m.name AS name"
    assert validate_cypher(query, 25) == (
        "MATCH (n) RETURN n.name AS name LIMIT 25 UNION MATCH (m) RETURN m.name AS name\nLIMIT 25"
    )
    query = "MATCH (n) RETURN n.name AS name\nUNION ALL\nMATCH (m) RETURN m.name AS name LIMIT 5"
    assert validate_cypher(query, 25) == (
        "MATCH (n) RETURN n.name AS name\nLIMIT 25\nUNION ALL\n"
        "MATCH (m) RETURN m.name AS name LIMIT 5"
    )


def test_a_limit_inside_a_subquery_does_not_count_for_the_result() -> None:
    query = "MATCH (n) RETURN n.name, COUNT { MATCH (n)--() RETURN 1 LIMIT 5 } AS c"
    assert validate_cypher(query, 25) == query + "\nLIMIT 25"


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a)-[:RELATED_TO]-+(b) RETURN b.name",
        "MATCH (a)-[:RELATED_TO]-*(b) RETURN b.name",
        "MATCH (a)-[:RELATED_TO]-{1,}(b) RETURN b.name",
        "MATCH (a)<-[:RELATED_TO]-{2,}(b) RETURN b.name",
        "MATCH (a)-->+(b) RETURN b.name",
        "MATCH (a)--*(b) RETURN b.name",
        "MATCH p = SHORTEST 1 (a)-[:R]-+(b) RETURN p",
        "MATCH p = ANY SHORTEST (a)-[:R]-*(b) RETURN p",
        "MATCH p = ((a)-->(b)){1,} RETURN p",
        "MATCH p = ((a)-->(b))* RETURN p",
    ],
)
def test_unbounded_quantified_paths_are_rejected(query: str) -> None:
    with pytest.raises(UnsafeCypherError, match="quantified path pattern .* upper bound"):
        validate_cypher(query)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a)-[:RELATED_TO]-{1,9}(b) RETURN b.name",
        "MATCH (a)-->{7}(b) RETURN b.name",
        "MATCH p = SHORTEST 1 (a)-[:R]-{,10}(b) RETURN p",
    ],
)
def test_quantified_paths_longer_than_the_hop_limit_are_rejected(query: str) -> None:
    with pytest.raises(UnsafeCypherError, match=f"longer than {MAX_HOPS} hops"):
        validate_cypher(query)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a)-[:RELATED_TO]-{1,3}(b) RETURN b.name",
        "MATCH (a)<-[:RELATED_TO]-{,4}(b) RETURN b.name",
        "MATCH p = ANY SHORTEST (a)-[:R]-{1,4}(b) RETURN p",
        "MATCH p = ((a)-->(b)){2} RETURN p",
    ],
)
def test_bounded_quantified_paths_are_accepted(query: str) -> None:
    assert validate_cypher(query) == query + "\nLIMIT 25"


def test_bounded_paths_fenced_answers_and_a_trailing_semicolon_are_accepted() -> None:
    query = "```cypher\nMATCH p = (a)-[:RELATED_TO|INVOLVED_IN*1..4]-(b) RETURN p LIMIT 3;\n```"
    assert validate_cypher(query) == (
        "MATCH p = (a)-[:RELATED_TO|INVOLVED_IN*1..4]-(b) RETURN p LIMIT 3"
    )
    assert validate_cypher("MATCH (a)-[*3]-(b) RETURN b.name").endswith("LIMIT 25")
    # A multiplication inside a list is not a path pattern.
    assert validate_cypher("MATCH (n) RETURN [x IN [1, 2] | x * 2] AS doubled")


def test_the_writer_asks_with_the_time_parameters_and_returns_the_query() -> None:
    client = ScriptedClient(['{"cypher": "MATCH (ev:Event) RETURN ev.key"}'])
    now = datetime(2026, 11, 5, 22, 54, tzinfo=UTC)
    times = TimeParameters(now, now.replace(hour=0, minute=0), now.replace(day=1))
    query = CypherWriter(client).write("What happened today?", times, max_rows=15)
    assert query == "MATCH (ev:Event) RETURN ev.key"
    request = client.requests[0]
    message = request.messages[0].content
    assert "2026-11-05T22:54:00+00:00" in message and "at most 15 rows" in message
    assert "What happened today?" in message
    assert "INVOLVED_IN" in request.system  # the schema is in the prompt
    assert request.json_schema["required"] == ["cypher"]
