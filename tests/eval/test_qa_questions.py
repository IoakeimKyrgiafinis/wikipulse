"""The QA question set: helpers, merging with human questions, checks of the generator."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wikipulse.eval.qa_questions import (
    QAQuestion,
    first_new,
    fold,
    found_terms,
    load_questions,
    mentions,
    merge_questions,
    name_list,
    period_question,
    write_questions,
)


def question(id_: str, source: str = "synthetic", labeler: str = "llm") -> QAQuestion:
    return QAQuestion(
        id=id_,
        question=f"Question {id_}?",
        type="time",
        expected_answer="Something.",
        answerable=True,
        source=source,
        labeler=labeler,
    )


def test_fold_and_mentions_ignore_case_and_accents() -> None:
    assert fold("Santiago PEÑA") == "santiago pena"
    assert mentions("The Federal Reserve met", "federal reserve")
    assert not mentions("Applegate County", "Apple")
    assert mentions("Apple's new phone", "Apple")


def test_name_list() -> None:
    assert name_list([]) == ""
    assert name_list(["A"]) == "A"
    assert name_list(["A", "B", "C"]) == "A, B and C"


def test_human_questions_are_never_replaced(tmp_path: Path) -> None:
    path = tmp_path / "qa_questions.jsonl"
    mine = question("q02", source="human", labeler="human")
    write_questions(path, [question("q01"), mine])
    merged = merge_questions(load_questions(path), [question("q01"), question("q02")])
    assert [(q.id, q.source) for q in merged] == [("q02", "human"), ("q01", "synthetic")]
    assert merged[0] == mine


def test_questions_round_trip_with_lf_endings(tmp_path: Path) -> None:
    path = tmp_path / "q.jsonl"
    write_questions(path, [question("q01"), question("q02")])
    assert b"\r\n" not in path.read_bytes()
    assert [q.id for q in load_questions(path)] == ["q01", "q02"]
    assert load_questions(tmp_path / "missing.jsonl") == []


def test_found_terms_respects_the_anchor() -> None:
    texts = ["Ueno Bank | a Paraguayan bank", "Acme Corp | its CEO is Jane", "Apple pie recipe"]
    assert found_terms(texts, ["CEO"], anchor="Ueno") == []
    assert found_terms(texts, ["CEO"], anchor=None) == ["CEO"]
    assert found_terms(texts, ["Apple", "Federal Reserve"], anchor=None) == ["Apple"]


def test_first_new_skips_used_events_and_entities() -> None:
    rows = [
        {"a_id": "Q1", "b_id": "Q2", "event_key": "r:1"},
        {"a_id": "Q3", "b_id": "Q4", "event_key": "r:1"},
        {"a_id": "Q1", "b_id": "Q5", "event_key": "r:2"},
        {"a_id": "Q6", "b_id": "Q7", "event_key": "r:3"},
    ]
    used_events: set[str] = set()
    used_entities: set[str] = set()
    assert first_new(rows, used_events, used_entities) == rows[0]
    assert first_new(rows, used_events, used_entities) == rows[3]
    assert first_new(rows, used_events, used_entities) is None


class FakeSnapshot:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.params: dict[str, Any] = {}

    def rows(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        self.params = params
        return self._rows


def test_period_question_expects_the_five_biggest_events() -> None:
    start = datetime(2026, 11, 5, tzinfo=UTC)
    rows = [
        {"key": f"r:{n}", "title": f"Story {n}", "pages": 7 - n, "started_at": start}
        for n in range(1, 8)
    ]
    snapshot = FakeSnapshot(rows)
    result = period_question(
        snapshot, "What happened today?", start, start + timedelta(days=1), "on 2026-11-05"
    )
    assert snapshot.params == {"start": start, "end": start + timedelta(days=1)}
    assert result.type == "time" and result.answerable
    assert result.expected_events == ["r:1", "r:2", "r:3", "r:4", "r:5"]
    assert result.expected_answer.startswith("7 events started on 2026-11-05.")
    assert '"Story 6" (1 page, 00:00 UTC)' not in result.expected_answer  # not in the top 5
    assert '"Story 5" (2 pages, 00:00 UTC)' in result.expected_answer
    assert result.source == "synthetic" and result.labeler == "llm"


def test_period_question_without_events() -> None:
    start = datetime(2026, 11, 4, tzinfo=UTC)
    result = period_question(FakeSnapshot([]), "Q?", start, start, "on 2026-11-04")
    assert result.expected_answer == "No event in the graph started on 2026-11-04."
    assert result.expected_events == []


@pytest.mark.parametrize("field", ["source", "labeler"])
def test_records_must_name_source_and_labeler(field: str) -> None:
    values = question("q01").model_dump()
    values[field] = "machine"
    with pytest.raises(ValueError):
        QAQuestion(**values)
