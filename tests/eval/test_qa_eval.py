"""The QA evaluation run: answer step, judge step, report and manual rating (all mocked)."""

import json
from datetime import UTC, datetime
from pathlib import Path

from tests.llm.helpers import ScriptedClient
from wikipulse.eval.qa import (
    answer_all,
    answer_claims,
    judge_all,
    judge_text,
    parse_args,
    rate_manually,
    read_answers,
    record_answer,
)
from wikipulse.eval.qa_judge import QAJudge, answer_hash, load_judgements
from wikipulse.eval.qa_metrics import AnswerRecord, FactView
from wikipulse.eval.qa_questions import QAQuestion
from wikipulse.eval.qa_report import build_report, render_markdown
from wikipulse.llm import LLMUnavailableError
from wikipulse.qa.types import Answer, Citation, Context, Fact

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def clock() -> datetime:
    return NOW


def question(id_: str, answerable: bool = True, **changes) -> QAQuestion:
    values = {
        "id": id_,
        "question": f"Who is involved in {id_}?",
        "type": "event" if answerable else "unanswerable",
        "expected_answer": "Ueno Bank and Santiago Peña." if answerable else "No facts.",
        "expected_entities": ["Q1", "Q2"] if answerable else [],
        "answerable": answerable,
        "source": "synthetic",
        "labeler": "llm",
    }
    return QAQuestion(**{**values, **changes})


def answer_for(text: str, answerable: bool = True) -> Answer:
    context = Context(question="Q?")
    context.add(
        Fact(
            kind="relation",
            channel="graph",
            text="Ueno Bank — collapsed — Paraguay",
            sources=[Citation(title="Ueno Bank", revision=1)],
        )
    )
    context.entities = []
    context.events = ["r:1"]
    context.seconds = {"graph": 0.1}
    return Answer(
        question="Q?",
        text=text.replace("[F1]", "[1]"),
        fact_text=text,
        answerable=answerable,
        citations=[Citation(title="Ueno Bank", revision=1)] if answerable else [],
        context=context,
        model="fake:qa",
        seconds={"answer": 1.0, "total": 1.5},
    )


class FakeSession:
    def __init__(self, answers: dict[str, Answer], fail: set[str] = frozenset()) -> None:
        self.answers = answers
        self.fail = fail
        self.asked: list[str] = []

    def ask(self, text: str) -> Answer:
        self.asked.append(text)
        if text in self.fail:
            raise LLMUnavailableError("Ollama is down")
        return self.answers[text]


def test_record_answer_keeps_context_and_timings() -> None:
    record = record_answer("r1", question("q1"), answer_for("Ueno Bank collapsed [F1]."))
    assert record.answered and record.fact_text == "Ueno Bank collapsed [F1]."
    assert record.text == "Ueno Bank collapsed [1]."
    assert record.facts == [
        FactView(id="F1", kind="relation", channel="graph", text="Ueno Bank — collapsed — Paraguay")
    ]
    assert record.citations == ["Ueno Bank, revision 1"]
    assert record.events == ["r:1"]
    assert record.seconds == {"graph": 0.1, "answer": 1.0, "total": 1.5}


def test_answer_all_resumes_and_records_failures(tmp_path: Path) -> None:
    path = tmp_path / "answers.jsonl"
    q1, q2, q3 = question("q1"), question("q2"), question("q3")
    session = FakeSession(
        {
            q1.question: answer_for("A [F1]."),
            q2.question: answer_for("B [F1]."),
        },
        fail={q3.question},
    )
    assert answer_all(session, [q1], path, "r1") == 1
    assert answer_all(session, [q1, q2, q3], path, "r1") == 2  # q1 is not asked again
    assert session.asked == [q1.question, q2.question, q3.question]
    saved = read_answers(path)
    assert set(saved) == {"q1", "q2", "q3"}
    assert saved["q3"].error and not saved["q3"].answered


def saved_answers() -> dict[str, AnswerRecord]:
    facts = [FactView(id="F1", kind="relation", channel="graph", text="Ueno Bank collapsed")]
    return {
        "q1": AnswerRecord(
            run="r1",
            question_id="q1",
            question="Who?",
            type="event",
            answered=True,
            text="Ueno Bank collapsed [1]. Peña resigned [1].",
            fact_text="Ueno Bank collapsed [F1]. Peña resigned [F1].",
            facts=facts,
            seconds={"total": 2.0},
        ),
        "q2": AnswerRecord(
            run="r1",
            question_id="q2",
            question="Fed?",
            type="unanswerable",
            answered=False,
            text="The knowledge graph has no facts about this question.",
            seconds={"total": 0.5},
        ),
        "q3": AnswerRecord(
            run="r1", question_id="q3", question="?", type="event", answered=False, error="boom"
        ),
    }


def judge_replies() -> list[str]:
    return [
        json.dumps({"reason": "partly", "score": 1}),
        json.dumps(
            {
                "claims": [
                    {"claim": 1, "reason": "F1", "supported": True},
                    {"claim": 2, "reason": "not in F1", "supported": False},
                ]
            }
        ),
        json.dumps({"reason": "refused, right", "score": 2}),
    ]


