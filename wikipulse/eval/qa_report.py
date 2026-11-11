"""The QA evaluation report: numbers as a dict (saved as JSON) and as Markdown."""

from collections.abc import Sequence
from typing import Any

from wikipulse.eval.qa_judge import (
    CORRECTNESS_PROMPT,
    FAITHFULNESS_PROMPT,
    QAJudgement,
    answer_hash,
    find_judgement,
    reference_hash,
)
from wikipulse.eval.qa_metrics import (
    RETRIEVAL_OK,
    AnswerRecord,
    QuestionScore,
    claim_agreement,
    correctness_agreement,
    failure_split,
    mean,
    score_question,
    summarize,
    summarize_by_type,
)
from wikipulse.eval.qa_questions import QAQuestion

HUMAN = "human"


def judgements_by_judge(
    questions: Sequence[QAQuestion],
    answers: dict[str, AnswerRecord],
    records: Sequence[QAJudgement],
    run: str,
    judge: str,
) -> tuple[dict[str, QAJudgement], dict[str, QAJudgement]]:
    """A judge's (correctness, faithfulness) judgements of this run's current answers."""
    correctness: dict[str, QAJudgement] = {}
    faithfulness: dict[str, QAJudgement] = {}
    for question in questions:
        record = answers.get(question.id)
        if record is None:
            continue
        hash_ = answer_hash(record.answered, record.fact_text or record.text)
        found = find_judgement(
            records,
            run,
            question.id,
            hash_,
            "correctness",
            judge,
            CORRECTNESS_PROMPT,
            reference_hash(question),
        )
        if found and found.score is not None:
            correctness[question.id] = found
        found = find_judgement(
            records, run, question.id, hash_, "faithfulness", judge, FAITHFULNESS_PROMPT
        )
        if found:
            faithfulness[question.id] = found
    return correctness, faithfulness


def _scores(judged: dict[str, QAJudgement]) -> dict[str, int]:
    return {q: int(j.score) for q, j in judged.items() if j.score is not None}


def _verdicts(judged: dict[str, QAJudgement]) -> dict[str, list[bool | None]]:
    return {q: [c.supported for c in j.claims] for q, j in judged.items()}


def agreement_section(
    judged: dict[str, tuple[dict[str, QAJudgement], dict[str, QAJudgement]]], primary: str
) -> list[dict[str, Any]]:
    """Every other judge (and the human, if any) against the primary judge, and the
    human against every model judge."""
    pairs = [(primary, other) for other in judged if other != primary]
    if HUMAN in judged:
        pairs += [(HUMAN, other) for other in judged if other not in (HUMAN, primary)]
    section = []
    for a, b in pairs:
        correctness_a, faithfulness_a = judged[a]
        correctness_b, faithfulness_b = judged[b]
        entry = {
            "a": a,
            "b": b,
            "correctness": correctness_agreement(_scores(correctness_a), _scores(correctness_b)),
        }
        if faithfulness_a and faithfulness_b:
            entry["faithfulness"] = claim_agreement(
                _verdicts(faithfulness_a), _verdicts(faithfulness_b)
            )
        section.append(entry)
    return section


def question_row(
    score: QuestionScore,
    record: AnswerRecord,
    judged: dict[str, tuple[dict[str, QAJudgement], dict[str, QAJudgement]]],
) -> dict[str, Any]:
    """One question's row of the report."""
    return {
        "id": score.id,
        "type": score.type,
        "question": score.question,
        "answerable": score.answerable,
        "answered": score.answered,
        "answer": record.text,
        "entity_recall": score.entity_recall,
        "event_recall": score.event_recall,
        "correctness": {
            judge: c[score.id].score for judge, (c, _) in judged.items() if score.id in c
        },
        "faithfulness": {
            judge: f[score.id].score for judge, (_, f) in judged.items() if score.id in f
        },
        "judge_reason": score.correctness_reason,
        "facts": len(record.facts),
        "channels": sorted({f.channel for f in record.facts}),
        "cypher": record.cypher,
        "cypher_rows": record.cypher_rows,
        "seconds": record.seconds,
        "error": record.error,
    }


