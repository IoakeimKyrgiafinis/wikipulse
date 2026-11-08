"""The tools' logic without MCP: limits, id resolution, error messages."""

from datetime import timedelta

import pytest

from tests.mcp_server.helpers import (
    CRISIS,
    DARTS,
    NOW,
    PENA,
    UENO,
    UENO_HOLDING,
    UENO_PARK,
    ScriptedAsker,
    ToolGraph,
    crisis_with,
    many_relations,
)
from wikipulse.mcp_server.explore import (
    MAX_DESCRIPTION_CHARS,
    MAX_EVENT_RELATIONS,
    MAX_LIMIT,
    MAX_QUESTION_CHARS,
    Explorer,
    ToolInputError,
    rank,
)


def explorer(**graph_fields) -> Explorer:
    return Explorer(ToolGraph(**graph_fields), ScriptedAsker(), trending_hours=24)


def test_trending_uses_the_graph_clock_and_orders_by_strength() -> None:
    tools = explorer()
    result = tools.trending(limit=5)
    assert result.as_of == NOW
    assert [e.event_id for e in result.events] == [CRISIS.key, DARTS.key]  # 5 pages, then 2
    assert result.events[0].top_pages == ["Ueno Bank", "Santiago Peña", "Pedro Alliana"]
    assert tools.graph.trending_calls == [(NOW - timedelta(hours=24), 5)]


def test_trending_clamps_the_limit_and_explains_an_empty_window() -> None:
    tools = explorer()
    result = tools.trending(limit=1000, hours=1)
    assert tools.graph.trending_calls[-1][1] == MAX_LIMIT
    assert result.events == []
    assert "larger `hours`" in result.note


def test_trending_on_an_empty_graph() -> None:
    result = explorer(clock_value=None).trending()
    assert result.events == [] and result.as_of is None
    assert result.note == "The graph has no events yet."


def test_long_descriptions_are_cut() -> None:
    long = crisis_with(description="x" * 2000)
    result = explorer(events_by_key={CRISIS.key: long}).trending()
    assert len(result.events[0].description) == MAX_DESCRIPTION_CHARS
    assert result.events[0].description.endswith("…")


@pytest.mark.parametrize("event_id", ["run:341", "341", "event 341", "#341", "event://run:341"])
def test_an_event_is_found_by_key_or_number(event_id: str) -> None:
    assert explorer().find_event(event_id).key == CRISIS.key


def test_an_unknown_event_says_where_to_get_ids() -> None:
    with pytest.raises(ToolInputError, match="get_trending_events"):
        explorer().find_event("run:999")
    with pytest.raises(ToolInputError, match="not an event id.*use ask or search_entities"):
        explorer().find_event("the banking crisis")


def test_an_event_number_in_two_runs_is_ambiguous() -> None:
    other = crisis_with(key="other:341")
    graph = {"events_by_key": {CRISIS.key: CRISIS, other.key: other}}
    with pytest.raises(ToolInputError, match="several grouping runs: run:341, other:341"):
        explorer(**graph).find_event("341")


def test_event_detail_has_entities_relations_briefing_and_limits() -> None:
    relations = many_relations(CRISIS.key, MAX_EVENT_RELATIONS + 5)
    tools = explorer(relations=relations, briefings={CRISIS.key: "Peña resigned [1]."})
    detail = tools.event("341")
    assert detail.event.title == CRISIS.title
    assert [p.url for p in detail.pages][0] == "https://en.wikipedia.org/w/index.php?oldid=101"
    assert [(e.name, e.wikidata_id) for e in detail.entities] == [("Pedro Alliana", "Q3")]
    assert detail.entities[0].source.revision == 7
    assert len(detail.relations) == MAX_EVENT_RELATIONS and detail.more_relations
    assert not detail.more_entities
    assert detail.briefing == "Peña resigned [1]."
    assert explorer().event("407").briefing is None


def test_ask_maps_the_answer_with_numbered_sources_and_notes() -> None:
    tools = explorer()
    result = tools.ask("  How are Ueno Bank and   Santiago Peña connected? ")
    assert tools.asker.questions == ["How are Ueno Bank and Santiago Peña connected?"]
    assert result.answer.endswith("[1][2].") and result.answerable
    assert [s.title for s in result.sources] == ["Ueno Bank", "Santiago Peña"]
    assert result.sources[0].url.endswith("oldid=101")
    assert result.notes[0].startswith("The query results stop")
    assert "1 sentence(s) without a citation were removed" in result.notes[1]
    assert result.as_of == NOW and result.seconds == 3.2


