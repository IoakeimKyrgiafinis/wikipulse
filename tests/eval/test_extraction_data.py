"""Which events, and saved re-extractions (data/extractions/<name>.jsonl)."""

from datetime import UTC, datetime

import pytest

from tests.graph.helpers import event, saff_page
from wikipulse.eval.extraction_data import (
    EventSelection,
    append_event,
    load_from_file,
    parse_events,
    read_records,
    vote_type,
)
from wikipulse.graph.extract import ExtractedEntity, ExtractedRelation, Extraction
from wikipulse.graph.link import UNLINKED, Link
from wikipulse.graph.store import build_graph_event

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
PAGE = saff_page(revision=111)


def test_events_are_numbers_a_day_or_a_range_of_start_times():
    assert parse_events("356, 341").numbers == (341, 356)
    day = parse_events("2026-11-05")
    assert (day.since, day.until) == (
        datetime(2026, 11, 5, tzinfo=UTC),
        datetime(2026, 11, 6, tzinfo=UTC),
    )
    span = parse_events("2026-11-05T16:00:00+00:00..2026-11-05T19:00:00+00:00")
    assert span.matches(1, datetime(2026, 11, 5, 18, 59, tzinfo=UTC))
    assert not span.matches(1, datetime(2026, 11, 5, 19, 0, tzinfo=UTC))
    with pytest.raises(ValueError):
        parse_events(",")
    assert EventSelection().matches(5, NOW)


def test_the_type_is_the_one_most_mentions_gave():
    assert vote_type(["place", "organization", "place"]) == "place"
    assert vote_type(["place", "organization"]) == "organization"  # tie: alphabetical


def saved_event(number, entities, links, relations=(), title="Title"):
    extraction = Extraction(
        title,
        "Description.",
        entities=[ExtractedEntity(name, type_, PAGE.title, 111, 0.9) for name, type_ in entities],
        relations=[ExtractedRelation(s, r, o, PAGE.title, 111, 0.6) for s, r, o in relations],
        model="ollama:qwen3:4b",
        prompt_version="extract_v2",
    )
    source = event((PAGE,), run_id="03b-test", number=number)
    return build_graph_event(source, [PAGE], extraction, links, NOW)


def test_a_saved_extraction_reads_back_like_the_graph(tmp_path):
    path = tmp_path / "v2.jsonl"
    bangladesh = Link("Q902", "link", "Bangladesh")
    append_event(
        path,
        saved_event(
            1,
            [("Bangladesh", "place"), ("Bhutan national team", "organization")],
            {"Bangladesh": bangladesh, "Bhutan national team": UNLINKED},
            [("Bangladesh", "played", "Bhutan national team")],
        ),
    )
    append_event(
        path,
        saved_event(2, [("Bangladesh", "organization")], {"Bangladesh": bangladesh}),
    )
    append_event(  # event 2 again, later: replaces the first line of event 2
        path,
        saved_event(
            2,
            [("People's Republic of Bangladesh", "place")],
            {"People's Republic of Bangladesh": bangladesh},
        ),
    )
    assert len(read_records(path)) == 2
    data = load_from_file(path, "03b-test", EventSelection())
    assert [e.number for e in data.events] == [1, 2]
    country = data.entities["Q902"]
    assert country.aliases == ("Bangladesh", "People's Republic of Bangladesh")
    assert country.type == "place"
    assert data.entities["name:bhutan national team"].wikidata_id is None
    (relation,) = data.relations
    assert (relation.subject, relation.relation, relation.object) == (
        "Q902",
        "played",
        "name:bhutan national team",
    )
    assert relation.source_revision == 111
    assert data.models == ["ollama:qwen3:4b"] and data.prompt_versions == ["extract_v2"]
    # The selection and the grouping run filter the saved events.
    assert [e.number for e in load_from_file(path, "03b-test", parse_events("2")).events] == [2]
    assert load_from_file(path, "other-run", EventSelection()).events == []