def build_report(
    name: str,
    questions: Sequence[QAQuestion],
    answers: dict[str, AnswerRecord],
    records: Sequence[QAJudgement],
    judges: Sequence[str],
    names_by_qid: dict[str, list[str]],
    titles_by_key: dict[str, str],
    config: dict[str, Any],
) -> dict[str, Any]:
    """The whole QA report as a JSON-ready dict."""
    primary = judges[0]
    everyone = [*judges]
    if any(r.labeler == "human" and r.run == name for r in records):
        everyone.append(HUMAN)
    judged = {j: judgements_by_judge(questions, answers, records, name, j) for j in everyone}
    correctness, faithfulness = judged[primary]
    scores = [
        score_question(
            q,
            answers[q.id],
            names_by_qid,
            titles_by_key,
            correctness.get(q.id),
            faithfulness.get(q.id),
        )
        for q in questions
        if q.id in answers
    ]
    per_judge = {
        judge: {
            "correctness": mean(j.score for j in c.values()),
            "fully_correct": mean(1.0 if j.score == 2 else 0.0 for j in c.values()),
            "judged": len(c),
            "faithfulness": _claim_share(f),
        }
        for judge, (c, f) in judged.items()
    }
    return {
        "name": name,
        "config": {**config, "primary_judge": primary, "judges": list(judges)},
        "metrics": summarize(scores),
        "by_type": summarize_by_type(scores),
        "failure_split": failure_split(scores),
        "per_judge": per_judge,
        "agreement": agreement_section(judged, primary),
        "questions": [question_row(s, answers[s.id], judged) for s in scores],
    }


def _claim_share(faithfulness: dict[str, QAJudgement]) -> float | None:
    verdicts = [c.supported for j in faithfulness.values() for c in j.claims]
    judged = [v for v in verdicts if v is not None]
    return sum(judged) / len(judged) if judged else None


# --- Markdown -----------------------------------------------------------------------------


def pct(value: float | None) -> str:
    """A share as a percentage with one decimal, or `n/a`."""
    return "n/a" if value is None else f"{100 * value:.1f}%"


def num(value: float | None, digits: int = 2) -> str:
    """A number with `digits` decimals, or `n/a`."""
    return "n/a" if value is None else f"{value:.{digits}f}"


def summary_line(report: dict[str, Any]) -> str:
    """The report's main numbers in one line."""
    m = report["metrics"]
    return (
        f"{m['questions']} questions: correctness {num(m['correctness'])} of 2 "
        f"({pct(m['fully_correct'])} fully correct), faithfulness {pct(m['faithfulness'])}, "
        f"entity recall {pct(m['entity_recall'])}, event recall {pct(m['event_recall'])}, "
        f"correct refusals {pct(m['correct_refusals'])}, false refusals "
        f"{pct(m['false_refusals'])}, median {num(m['latency_median_s'], 1)} s"
    )


def _metrics_row(label: str, m: dict[str, Any]) -> str:
    return (
        f"| {label} | {m['questions']} | {pct(m['entity_recall'])} | {pct(m['event_recall'])} "
        f"| {num(m['correctness'])} | {pct(m['fully_correct'])} | {pct(m['faithfulness'])} "
        f"({m['claims']}) | {pct(m['correct_refusals'])} | {pct(m['false_refusals'])} "
        f"| {num(m['latency_median_s'], 1)} / {num(m['latency_p90_s'], 1)} |"
    )


