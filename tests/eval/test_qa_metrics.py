"""QA evaluation metrics: recall, aggregates, failure split, judge agreement."""

from datetime import UTC, datetime

import pytest

from wikipulse.eval.qa_judge import ClaimVerdict, QAJudgement
from wikipulse.eval.qa_metrics import (
    AnswerRecord,
    FactView,
    QuestionScore,
    claim_agreement,
    cohen_kappa,
    correctness_agreement,
    entity_hits,
    event_hits,
    failure_class,
    failure_split,
    name_in,
    percentile,
    score_question,
    summarize,
    summarize_by_type,
)
from wikipulse.eval.qa_questions import QAQuestion

NOW = datetime(2026, 11, 8, tzinfo=UTC)


def record(facts: list[str], **changes) -> AnswerRecord:
    values = {
        "run": "r1",
        "question_id": "q01",
        "question": "Q?",
        "type": "event",
        "answered": True,
        "facts": [
            FactView(id=f"F{n}", kind="relation", channel="graph", text=text)
            for n, text in enumerate(facts, 1)
        ],
    }
    return AnswerRecord(**{**values, **changes})


def score(**changes) -> QuestionScore:
    values = {"id": "q01", "type": "event", "question": "Q?", "answerable": True, "answered": True}
    return QuestionScore(**{**values, **changes})


@pytest.mark.parametrize(
    ("text", "name", "found"),
    [
        ("Santiago Pena resigned", "Santiago Peña", True),  # accents ignored
        ("SANTIAGO PEÑA resigned", "santiago peña", True),  # case ignored
        ("Peñarol won", "Peña", False),  # whole words only
        ("tell us more", "US", False),  # short names keep their case
        ("The US Senate", "US", True),
    ],
)
def test_name_in(text: str, name: str, found: bool) -> None:
    assert name_in(text, name) is found


def test_entity_hits_use_names_and_aliases() -> None:
    names = {"Q1": ["Santiago Peña", "Peña"], "Q2": ["Ueno Bank"], "Q3": ["Visión Banco"]}
    answer = record(["Peña —resigned as→ President", "Ueno Bank collapsed"])
    assert entity_hits(["Q1", "Q2", "Q3", "Q404"], names, answer) == [True, True, False, False]


def test_entity_hits_count_entities_the_question_named() -> None:
    answer = record([], entities=["Ueno Bank"])
    assert entity_hits(["Q2"], {"Q2": ["Ueno Bank"]}, answer) == [True]


def test_event_hits_by_key_seed_or_title() -> None:
    titles = {"r:1": "Peña resigns", "r:2": "Bank collapses", "r:3": "Election held", "r:4": "X"}
    answer = record(
        ['Event "Peña resigns" (r:1, started ...)', "Query result: title = Bank collapses"],
        events=["r:3"],
    )
    assert event_hits(["r:1", "r:2", "r:3", "r:4"], titles, answer) == [True, True, True, False]


def test_score_question_uses_the_judgements() -> None:
    question = QAQuestion(
        id="q01",
        question="Who?",
        type="event",
        expected_answer="Peña",
        expected_entities=["Q1", "Q2"],
        expected_events=["r:1"],
        answerable=True,
        source="synthetic",
        labeler="llm",
    )
    correctness = QAJudgement(
        run="r1",
        question_id="q01",
        answer_hash="h",
        metric="correctness",
        score=1,
        labeler="llm",
        model="m",
        reason="partly",
        labeled_at=NOW,
    )
    faithfulness = correctness.model_copy(
        update={
            "metric": "faithfulness",
            "score": 0.5,
            "claims": [
                ClaimVerdict(claim="a", supported=True),
                ClaimVerdict(claim="b", supported=False),
                ClaimVerdict(claim="c", supported=None),
            ],
        }
    )
    answer = record(["Peña is involved in r:1"], seconds={"total": 3.5})
    result = score_question(
        question, answer, {"Q1": ["Peña"], "Q2": ["Ueno Bank"]}, {}, correctness, faithfulness
    )
    assert result.entity_recall == 0.5 and result.event_recall == 1.0
    assert result.retrieval_recall == pytest.approx(2 / 3)
    assert result.correctness == 1 and result.correctness_reason == "partly"
    assert (result.claims_supported, result.claims_judged) == (1, 2)
    assert result.seconds == 3.5