def test_judge_all_judges_once_and_skips_refusals_for_faithfulness(tmp_path: Path) -> None:
    path = tmp_path / "qa_judgements.jsonl"
    questions = [question("q1"), question("q2", answerable=False), question("q3")]
    client = ScriptedClient(judge_replies(), model="judge")
    made = judge_all(QAJudge(client), questions, saved_answers(), "r1", path, clock)
    assert made == 3  # q1: correctness and faithfulness; q2: correctness; q3 failed
    assert "Peña resigned" in client.requests[0].messages[0].content
    assert "[1]" not in client.requests[0].messages[0].content
    records = load_judgements(path)
    faith = next(r for r in records if r.metric == "faithfulness")
    assert faith.score == 0.5 and [c.supported for c in faith.claims] == [True, False]
    assert all(r.labeler == "llm" and r.model == "fake:judge" for r in records)
    # A second run has nothing left to judge (the scripted client has no answers left).
    assert judge_all(QAJudge(client), questions, saved_answers(), "r1", path, clock) == 0


def test_judge_text_drops_citation_numbers() -> None:
    record = saved_answers()["q1"]
    assert judge_text(record) == "Ueno Bank collapsed. Peña resigned."


def test_report_scores_agreement_and_markdown(tmp_path: Path) -> None:
    path = tmp_path / "qa_judgements.jsonl"
    questions = [question("q1"), question("q2", answerable=False), question("q3")]
    answers = saved_answers()
    judge_all(QAJudge(ScriptedClient(judge_replies(), "a")), questions, answers, "r1", path, clock)
    second = [
        json.dumps({"reason": "fine", "score": 2}),
        json.dumps(
            {
                "claims": [
                    {"claim": 1, "reason": "", "supported": True},
                    {"claim": 2, "reason": "", "supported": True},
                ]
            }
        ),
        json.dumps({"reason": "refused", "score": 2}),
    ]
    judge_all(QAJudge(ScriptedClient(second, "b")), questions, answers, "r1", path, clock)
    report = build_report(
        "r1",
        questions,
        answers,
        load_judgements(path),
        ["fake:a", "fake:b"],
        {"Q1": ["Ueno Bank"], "Q2": ["Santiago Peña"]},
        {},
        {"note": "test"},
    )
    m = report["metrics"]
    assert m["questions"] == 3 and m["errors"] == 1
    assert m["entity_recall"] == 0.25  # q1 finds Ueno Bank only, q3 found nothing
    assert m["correctness"] == 1.5 and m["correct_refusals"] == 1.0
    assert m["faithfulness"] == 0.5
    agreement = report["agreement"][0]
    assert (agreement["a"], agreement["b"]) == ("fake:a", "fake:b")
    assert agreement["correctness"]["exact"] == 0.5
    assert agreement["faithfulness"]["agreement"] == 0.5
    assert report["failure_split"]["wrong, retrieval incomplete (retrieval failure)"] == ["q1"]
    text = render_markdown(report)
    assert "# QA evaluation: r1" in text
    assert "### fake:a vs fake:b" in text
    assert "| q1 | event |" in text


def test_rating_appends_human_judgements(tmp_path: Path) -> None:
    path = tmp_path / "qa_judgements.jsonl"
    questions = [question("q1"), question("q2", answerable=False)]
    answers = saved_answers()
    judge_all(QAJudge(ScriptedClient(judge_replies(), "a")), questions, answers, "r1", path, clock)
    replies = iter(["5", "1", "my note", "2", ""])
    shown: list[str] = []
    rated, compared, agreed = rate_manually(
        questions, answers, "r1", path, "fake:a", 15, clock, lambda _: next(replies), shown.append
    )
    assert (rated, compared, agreed) == (2, 2, 2)
    humans = [r for r in load_judgements(path) if r.labeler == "human"]
    assert [(h.question_id, h.score, h.reason) for h in humans] == [
        ("q1", 1, "my note"),
        ("q2", 2, ""),
    ]
    assert humans[0].answer_hash == answer_hash(True, answers["q1"].fact_text)
    # Rated answers are not offered again.
    assert rate_manually(questions, answers, "r1", path, "fake:a", 15, clock, input, print) == (
        0,
        0,
        0,
    )


def test_parse_args_defaults_from_settings() -> None:
    args = parse_args(["--name", "x"])
    assert args.steps == ["answer", "judge", "report"]
    assert len(args.judges) >= 1
    assert sorted(args.channels) == ["cypher", "graph", "vector"]


def test_a_corrected_reference_is_judged_again(tmp_path: Path) -> None:
    path = tmp_path / "qa_judgements.jsonl"
    questions = [question("q1"), question("q2", answerable=False)]
    answers = saved_answers()
    judge_all(QAJudge(ScriptedClient(judge_replies(), "a")), questions, answers, "r1", path, clock)
    corrected = [question("q1", expected_answer="Ueno Bank, Santiago Peña and Visión Banco.")]
    client = ScriptedClient([json.dumps({"reason": "new reference", "score": 2})], "a")
    # Only correctness is asked again; faithfulness doesn't depend on the reference.
    assert judge_all(QAJudge(client), corrected, answers, "r1", path, clock) == 1
    report = build_report("r1", corrected, answers, load_judgements(path), ["fake:a"], {}, {}, {})
    assert report["questions"][0]["correctness"] == {"fake:a": 2}


def test_an_answer_that_is_only_a_cited_list_is_one_claim() -> None:
    record = saved_answers()["q1"].model_copy(
        update={"fact_text": "The Democratic Party is involved in 11 events: [F1][F2]"}
    )
    assert answer_claims(record) == ["The Democratic Party is involved in 11 events: [F1][F2]"]
    assert answer_claims(saved_answers()["q1"]) == [
        "Ueno Bank collapsed [F1].",
        "Peña resigned [F1].",
    ]
    assert answer_claims(saved_answers()["q2"]) == []
