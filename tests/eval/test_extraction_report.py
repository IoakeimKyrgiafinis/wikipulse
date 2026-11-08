"""Extraction evaluation: linking checks, Wikidata support, labeled precision, the report."""

from datetime import UTC, datetime

from tests.eval.extraction_helpers import (
    ALLIANA,
    GEORGIA,
    PARAGUAY,
    PENA,
    PRESIDENT,
    UENO,
    data,
    ent,
    rel,
)
from wikipulse.eval.extraction_report import (
    build_report,
    check_links,
    check_support,
    compare_reports,
    count_labels,
    count_links,
    link_error_ids,
    linked_pairs,
    render_markdown,
    summary_line,
    support_summary,
)
from wikipulse.eval.relation_labels import RelationLabelRecord
from wikipulse.eval.wikidata import Statement

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
GIT = {"commit": "abc", "dirty": False}

# What Wikidata says (items_by_label): name -> items carrying it as a label.
LABELS = {
    "Santiago Peña": ("Q22695687",),
    "Pedro Alliana": ("Q51885479",),
    "Paraguay": ("Q733", "Q1155498"),  # the country, and a ship
    "President of Paraguay": ("Q34071",),
    "Georgia": ("Q230", "Q1428", "Q16614"),
    "Peña": (),
}


def test_a_linked_name_one_item_carries_must_link_to_that_item():
    wrong = ent("Q999", "President of Paraguay", "other", "search")  # not Q34071
    checks = {c.name: c for c in check_links(data([PENA, wrong], []), LABELS)}
    assert checks["Santiago Peña"].outcome == "correct"
    assert not checks["Santiago Peña"].ambiguous
    assert checks["President of Paraguay"].outcome == "wrong"


def test_names_several_items_carry_only_check_that_the_link_is_one_of_them():
    other_georgia = ent("Q42", "Georgia", "place", "search")
    counts = count_links(check_links(data([PARAGUAY, GEORGIA, other_georgia], []), LABELS))
    assert (counts.shared_candidate, counts.shared_other) == (2, 1)
    assert counts.accuracy is None  # nothing with a single answer
    assert counts.shared_rate == 2 / 3


def test_unlinked_names_and_names_without_a_label_are_counted_apart():
    unlinked = ent("name:pedro alliana", "Pedro Alliana")
    no_label = ent("Q22695687", "Santiago Peña", aliases=("Santiago Peña", "Peña"))
    counts = count_links(check_links(data([unlinked, no_label, UENO], []), LABELS))
    assert counts.names == 4  # every name the model used counts once
    assert counts.unique_unlinked == 1
    assert counts.no_label == 2  # "Peña" and "Ueno Bank"
    assert counts.unique_correct == 1
    assert counts.accuracy == 1.0


def test_relation_support_needs_both_entities_linked_and_a_statement_either_way():
    relations = [
        rel(PENA.key, "was president of", PARAGUAY.key),
        rel(ALLIANA.key, "became", PRESIDENT.key),
        rel(UENO.key, "located in", PARAGUAY.key),  # Ueno Bank is unlinked
    ]
    extraction = data([PENA, ALLIANA, PARAGUAY, PRESIDENT, UENO], relations)
    assert linked_pairs(extraction) == [("Q22695687", "Q733"), ("Q51885479", "Q34071")]
    # Wikidata states it from the object's side: Paraguay's head of state is Peña.
    statements = {
        frozenset(("Q733", "Q22695687")): [Statement("Q733", "P35", "head of state", "Q22695687")]
    }
    checks = check_support(extraction, statements)
    assert [c.supported for c in checks] == [True, False, False]
    assert [c.both_linked for c in checks] == [True, True, False]
    summary = support_summary(checks)
    assert (summary["both_linked"], summary["supported"]) == (2, 1)
    assert summary["support_rate"] == 0.5
    assert summary["properties"] == {"P35 head of state": 1}


def label(relation, value, labeler="llm", model="ollama:qwen3:8b") -> RelationLabelRecord:
    event_key, subject, relation_text, object_, revision = relation.key
    return RelationLabelRecord(
        event_key=event_key,
        subject_key=subject,
        subject=subject,
        relation=relation_text,
        object_key=object_,
        object=object_,
        source_title="Source page",
        source_revision=revision,
        label=value,
        labeler=labeler,
        model=model if labeler == "llm" else None,
        prompt_version=2 if labeler == "llm" else None,
        labeled_at=NOW,
    )


