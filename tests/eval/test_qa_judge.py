"""The QA judge: messages, parsing its answers, and the judgements file (mocked LLM)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.llm.helpers import ScriptedClient
from wikipulse.eval.qa_judge import (
    CLAIMS_PER_CALL,
    ClaimVerdict,
    QAJudge,
    QAJudgement,
    answer_hash,
    append_judgement,
    correctness_message,
    faithfulness_message,
    faithfulness_score,
    find_judgement,
    load_judgements,
    parse_correctness,
    parse_faithfulness,
)
from wikipulse.eval.qa_questions import QAQuestion

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def question(answerable: bool = True) -> QAQuestion:
    return QAQuestion(
        id="q01",
        question="Who resigned?",
        type="event" if answerable else "unanswerable",
        expected_answer="Santiago Peña resigned." if answerable else "The graph has no facts.",
        answerable=answerable,
        source="synthetic",
        labeler="llm",
    )


def judgement(**changes) -> QAJudgement:
    values = {
        "run": "r1",
        "question_id": "q01",
        "answer_hash": "h1",
        "metric": "correctness",
        "score": 2,
        "labeler": "llm",
        "model": "ollama:judge",
        "prompt_version": "correctness_v1",
        "labeled_at": NOW,
    }
    return QAJudgement(**{**values, **changes})


def test_correctness_message_shows_reference_status_and_refusal() -> None:
    text = correctness_message(question(answerable=False), "No facts about it.", answered=False)
    assert "REFERENCE (not answerable): The graph has no facts." in text
    assert "ANSWER: (The system refused.) No facts about it." in text
    assert "answerable)" in correctness_message(question(), "Peña.", answered=True)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"reason": "ok", "score": 2}, (2, "ok")),
        ({"reason": " half ", "score": 1}, (1, "half")),
        ({"reason": "", "score": "0"}, (0, "")),
    ],
)
def test_parse_correctness(data, expected) -> None:
    assert parse_correctness(data) == expected


@pytest.mark.parametrize("data", [{"score": 3}, {"score": True}, {"reason": "x"}, ["2"], None])
def test_parse_correctness_rejects_invalid(data) -> None:
    with pytest.raises(ValueError):
        parse_correctness(data)


def test_parse_faithfulness_orders_and_marks_missing_claims() -> None:
    data = {
        "claims": [
            {"claim": 2, "reason": "not in F1", "supported": False},
            {"claim": 1, "reason": "F1 says so", "supported": True},
            {"claim": 1, "reason": "duplicate", "supported": False},  # the first counts
            {"claim": 9, "reason": "no such claim", "supported": True},
            {"claim": 3, "reason": "no verdict", "supported": "yes"},
        ]
    }
    verdicts = parse_faithfulness(data, ["A [F1].", "B [F2].", "C [F1]."])
    assert [v.supported for v in verdicts] == [True, False, None]
    assert verdicts[0].reason == "F1 says so"
    assert verdicts[2].claim == "C [F1]."


def test_parse_faithfulness_rejects_non_lists() -> None:
    with pytest.raises(ValueError):
        parse_faithfulness({"claims": "all fine"}, ["A"])


def test_faithfulness_score_ignores_unjudged_claims() -> None:
    verdicts = [
        ClaimVerdict(claim="a", supported=True),
        ClaimVerdict(claim="b", supported=False),
        ClaimVerdict(claim="c", supported=None),
    ]
    assert faithfulness_score(verdicts) == 0.5
    assert faithfulness_score([ClaimVerdict(claim="c", supported=None)]) is None


def test_faithfulness_message_numbers_claims_and_trims_facts() -> None:
    text = faithfulness_message("Q?", [("F1", "x" * 1000), ("F2", "short")], ["A [F1].", "B."])
    assert "[F1] " + "x" * 399 + "…" in text
    assert "[F2] short" in text
    assert "CLAIMS:\n1. A [F1].\n2. B." in text


def test_judge_asks_once_for_correctness_without_thinking() -> None:
    client = ScriptedClient([json.dumps({"reason": "matches", "score": 2})], model="judge")
    judge = QAJudge(client)
    assert judge.correctness(question(), "Santiago Peña resigned.", True) == (2, "matches")
    request = client.requests[0]
    assert request.think is False and request.temperature == 0
    assert "Scores:" in request.system
    assert judge.model == "fake:judge"


def test_judge_splits_long_answers_into_batches() -> None:
    claims = [f"Claim {n} [F1]." for n in range(CLAIMS_PER_CALL + 2)]
    first = {
        "claims": [
            {"claim": n, "reason": "", "supported": True} for n in range(1, CLAIMS_PER_CALL + 1)
        ]
    }
    second = {
        "claims": [
            {"claim": 1, "reason": "", "supported": False},
            {"claim": 2, "reason": "", "supported": True},
        ]
    }
    client = ScriptedClient([json.dumps(first), json.dumps(second)])
    verdicts = QAJudge(client).faithfulness("Q?", [("F1", "fact")], claims)
    assert len(client.requests) == 2
    assert [v.claim for v in verdicts] == claims
    assert [v.supported for v in verdicts][-2:] == [False, True]
    assert "1. Claim 10 [F1]." in client.requests[1].messages[0].content


def test_judgements_file_is_append_only(tmp_path: Path) -> None:
    path = tmp_path / "labels" / "qa_judgements.jsonl"
    append_judgement(path, judgement(labeler="human", model=None, prompt_version=None))
    append_judgement(path, judgement(score=1))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = load_judgements(path)
    assert [r.judge() for r in records] == ["human", "ollama:judge"]


def test_find_judgement_matches_answer_judge_and_prompt() -> None:
    records = [
        judgement(score=0),
        judgement(score=1),  # the later one wins
        judgement(answer_hash="other", score=2),
        judgement(prompt_version="correctness_v0", score=2),
        judgement(labeler="human", model=None, prompt_version=None, score=2),
    ]
    found = find_judgement(
        records, "r1", "q01", "h1", "correctness", "ollama:judge", "correctness_v1"
    )
    assert found is not None and found.score == 1
    human = find_judgement(records, "r1", "q01", "h1", "correctness", "human", "correctness_v1")
    assert human is not None and human.labeler == "human"
    assert find_judgement(records, "r1", "q02", "h1", "correctness", "ollama:judge") is None


def test_answer_hash_changes_with_text_and_refusal() -> None:
    assert answer_hash(True, "A [F1].") == answer_hash(True, "A [F1].")
    assert answer_hash(True, "A [F1].") != answer_hash(False, "A [F1].")
    assert answer_hash(True, "A [F1].") != answer_hash(True, "B [F1].")