def test_ask_rejects_empty_and_long_questions_before_any_model_call() -> None:
    tools = explorer()
    with pytest.raises(ToolInputError, match="empty"):
        tools.ask("   ")
    with pytest.raises(ToolInputError, match=f"limit is {MAX_QUESTION_CHARS}"):
        tools.ask("why " * 200)
    assert tools.asker.questions == []


def test_ask_without_an_answerer() -> None:
    with pytest.raises(ToolInputError, match="not available"):
        Explorer(ToolGraph()).ask("What happened today?")


def test_entities_resolve_by_exact_name_alias_or_id() -> None:
    tools = explorer()
    assert tools.resolve_entity("santiago pena").key == PENA.key  # case and accents folded
    assert tools.resolve_entity("Peña").key == PENA.key  # an alias
    assert tools.resolve_entity("Q1").key == UENO.key


def test_an_ambiguous_name_lists_the_candidates_with_ids() -> None:
    tools = explorer(entities=[UENO, UENO_PARK, UENO_HOLDING, PENA])
    with pytest.raises(ToolInputError, match=r"Several entities are named 'Ueno'.*id Q9.*id name:"):
        tools.resolve_entity("Ueno")


def test_a_partial_name_suggests_close_matches() -> None:
    with pytest.raises(ToolInputError, match=r"exactly 'Bank'. Close matches: Ueno Bank .*Q1"):
        explorer().resolve_entity("Bank")


def test_an_unknown_name_points_to_search() -> None:
    with pytest.raises(ToolInputError, match="No entity matches 'Eurovision'.*search_entities"):
        explorer().resolve_entity("Eurovision")
    with pytest.raises(ToolInputError, match="empty"):
        explorer().resolve_entity("  ")


def test_connection_returns_paths_with_sources() -> None:
    result = explorer().connection("Ueno Bank", "Santiago Peña")
    assert (result.entity_a.entity_id, result.entity_b.entity_id) == ("Q1", "Q2")
    path = result.paths[0]
    assert path.summary == (f"Ueno Bank —involved in→ {CRISIS.title} ←involved in— Santiago Peña")
    assert [s.source.revision for s in path.steps] == [101, 102]
    assert result.note is None


def test_no_connection_says_what_to_try_next() -> None:
    result = explorer(connected=False).connection("Ueno Bank", "Santiago Peña")
    assert result.paths == []
    assert "No connection within 4 steps" in result.note


def test_connecting_an_entity_to_itself_is_an_error() -> None:
    with pytest.raises(ToolInputError, match="same entity"):
        explorer().connection("Peña", "Santiago Peña")


def test_search_ranks_exact_matches_first_and_counts_events() -> None:
    tools = explorer(entities=[UENO, UENO_PARK, UENO_HOLDING, PENA])
    result = tools.search("Ueno", limit=2)
    assert [e.entity_id for e in result.entities] == ["name:ueno", "Q9"]  # both exact
    assert all(e.exact for e in result.entities)
    assert result.entities[1].aliases == ["Ueno"]
    full = tools.search("Ueno")
    assert full.entities[-1].entity_id == UENO.key and full.entities[-1].events == 2
    assert [(e.event_id, e.title) for e in full.entities[-1].top_events] == [
        (CRISIS.key, CRISIS.title)
    ]


def test_search_without_matches_has_a_note() -> None:
    result = explorer().search("the")  # a stopword: no search terms at all
    assert result.entities == []
    assert "No entity in the knowledge graph matches 'the'" in result.note
    assert "Do not answer from other knowledge" in result.note


def test_rank_prefers_more_shared_words_then_shorter_names() -> None:
    ranked = rank("central bank", [UENO, UENO_PARK])
    assert ranked[0] == UENO


def test_close_closes_the_graph_and_the_answerer() -> None:
    tools = explorer()
    tools.close()
    assert tools.graph.closed and tools.asker.closed


def test_search_says_when_matches_share_only_some_words() -> None:
    result = explorer().search("Federal Bank")  # only "Bank" matches: Ueno Bank
    assert [e.name for e in result.entities] == ["Ueno Bank"]
    assert "share only some words" in result.note
    assert explorer().search("Ueno Bank").note is None
