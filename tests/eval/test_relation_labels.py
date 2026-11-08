"""Relation labels: the sample, the labels file, the LLM judge and manual labeling."""

import json
import random
from datetime import UTC, datetime

from tests.eval.extraction_helpers import PARAGUAY, PENA, rel
from tests.llm.helpers import ScriptedClient
from wikipulse.eval.relation_labels import (
    JUDGE_PROMPT_VERSION,
    LLMRelationJudge,
    RelationToLabel,
    SourceText,
    judge_message,
    label_manually,
    label_with_llm,
    labels_by_relation,
    load_relation_records,
    make_record,
    sample_relations,
)

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
SOURCE = SourceText(
    "Santiago Peña",
    "President of Paraguay",
    "Santiago Peña is a Paraguayan politician who was president of Paraguay.",
    "On 5 November 2026 Peña resigned after the collapse of Ueno Bank.",
)


def item(relation_text: str = "was president of", revision: int = 100) -> RelationToLabel:
    relation = rel(PENA.key, relation_text, PARAGUAY.key, revision=revision)
    return RelationToLabel(relation, "Santiago Peña", "Paraguay", "person", "place", SOURCE)


def now() -> datetime:
    return NOW


def test_the_sample_is_fixed_and_does_not_depend_on_the_order_of_relations():
    relations = [rel(PENA.key, f"relation {i}", PARAGUAY.key) for i in range(80)]
    first = sample_relations(relations, 50, seed=2026)
    shuffled = relations[:]
    random.Random(1).shuffle(shuffled)
    assert len(first) == 50
    assert sample_relations(shuffled, 50, seed=2026) == first
    assert sample_relations(relations, 50, seed=7) != first
    # A larger extraction keeps the relations both share in the same order.
    more = relations + [rel(PENA.key, f"other {i}", PARAGUAY.key) for i in range(20)]
    kept = [r for r in sample_relations(more, 100) if r in relations]
    assert kept == sample_relations(relations, 80)


def test_the_judge_sees_the_source_text_and_the_claim():
    message = judge_message(item())
    assert "SOURCE: Santiago Peña" in message
    assert "resigned after the collapse of Ueno Bank" in message
    assert 'CLAIM: "Santiago Peña" was president of "Paraguay"' in message
    no_source = RelationToLabel(item().relation, "A", "B", "person", "place", None)
    assert "could not be read" in judge_message(no_source)


def test_llm_labels_record_the_labeler_model_and_prompt_and_are_not_repeated(tmp_path):
    path = tmp_path / "relation_labels.jsonl"
    answers = [
        json.dumps({"label": "correct", "reason": "The summary says so."}),
        json.dumps({"label": "wrong", "reason": "Not in the source."}),
    ]
    judge = LLMRelationJudge(ScriptedClient(answers, model="judge"))
    items = [item(), item("born in")]
    assert label_with_llm(items, judge, "fake:judge", "04c-test", path, now) == 2
    records = load_relation_records(path)
    assert [r.label for r in records] == ["correct", "wrong"]
    assert {(r.labeler, r.model, r.prompt_version) for r in records} == {
        ("llm", "fake:judge", JUDGE_PROMPT_VERSION)
    }
    assert records[0].sampled_for == "04c-test"
    # Same model, same prompt: nothing to do (the scripted client has no answers left).
    assert label_with_llm(items, judge, "fake:judge", "04c-test", path, now) == 0


def test_a_human_label_is_never_overwritten_and_beats_the_llm(tmp_path):
    path = tmp_path / "relation_labels.jsonl"
    judge = LLMRelationJudge(ScriptedClient([json.dumps({"label": "correct", "reason": "r"})]))
    label_with_llm([item()], judge, "fake:m", "04c-test", path, now)
    answers = iter(["w", "the direction is reversed"])
    shown: list[str] = []
    labeled, compared, agreed = label_manually(
        [item()], "04c-test", path, now, ask=lambda _: next(answers), show=shown.append
    )
    assert (labeled, compared, agreed) == (1, 1, 0)
    assert "The LLM said: correct" in "\n".join(shown)
    before = path.read_text(encoding="utf-8")
    # The LLM may label again (say, with a new model), but a human label stops it.
    other = LLMRelationJudge(ScriptedClient([]))
    assert label_with_llm([item()], other, "fake:other", "04c-test", path, now) == 0
    assert path.read_text(encoding="utf-8") == before
    label = labels_by_relation(load_relation_records(path))[item().key]
    assert (label.label, label.labeler, label.reason) == (
        "wrong",
        "human",
        "the direction is reversed",
    )


def test_manual_labeling_skips_what_a_human_labeled_and_can_stop(tmp_path):
    path = tmp_path / "relation_labels.jsonl"
    path.write_text(
        make_record(item(), "correct", "human", "earlier", NOW).model_dump_json() + "\n",
        encoding="utf-8",
    )
    answers = iter(["q"])
    labeled, _, _ = label_manually(
        [item(), item("born in")], "04c-test", path, now, ask=lambda _: next(answers), show=print
    )
    assert labeled == 0
    assert len(load_relation_records(path)) == 1


def test_the_same_fact_from_another_revision_is_another_label():
    assert item(revision=100).key != item(revision=101).key