def test_summarize_refusals_correctness_faithfulness_latency() -> None:
    scores = [
        score(
            id="a",
            correctness=2,
            claims_supported=3,
            claims_judged=4,
            seconds=2.0,
            entity_hits=[True, False],
        ),
        score(id="b", correctness=0, answered=False, seconds=4.0, entity_hits=[True]),
        score(
            id="c",
            type="unanswerable",
            answerable=False,
            answered=False,
            correctness=2,
            seconds=1.0,
        ),
        score(
            id="d",
            type="unanswerable",
            answerable=False,
            answered=True,
            correctness=0,
            claims_supported=1,
            claims_judged=1,
            seconds=10.0,
        ),
    ]
    m = summarize(scores)
    assert m["questions"] == 4
    assert m["entity_recall"] == pytest.approx(0.75)  # mean of 0.5 and 1.0
    assert m["entity_recall_n"] == 2 and m["event_recall"] is None
    assert m["correctness"] == 1.0 and m["fully_correct"] == 0.5
    assert m["faithfulness"] == pytest.approx(4 / 5)  # over claims, not answers
    assert m["correct_refusals"] == 0.5 and m["false_refusals"] == 0.5
    assert m["latency_median_s"] == 3.0 and m["latency_max_s"] == 10.0
    by_type = summarize_by_type(scores)
    assert list(by_type) == ["event", "unanswerable"]
    assert by_type["event"]["correct_refusals"] is None


def test_percentile_nearest_rank() -> None:
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == 9
    assert percentile([5.0], 90) == 5.0
    assert percentile([], 90) is None


def test_failure_split_separates_retrieval_from_generation() -> None:
    scores = [
        score(id="ok", correctness=2, entity_hits=[False]),
        score(id="gen", correctness=1, entity_hits=[True, True], event_hits=[True]),
        score(id="ret", correctness=0, entity_hits=[True, False]),
        score(id="count", correctness=0),  # no expected entities or events
        score(id="todo"),
        score(id="refusal", answerable=False, answered=False, correctness=2),
    ]
    assert failure_class(scores[1]) == "wrong, retrieval complete (generation failure)"
    assert failure_split(scores) == {
        "correct": ["ok"],
        "wrong, retrieval complete (generation failure)": ["gen"],
        "wrong, retrieval incomplete (retrieval failure)": ["ret"],
        "wrong, retrieval not measurable": ["count"],
        "not judged": ["todo"],
    }


def test_cohen_kappa_known_values() -> None:
    a = [1, 1, 0, 0, 1, 0, 1, 1, 0, 1]
    assert cohen_kappa(a, a, [0, 1]) == 1.0
    # 70% agreement; by chance 0.6 * 0.5 + 0.4 * 0.5 = 50% -> (0.7 - 0.5) / 0.5 = 0.4
    b = [1, 0, 0, 1, 1, 0, 1, 0, 0, 1]
    assert cohen_kappa(a, b, [0, 1]) == pytest.approx(0.4)
    assert cohen_kappa([0, 1], [1, 0], [0, 1]) == pytest.approx(-1.0)
    assert cohen_kappa([], [], [0, 1]) is None


def test_weighted_kappa_counts_near_misses_less() -> None:
    a = [0, 1, 2, 2, 1, 0]
    near = [0, 2, 2, 1, 1, 0]
    far = [2, 1, 2, 0, 1, 0]
    assert cohen_kappa(a, near, [0, 1, 2], weighted=True) > cohen_kappa(
        a, far, [0, 1, 2], weighted=True
    )


def test_correctness_agreement_on_common_questions() -> None:
    a = {"q1": 2, "q2": 1, "q3": 0, "q4": 2}
    b = {"q1": 2, "q2": 2, "q3": 2, "q5": 0}
    result = correctness_agreement(a, b)
    assert result["items"] == 3
    assert result["exact"] == pytest.approx(1 / 3)
    assert result["within_one"] == pytest.approx(2 / 3)
    assert result["matrix"] == [[0, 0, 1], [0, 0, 1], [0, 0, 1]]
    assert result["disagreements"] == ["q2", "q3"]


def test_claim_agreement_skips_unjudged_claims() -> None:
    a = {"q1": [True, True, False], "q2": [True, None]}
    b = {"q1": [True, False, False], "q2": [True, True]}
    result = claim_agreement(a, b)
    assert result["claims"] == 4
    assert result["agreement"] == 0.75
    assert result["supported_a"] == 0.75 and result["supported_b"] == 0.5
    assert result["mean_answer_gap"] == pytest.approx((1 / 3 + 0) / 2)