def test_labeled_precision_ignores_unsure_and_names_the_labelers():
    sample = [rel(PENA.key, f"r{i}", PARAGUAY.key) for i in range(5)]
    labels = {
        sample[0].key: label(sample[0], "correct"),
        sample[1].key: label(sample[1], "correct", "human"),
        sample[2].key: label(sample[2], "wrong"),
        sample[3].key: label(sample[3], "unsure"),
    }
    counts = count_labels(sample, labels)
    assert (counts.correct, counts.wrong, counts.unsure, counts.unlabeled) == (2, 1, 1, 1)
    assert counts.precision == 2 / 3
    assert counts.labelers == {"llm (ollama:qwen3:8b, judge prompt v2)": 3, "human": 1}


def make_report(labels=None):
    relations = [
        rel(PENA.key, "was president of", PARAGUAY.key),
        rel(UENO.key, "located in", PARAGUAY.key),
    ]
    extraction = data([PENA, PARAGUAY, UENO], relations)
    statements = {frozenset(("Q733", "Q22695687")): [Statement("Q733", "P35", "head", "Q22695687")]}
    labels = labels if labels is not None else {relations[1].key: label(relations[1], "wrong")}
    config = {"extraction": {"models": extraction.models, "prompt_versions": ["extract_v1"]}}
    return build_report(
        "test",
        extraction,
        "events 1",
        check_links(extraction, LABELS),
        check_support(extraction, statements),
        relations,
        labels,
        config,
        NOW.isoformat(),
        GIT,
    )


def test_the_report_breaks_everything_down_by_entity_type():
    report = make_report()
    person, organization = report["by_type"]["person"], report["by_type"]["organization"]
    # Relations count under their subject's type.
    assert person["support"]["supported"] == 1
    assert person["linking"]["accuracy"] == 1.0
    assert organization["linked"] == 0
    assert organization["labels"]["wrong"] == 1
    assert report["labels"]["precision"] == 0.0
    assert report["overview"]["relations_both_linked"] == 1


def test_the_markdown_shows_the_numbers_and_the_extraction_config():
    report = make_report()
    text = render_markdown(report)
    assert "| Linking accuracy (names exactly one Wikidata item carries) | 100.0%" in text
    assert "Relation support in Wikidata (lower bound) | 100.0%" in text
    assert "labeled by llm (ollama:qwen3:8b, judge prompt v2): 1" in text
    assert '"extract_v1"' in text and '"ollama:qwen3:4b"' in text
    assert "| person |" in text and "| organization |" in text
    assert "precision 0.0%" in summary_line(report)


def test_compare_lines_up_the_same_measures_of_two_reports():
    a = make_report()
    b = make_report(labels={})
    rows = {name: (x, y) for name, x, y in compare_reports(a, b)}
    assert rows["Labeled precision"] == ("0.0%", "n/a")
    assert rows["Wikidata support"] == ("100.0%", "100.0%")


def test_linking_disagreements_are_listed_with_what_the_items_are():
    wrong = ent("Q999", "President of Paraguay", "other", "search")
    missed = ent("name:pedro alliana", "Pedro Alliana")
    extraction = data([wrong, missed, PENA], [])
    checks = check_links(extraction, LABELS)
    assert link_error_ids(checks) == ["Q34071", "Q51885479", "Q999"]
    descriptions = {"Q999": "Some item: a painting", "Q34071": "President of Paraguay: office"}
    report = build_report(
        "t", extraction, "events 1", checks, [], [], {}, {}, NOW.isoformat(), GIT, "", descriptions
    )
    (error,) = report["link_errors"]
    assert (error["linked"], error["label_item_text"]) == (
        "Some item: a painting",
        "President of Paraguay: office",
    )
    (miss,) = report["missed_links"]
    assert (miss["name"], miss["linked_id"], miss["label_item"]) == (
        "Pedro Alliana",
        None,
        "Q51885479",
    )
    text = render_markdown(report)
    assert "linked Q999 (Some item: a painting), label item Q34071" in text
    assert "*Pedro Alliana* (person, none): label item Q51885479" in text