def _cell(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split()).replace("|", r"\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _agreement_lines(entry: dict[str, Any]) -> list[str]:
    c = entry["correctness"]
    lines = [
        f"### {entry['a']} vs {entry['b']}",
        "",
        f"- Correctness, {c['items']} answers: exact agreement {pct(c['exact'])}, within one "
        f"point {pct(c['within_one'])}, Cohen's kappa (linear weights) "
        f"{num(c['kappa_linear'])}; mean score {num(c['mean_a'])} vs {num(c['mean_b'])}.",
        f"- Disagreements: {', '.join(c['disagreements']) or 'none'}.",
        "",
        rf"| {entry['a']} \ {entry['b']} | 0 | 1 | 2 |",
        "|---|---:|---:|---:|",
    ]
    lines += [
        f"| {i} | " + " | ".join(str(v) for v in row) + " |" for i, row in enumerate(c["matrix"])
    ]
    f = entry.get("faithfulness")
    if f:
        lines += [
            "",
            f"- Faithfulness, {f['claims']} claims judged by both: agreement "
            f"{pct(f['agreement'])}, Cohen's kappa {num(f['kappa'])}; supported "
            f"{pct(f['supported_a'])} vs {pct(f['supported_b'])}; mean difference of an "
            f"answer's score {pct(f['mean_answer_gap'])}.",
        ]
    return [*lines, ""]


def render_markdown(report: dict[str, Any]) -> str:
    """The QA report as Markdown."""
    config = report["config"]
    m = report["metrics"]
    judges = [*config["judges"], *([HUMAN] if HUMAN in report["per_judge"] else [])]
    lines = [
        f"# QA evaluation: {report['name']}",
        "",
        summary_line(report) + ".",
        "",
        "## Configuration",
        "",
    ]
    lines += [f"- {key}: `{json_value(value)}`" for key, value in config.items()]
    lines += [
        "",
        "## Results",
        "",
        "Recall: share of a question's expected entities (events) found in the retrieved "
        "facts, averaged over the questions that have any. Correctness: primary judge, 0 to "
        "2. Faithfulness: supported claims / judged claims (number of claims). Latency: "
        "median / 90th percentile seconds per question.",
        "",
        "| | Questions | Entity recall | Event recall | Correctness | Fully correct "
        "| Faithfulness | Correct refusals | False refusals | Latency (s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        _metrics_row("**all**", m),
    ]
    lines += [_metrics_row(t, tm) for t, tm in report["by_type"].items()]
    lines += [
        "",
        f"Errors: {m['errors']}. Slowest question: {num(m['latency_max_s'], 1)} s.",
        "",
        "## Retrieval or generation?",
        "",
        f"Answerable questions by outcome (retrieval counts as complete at a recall of "
        f"{RETRIEVAL_OK:.0%} or more of the expected entities and events):",
        "",
    ]
    lines += [
        f"- {label}: {len(ids)} ({', '.join(ids)})"
        for label, ids in report["failure_split"].items()
    ]
    lines += [
        "",
        "## Judges",
        "",
        "| Judge | Answers | Correctness | Fully correct | Faithfulness |",
    ]
    lines.append("|---|---:|---:|---:|---:|")
    for judge, values in report["per_judge"].items():
        lines.append(
            f"| {judge} | {values['judged']} | {num(values['correctness'])} "
            f"| {pct(values['fully_correct'])} | {pct(values['faithfulness'])} |"
        )
    lines += ["", "## Judge agreement", ""]
    for entry in report["agreement"]:
        lines += _agreement_lines(entry)
    if not report["agreement"]:
        lines += ["Only one judge; no agreement to report.", ""]
    header = " | ".join(judges)
    lines += [
        "## Questions",
        "",
        f"| Id | Type | Question | Answered | Entity / event recall | Correctness ({header}) "
        "| Faithfulness | Facts | Seconds |",
        "|---|---|---|:-:|---|---|---|---:|---:|",
    ]
    for q in report["questions"]:
        scores = " / ".join(str(q["correctness"].get(j, "–")) for j in judges)
        faith = " / ".join(
            num(q["faithfulness"][j]) if q["faithfulness"].get(j) is not None else "–"
            for j in judges
        )
        lines.append(
            f"| {q['id']} | {q['type']} | {_cell(q['question'], 70)} "
            f"| {'yes' if q['answered'] else 'no'} | {pct(q['entity_recall'])} / "
            f"{pct(q['event_recall'])} | {scores} | {faith} | {q['facts']} "
            f"| {num(q['seconds'].get('total'), 1)} |"
        )
    lines += ["", "## Answers", ""]
    for q in report["questions"]:
        lines += [
            f"**{q['id']}** ({q['type']}) {q['question']}",
            "",
            "> " + "\n> ".join(_cell(q["answer"], 700).splitlines() or [""]),
            "",
            f"Judge ({config['primary_judge']}): {q['judge_reason'] or 'n/a'}",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def json_value(value: Any) -> str:
    """A config value as text for the report."""
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)
