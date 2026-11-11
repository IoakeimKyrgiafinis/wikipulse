"""Scores of the QA evaluation, as pure functions (no Neo4j, no LLM).

- **Retrieval**: did the context hold what the answer needs? *Entity recall* is the
  share of a question's expected entities (Wikidata IDs) whose name or alias appears in
  the retrieved facts; *event recall* the same for expected events (by key or title).
- **Generation**: is the answer right (*correctness*, judge, 0 to 2), does it say only
  what the facts say (*faithfulness*, judge, share of supported claims), does it refuse
  exactly when it should (*correct refusals*, *false refusals*)?
- **Latency**: seconds per question, overall and per step.

Keeping retrieval and generation apart tells which half to fix: a wrong answer from a
context that held everything is a generation failure (`failure_split`).
"""

import math
import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from wikipulse.eval.qa_judge import QAJudgement
from wikipulse.eval.qa_questions import QUESTION_TYPES, QAQuestion, fold, mentions

RETRIEVAL_OK = 0.8  # recall at which retrieval counts as having found what was needed


class FactView(BaseModel):
    id: str
    kind: str
    channel: str
    text: str


class AnswerRecord(BaseModel):
    """What the system answered to one question, with the context it answered from."""

    run: str
    question_id: str
    question: str
    type: str
    answered: bool  # False: the system refused
    text: str = ""  # with citation numbers
    fact_text: str = ""  # with the fact ids the model cited
    citations: list[str] = Field(default_factory=list)
    facts: list[FactView] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)  # entities the question named
    events: list[str] = Field(default_factory=list)  # events retrieval looked at
    cypher: str | None = None
    cypher_rows: int | None = None
    notes: list[str] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)
    removed_sentences: list[str] = Field(default_factory=list)
    seconds: dict[str, float] = Field(default_factory=dict)
    model: str | None = None
    error: str | None = None  # the system failed (exception) on this question


# --- Retrieval ------------------------------------------------------------------------


def name_in(text: str, name: str) -> bool:
    """Whole-word match, ignoring case and accents; names of up to three characters
    ("US", "UN") must match case too, so "us" in a sentence doesn't count."""
    if len(name) <= 3:
        return re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text) is not None
    return mentions(text, name)


def retrieved_text(record: AnswerRecord) -> str:
    """All retrieved facts and entities of an answer as one text."""
    return "\n".join([*(f.text for f in record.facts), *record.entities])


def entity_hits(
    expected: Sequence[str], names_by_qid: dict[str, list[str]], record: AnswerRecord
) -> list[bool]:
    """For each expected Wikidata ID: does one of its names appear in the context?"""
    text = retrieved_text(record)
    return [any(name_in(text, name) for name in names_by_qid.get(qid, [])) for qid in expected]


def event_hits(
    expected: Sequence[str], titles_by_key: dict[str, str], record: AnswerRecord
) -> list[bool]:
    """For each expected event: was it looked at, or do the facts name its key or title?"""
    text = retrieved_text(record)
    folded = fold(text)
    hits = []
    for key in expected:
        title = titles_by_key.get(key)
        found = key in record.events or key in text or bool(title and fold(title) in folded)
        hits.append(found)
    return hits


def share(hits: Sequence[bool]) -> float | None:
    """The share of true values, or None for none."""
    return sum(hits) / len(hits) if hits else None


# --- One question's scores ------------------------------------------------------------------


@dataclass
class QuestionScore:
    id: str
    type: str
    question: str
    answerable: bool  # should be answered
    answered: bool  # was answered
    entity_hits: list[bool] = field(default_factory=list)
    event_hits: list[bool] = field(default_factory=list)
    correctness: int | None = None  # primary judge
    correctness_reason: str = ""
    faithfulness: float | None = None  # primary judge
    claims_supported: int = 0
    claims_judged: int = 0
    seconds: float | None = None
    error: str | None = None

    @property
    def entity_recall(self) -> float | None:
        return share(self.entity_hits)

    @property
    def event_recall(self) -> float | None:
        return share(self.event_hits)

    @property
    def retrieval_recall(self) -> float | None:
        """Entities and events together: what the context needed to hold."""
        return share(self.entity_hits + self.event_hits)


def score_question(
    question: QAQuestion,
    record: AnswerRecord,
    names_by_qid: dict[str, list[str]],
    titles_by_key: dict[str, str],
    correctness: QAJudgement | None,
    faithfulness: QAJudgement | None,
) -> QuestionScore:
    """One question's scores from its answer and judgements."""
    verdicts = faithfulness.claims if faithfulness else []
    judged = [v.supported for v in verdicts if v.supported is not None]
    return QuestionScore(
        id=question.id,
        type=question.type,
        question=question.question,
        answerable=question.answerable,
        answered=record.answered,
        entity_hits=entity_hits(question.expected_entities, names_by_qid, record),
        event_hits=event_hits(question.expected_events, titles_by_key, record),
        correctness=int(correctness.score)
        if correctness and correctness.score is not None
        else None,
        correctness_reason=correctness.reason if correctness else "",
        faithfulness=faithfulness.score if faithfulness else None,
        claims_supported=sum(judged),
        claims_judged=len(judged),
        seconds=record.seconds.get("total"),
        error=record.error,
    )


# --- Aggregates -------------------------------------------------------------------------


