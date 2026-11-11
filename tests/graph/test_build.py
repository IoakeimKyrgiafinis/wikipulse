"""Turning an extraction and its links into graph records (no database)."""

from datetime import UTC, datetime

from tests.graph.helpers import event, saff_page
from wikipulse.graph.extract import ExtractedEntity, ExtractedRelation, Extraction
from wikipulse.graph.link import UNLINKED, Link
from wikipulse.graph.names import is_proper_name, normalize_name
from wikipulse.graph.store import build_graph_event, entity_key

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
PAGE = saff_page(revision=111)


def mention(name: str, type_: str = "person", confidence: float = 0.9) -> ExtractedEntity:
    return ExtractedEntity(name, type_, PAGE.title, 111, confidence)


def fact(subject: str, rel: str, object_: str) -> ExtractedRelation:
    return ExtractedRelation(subject, rel, object_, PAGE.title, 111, 0.9)


def test_names_compare_without_case_underscores_or_a_leading_the() -> None:
    assert normalize_name("The_Beatles") == normalize_name("the beatles") == "beatles"
    assert normalize_name("  Santiago   Peña ") == "santiago peña"
    for name in ("Bhutan", "2026 SAFF Championship", "the Senate", '"Ueno Bank"'):
        assert is_proper_name(name)
    for not_a_name in (
        "the police",
        "a teacher",
        "by June 2026",
        "October 15, 2026",
        "5 November 2026",
        "2026",
        "May 2026",
        "149",
        "20 years",
    ):
        assert not is_proper_name(not_a_name)


def test_linked_entities_are_keyed_by_wikidata_id_and_others_by_name() -> None:
    assert entity_key("Donald Trump", Link("Q22686", "search", "Donald Trump")) == "Q22686"
    assert entity_key("Kaylub  Giamanco", UNLINKED) == "name:kaylub giamanco"


def test_two_names_for_one_wikidata_item_become_one_entity() -> None:
    extraction = Extraction(
        "Title",
        "Description.",
        entities=[mention("Donald Trump", confidence=0.6), mention("Trump", confidence=0.9)],
        relations=[fact("Trump", "same person as", "Donald Trump"), fact("Trump", "won", "X")],
        model="fake:m",
    )
    links = {
        "Donald Trump": Link("Q22686", "link", "Donald Trump"),
        "Trump": Link("Q22686", "search", "Donald Trump"),
    }
    graph = build_graph_event(event((PAGE,)), [PAGE], extraction, links, NOW)
    (trump,) = graph.entities
    assert (trump.key, trump.name, trump.link_method) == ("Q22686", "Donald Trump", "link")
    assert trump.aliases == ["Donald Trump", "Trump"]
    assert trump.confidence == 0.9  # the most confident mention
    assert graph.relations == []  # a self-relation, and one to an unknown entity


def test_the_event_node_gets_the_llm_title_and_provenance() -> None:
    extraction = Extraction(
        "Bangladesh beat Bhutan",
        "Bangladesh beat Bhutan 1-0.",
        entities=[mention("Mohammad Ibrahim"), mention("Bhutan", "place")],
        relations=[fact("Mohammad Ibrahim", "plays for", "Bhutan")],
        model="ollama:qwen3:4b",
    )
    links = {"Mohammad Ibrahim": Link("Q60734382", "page", "Mohammad Ibrahim (footballer)")}
    graph = build_graph_event(
        event((PAGE,), correlation_id="edit-7"), [PAGE], extraction, links, NOW
    )
    assert graph.key == "test-run:7"
    assert graph.props["correlation_id"] == "edit-7"
    assert graph.props["title"] == "Bangladesh beat Bhutan"
    assert graph.props["provisional_title"] == "2026 SAFF Championship"
    assert graph.props["description"] == "Bangladesh beat Bhutan 1-0."
    assert [e.name for e in graph.entities] == ["Mohammad Ibrahim", "Bhutan"]  # no qualifier
    assert graph.entities[1].key == "name:bhutan" and graph.entities[1].link_method == "none"
    (relation,) = graph.relations
    assert (relation.subject, relation.object) == ("Q60734382", "name:bhutan")
    assert graph.provenance == {
        "extracted_at": NOW,
        "model": "ollama:qwen3:4b",
        "prompt_version": "extract_v1",
    }
    assert graph.pages[0]["revision"] == 111
