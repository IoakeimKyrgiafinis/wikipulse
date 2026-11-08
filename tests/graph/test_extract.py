"""Extraction with a mocked LLM: the schema, the message, and parsing the answer."""

import json

import pytest

from tests.graph.helpers import answer, entity, event, relation, saff_page
from tests.llm.helpers import ScriptedClient
from wikipulse.graph.extract import (
    PROMPT_VERSION,
    Extractor,
    build_message,
    extraction_schema,
    load_prompt,
    parse_extraction,
)
from wikipulse.llm import StructuredOutputError
from wikipulse.llm.schema import schema_errors

PAGES = (
    saff_page(revision=111, changes="Bangladesh beat Bhutan 1–0."),
    saff_page(title="Bangladesh national football team", revision=222),
)


def test_the_prompt_file_exists_and_names_the_entity_types() -> None:
    prompt = load_prompt()
    for word in ("person", "organization", "place", "event", "other", "SOURCE"):
        assert word in prompt


def test_the_schema_allows_only_existing_sources_and_known_types() -> None:
    schema = extraction_schema(sources=2)
    good = json.loads(answer([entity("Bhutan", "place", source=2)]))
    assert schema_errors(good, schema) == []
    for bad in (
        entity("Bhutan", "place", source=3),  # there is no SOURCE 3
        entity("Bhutan", "country"),  # not an entity type
        entity("Bhutan", certainty="pretty sure"),
    ):
        assert schema_errors(json.loads(answer([bad])), schema)


def test_the_message_numbers_the_sources_with_summary_and_changes() -> None:
    message = build_message(event(PAGES), PAGES, max_chars=1000)
    assert "2026-11-05 16:00 UTC" in message
    assert "SOURCE 1: 2026 SAFF Championship" in message
    assert "SOURCE 2: Bangladesh national football team" in message
    assert "Bangladesh beat Bhutan 1–0." in message
    assert "(none available)" in message  # page 2 has no changes text


def test_parsing_resolves_sources_and_certainty_to_provenance() -> None:
    data = json.loads(
        answer(
            [entity("Bhutan", "place", source=2, certainty="medium")],
            [relation("Bangladesh", "played against", "Bhutan", certainty="low")],
        )
    )
    result = parse_extraction(data, PAGES)
    bhutan = next(e for e in result.entities if e.name == "Bhutan")
    assert (bhutan.source_title, bhutan.source_revision) == (
        "Bangladesh national football team",
        222,
    )
    assert bhutan.confidence == 0.6
    (fact,) = result.relations
    assert (fact.subject, fact.relation, fact.object) == ("Bangladesh", "played against", "Bhutan")
    assert (fact.source_revision, fact.confidence) == (111, 0.3)


def test_parsing_merges_duplicates_and_adds_entities_only_named_in_relations() -> None:
    data = json.loads(
        answer(
            [
                entity("Bangladesh national football team"),
                entity(" the Bangladesh national football team ", "event", certainty="low"),
                entity('"Bhutan"', "place"),
            ],
            [
                relation("Bangladesh national football team", "Played Against ", "Bhutan"),
                relation("Bangladesh national football team", "played against", "bhutan"),
                relation("Jamal Bhuyan", "captain of", "Bangladesh national football team"),
                relation("Bhutan", "same as", "Bhutan"),
            ],
        )
    )
    result = parse_extraction(data, PAGES)
    names = [(e.name, e.type) for e in result.entities]
    assert names == [
        ("Bangladesh national football team", "organization"),  # the first type is kept
        ("Bhutan", "place"),
        ("Jamal Bhuyan", "other"),  # only named in a relation
    ]
    assert [(r.subject, r.relation, r.object) for r in result.relations] == [
        ("Bangladesh national football team", "played against", "Bhutan"),
        ("Jamal Bhuyan", "captain of", "Bangladesh national football team"),
    ]


def test_parsing_drops_generic_nouns_and_dates_with_their_relations() -> None:
    data = json.loads(
        answer(
            [entity("a teacher", "person"), entity("October 15, 2026", "event"), entity("Bend")],
            [relation("Griffin John", "shot", "a teacher")],
        )
    )
    result = parse_extraction(data, PAGES)
    assert [e.name for e in result.entities] == ["Bend", "Griffin John"]
    assert result.relations == []


def test_extractor_asks_with_the_prompt_and_schema_and_records_the_model() -> None:
    llm = ScriptedClient([answer([entity("Bhutan", "place")])])
    result = Extractor(llm).extract(event(PAGES), PAGES)
    (request,) = llm.requests
    assert request.system == load_prompt()
    assert request.json_schema == extraction_schema(2)
    assert request.temperature == 0.0
    assert result.title == "Bangladesh beat Bhutan at the SAFF Championship"
    assert [e.name for e in result.entities] == ["Bhutan"]
    assert (result.model, result.prompt_version) == ("fake:m", PROMPT_VERSION)
    assert result.usage.output_tokens == 5


def test_an_invalid_answer_is_retried_once_then_raises() -> None:
    bad = answer([entity("Bhutan", "country")])
    llm = ScriptedClient([bad, answer([entity("Bhutan", "place")])])
    assert Extractor(llm).extract(event(PAGES), PAGES).entities[0].type == "place"
    with pytest.raises(StructuredOutputError):
        Extractor(ScriptedClient([bad, "not json"])).extract(event(PAGES), PAGES)