def mean(values: Iterable[float | None]) -> float | None:
    """The mean of the values that are not None, or None."""
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def percentile(values: Sequence[float], q: float) -> float | None:
    """The q-th percentile (0 to 100), nearest-rank."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(scores: Sequence[QuestionScore]) -> dict[str, Any]:
    """The metrics of a group of questions (all, or one type)."""
    answerable = [s for s in scores if s.answerable]
    unanswerable = [s for s in scores if not s.answerable]
    judged = [s.correctness for s in scores if s.correctness is not None]
    supported = sum(s.claims_supported for s in scores)
    claims = sum(s.claims_judged for s in scores)
    seconds = [s.seconds for s in scores if s.seconds is not None]
    return {
        "questions": len(scores),
        "entity_recall": mean(s.entity_recall for s in scores),
        "entity_recall_n": sum(1 for s in scores if s.entity_hits),
        "event_recall": mean(s.event_recall for s in scores),
        "event_recall_n": sum(1 for s in scores if s.event_hits),
        "correctness": mean(judged),
        "fully_correct": sum(1 for c in judged if c == 2) / len(judged) if judged else None,
        "judged": len(judged),
        "faithfulness": supported / claims if claims else None,
        "claims": claims,
        "correct_refusals": sum(not s.answered for s in unanswerable) / len(unanswerable)
        if unanswerable
        else None,
        "unanswerable": len(unanswerable),
        "false_refusals": sum(not s.answered for s in answerable) / len(answerable)
        if answerable
        else None,
        "answerable": len(answerable),
        "latency_median_s": statistics.median(seconds) if seconds else None,
        "latency_p90_s": percentile(seconds, 90),
        "latency_max_s": max(seconds) if seconds else None,
        "errors": sum(1 for s in scores if s.error),
    }


def summarize_by_type(scores: Sequence[QuestionScore]) -> dict[str, dict[str, Any]]:
    """The summary metrics per question type."""
    types = [t for t in QUESTION_TYPES if any(s.type == t for s in scores)]
    return {t: summarize([s for s in scores if s.type == t]) for t in types}


def failure_class(score: QuestionScore) -> str:
    """Where an answerable question went wrong: retrieval, generation, or nowhere."""
    if score.correctness is None:
        return "not judged"
    if score.correctness == 2:
        return "correct"
    recall = score.retrieval_recall
    if recall is None:
        return "wrong, retrieval not measurable"
    if recall >= RETRIEVAL_OK:
        return "wrong, retrieval complete (generation failure)"
    return "wrong, retrieval incomplete (retrieval failure)"


def failure_split(scores: Sequence[QuestionScore]) -> dict[str, list[str]]:
    """Question ids of the answerable questions, by `failure_class`."""
    split: dict[str, list[str]] = {}
    for score in scores:
        if score.answerable:
            split.setdefault(failure_class(score), []).append(score.id)
    return split


# --- Agreement between judges -----------------------------------------------------------


def cohen_kappa(
    a: Sequence[Any], b: Sequence[Any], categories: Sequence[Any], weighted: bool = False
) -> float | None:
    """Cohen's kappa of two raters over the same items; `weighted` uses linear weights
    (for ordered categories such as 0, 1, 2: disagreeing by 1 costs half of by 2)."""
    if len(a) != len(b) or not a:
        return None
    k = len(categories)
    index = {c: i for i, c in enumerate(categories)}

    def cost(i: int, j: int) -> float:
        if weighted and k > 1:
            return abs(i - j) / (k - 1)
        return 0.0 if i == j else 1.0

    n = len(a)
    observed = sum(cost(index[x], index[y]) for x, y in zip(a, b, strict=True)) / n
    freq_a = [sum(1 for x in a if x == c) / n for c in categories]
    freq_b = [sum(1 for y in b if y == c) / n for c in categories]
    expected = sum(freq_a[i] * freq_b[j] * cost(i, j) for i in range(k) for j in range(k))
    if expected == 0:
        return 1.0 if observed == 0 else None
    return 1 - observed / expected


def correctness_agreement(a: dict[str, int], b: dict[str, int]) -> dict[str, Any]:
    """Two judges' correctness scores (question id -> 0..2) on the questions both judged."""
    common = sorted(set(a) & set(b))
    xs, ys = [a[q] for q in common], [b[q] for q in common]
    matrix = [
        [sum(1 for x, y in zip(xs, ys, strict=True) if (x, y) == (i, j)) for j in range(3)]
        for i in range(3)
    ]
    return {
        "items": len(common),
        "exact": sum(x == y for x, y in zip(xs, ys, strict=True)) / len(common) if common else None,
        "within_one": sum(abs(x - y) <= 1 for x, y in zip(xs, ys, strict=True)) / len(common)
        if common
        else None,
        "kappa_linear": cohen_kappa(xs, ys, [0, 1, 2], weighted=True),
        "mean_a": mean(xs),
        "mean_b": mean(ys),
        "matrix": matrix,  # rows: judge a's score, columns: judge b's
        "disagreements": [q for q in common if a[q] != b[q]],
    }


def claim_agreement(a: dict[str, list[bool | None]], b: dict[str, list[bool | None]]) -> dict:
    """Two judges' verdicts on the same claims (question id -> one verdict per claim)."""
    xs: list[bool] = []
    ys: list[bool] = []
    score_gaps = []
    for question in sorted(set(a) & set(b)):
        pairs = [
            (x, y) for x, y in zip(a[question], b[question], strict=False) if None not in (x, y)
        ]
        xs += [bool(x) for x, _ in pairs]
        ys += [bool(y) for _, y in pairs]
        if pairs:
            share_a = sum(x for x, _ in pairs) / len(pairs)
            share_b = sum(y for _, y in pairs) / len(pairs)
            score_gaps.append(abs(share_a - share_b))
    return {
        "claims": len(xs),
        "agreement": sum(x == y for x, y in zip(xs, ys, strict=True)) / len(xs) if xs else None,
        "kappa": cohen_kappa(xs, ys, [False, True]),
        "supported_a": sum(xs) / len(xs) if xs else None,
        "supported_b": sum(ys) / len(ys) if ys else None,
        "mean_answer_gap": mean(score_gaps),
    }
