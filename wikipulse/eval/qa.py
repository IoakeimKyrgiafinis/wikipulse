"""Evaluate question answering on the fixed question set, against a frozen graph.

    uv run python -m wikipulse.eval.qa --name 05b-v1
    uv run python -m wikipulse.eval.qa --name 05b-v1 --steps report
    uv run python -m wikipulse.eval.qa --name 05b-v1 --rate     (rate answers yourself)

Three steps, each over all questions before the next starts, so one GPU never swaps
models between questions:

1. `answer`: the QA pipeline (`wikipulse.qa`, model QA_MODEL) answers every question of
   `evals/labels/qa_questions.jsonl` from the snapshot at EVAL_QA_NEO4J_URI. Answers and
   their contexts go to `data/qa_eval/<name>/answers.jsonl` (resumable).
2. `judge`: every judge model of EVAL_QA_JUDGES (or `--judges`) grades every answer:
   correctness 0 to 2 and the faithfulness of each claim. Judgements are appended to
   `evals/labels/qa_judgements.jsonl`; what a judge already judged is skipped.
3. `report`: `evals/results/<date>_<name>.md` and `.json`: entity and event recall,
   correctness, faithfulness, refusals, latency, overall and per question type; which
   wrong answers are retrieval and which generation failures; how far the judges (and
   your own ratings, if any) agree.

The snapshot comes from `scripts/snapshot_graph.py restore`; the questions from
`uv run python -m wikipulse.eval.qa_questions`.
"""

import argparse
import json
import logging
import re
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j.exceptions import Neo4jError

from wikipulse.config import Settings, get_settings
from wikipulse.eval.detection import git_commit
from wikipulse.eval.qa_judge import (
    CORRECTNESS_PROMPT,
    DEFAULT_JUDGEMENTS_PATH,
    FAITHFULNESS_PROMPT,
    QAJudge,
    QAJudgement,
    answer_hash,
    append_judgement,
    faithfulness_score,
    find_judgement,
    load_judgements,
    reference_hash,
)
from wikipulse.eval.qa_metrics import AnswerRecord, FactView
from wikipulse.eval.qa_questions import (
    DEFAULT_QUESTIONS_PATH,
    QAQuestion,
    Snapshot,
    load_questions,
)
from wikipulse.eval.qa_report import build_report, render_markdown, summary_line
from wikipulse.llm import LLMError, get_client
from wikipulse.logging_setup import setup_logging
from wikipulse.qa.answer import PROMPT_VERSION as ANSWER_PROMPT
from wikipulse.qa.answer import QASession, claims
from wikipulse.qa.cypher import PROMPT_VERSION as CYPHER_PROMPT
from wikipulse.qa.retrieve import ALL_CHANNELS, RetrievalConfig
from wikipulse.qa.types import Answer, Channel

logger = logging.getLogger(__name__)

DEFAULT_RESULTS_DIR = Path("evals") / "results"
STEPS = ("answer", "judge", "report")
WARM_UP_QUESTION = "Which stories are in the graph?"
_CITATION_NUMBERS = re.compile(r"\s*(?:\[\d+\])+")


def answers_path(settings: Settings, name: str) -> Path:
    """Where the answers of an evaluation run are stored."""
    return settings.data_dir / "qa_eval" / name / "answers.jsonl"


def read_answers(path: Path) -> dict[str, AnswerRecord]:
    """The saved answers by question id (the last one wins)."""
    if not path.exists():
        return {}
    records = [
        AnswerRecord.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {r.question_id: r for r in records}


def append_answer(path: Path, record: AnswerRecord) -> None:
    """Append one answer record to the answers file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as out:
        out.write(record.model_dump_json() + "\n")


def record_answer(run: str, question: QAQuestion, result: Answer) -> AnswerRecord:
    """The stored record of the system's answer to a question."""
    context = result.context
    return AnswerRecord(
        run=run,
        question_id=question.id,
        question=question.question,
        type=question.type,
        answered=result.answerable,
        text=result.text,
        fact_text=result.fact_text,
        citations=[c.label() for c in result.citations],
        facts=[
            FactView(id=f.id, kind=f.kind, channel=f.channel, text=f.text)
            for f in (context.facts if context else [])
        ],
        entities=[e.name for e in context.entities] if context else [],
        events=list(context.events) if context else [],
        cypher=context.cypher if context else None,
        cypher_rows=context.cypher_rows if context else None,
        notes=list(context.notes) if context else [],
        limits=list(result.limits),
        removed_sentences=list(result.removed_sentences),
        seconds={**(context.seconds if context else {}), **result.seconds},
        model=result.model,
    )


def eval_settings(settings: Settings, neo4j_uri: str | None) -> Settings:
    """The settings with the snapshot as the graph that QA reads."""
    return settings.model_copy(update={"neo4j_uri": neo4j_uri or settings.eval_qa_neo4j_uri})


# --- Step 1: answer -----------------------------------------------------------------------


def answer_all(session: QASession, questions: Sequence[QAQuestion], path: Path, run: str) -> int:
    """Answer the questions that have no saved answer yet; returns how many."""
    done = read_answers(path)
    todo = [q for q in questions if q.id not in done]
    logger.info("%d questions to answer (%d already answered)", len(todo), len(done))
    for number, question in enumerate(todo, start=1):
        started = time.monotonic()
        try:
            record = record_answer(run, question, session.ask(question.question))
        except (LLMError, Neo4jError, OSError) as error:
            logger.warning("%s failed: %r", question.id, error)
            record = AnswerRecord(
                run=run,
                question_id=question.id,
                question=question.question,
                type=question.type,
                answered=False,
                error=repr(error),
                seconds={"total": round(time.monotonic() - started, 3)},
            )
        append_answer(path, record)
        logger.info(
            "[%d/%d] %s (%s, %.1f s): %s",
            number,
            len(todo),
            question.id,
            "answered" if record.answered else "refused",
            record.seconds.get("total", 0),
            record.text[:120].replace("\n", " "),
        )
    return len(todo)


def run_answer(args: argparse.Namespace, settings: Settings, questions: list[QAQuestion]) -> str:
    """The `answer` step: ask every question not answered yet; returns a summary."""
    path = answers_path(settings, args.name)
    if args.force and path.exists():
        path.unlink()
    with QASession.from_settings(
        eval_settings(settings, args.neo4j_uri), channels=args.channels, model=args.model
    ) as session:
        # Loads the embedding model and the LLM, so the first question's latency is
        # not mostly loading time. Not recorded.
        logger.info("Warm-up: %s", WARM_UP_QUESTION)
        session.ask(WARM_UP_QUESTION)
        answered = answer_all(session, questions, path, args.name)
        usage = session.client.total_usage
    return (
        f"Answered {answered} questions into {path} with {session.client.setting} "
        f"({usage.calls} LLM calls, {usage.latency_s:.0f} s of LLM time)."
    )


# --- Step 2: judge ------------------------------------------------------------------------


def judge_text(record: AnswerRecord) -> str:
    """The answer as a reader sees it, without the citation numbers."""
    return _CITATION_NUMBERS.sub("", record.text).strip()


def answer_claims(record: AnswerRecord) -> list[str]:
    """The claims of an answer, for faithfulness. An answer whose only sentence ends in a
    list of citations ("... in 11 events: [F22][F23]") has no claim by `claims()`'s rules
    (a line ending in ":" is a heading); then the whole answer is one claim."""
    if not record.answered:
        return []
    found = claims(record.fact_text)
    if not found and record.fact_text.strip():
        return [record.fact_text.strip()]
    return found


def judge_all(
    judge: QAJudge,
    questions: Sequence[QAQuestion],
    answers: dict[str, AnswerRecord],
    run: str,
    path: Path,
    now: Callable[[], datetime],
) -> int:
    """Correctness and faithfulness for every answer this judge hasn't judged yet."""
    records = load_judgements(path)
    made = 0
    for question in questions:
        record = answers.get(question.id)
        if record is None or record.error:
            continue
        hash_ = answer_hash(record.answered, record.fact_text or record.text)
        common = {"run": run, "question_id": question.id, "answer_hash": hash_, "labeler": "llm"}
        reference = reference_hash(question)
        if not find_judgement(
            records,
            run,
            question.id,
            hash_,
            "correctness",
            judge.model,
            CORRECTNESS_PROMPT,
            reference,
        ):
            try:
                score, reason = judge.correctness(question, judge_text(record), record.answered)
            except (LLMError, ValueError) as error:
                logger.warning("%s: no correctness judgement (%r)", question.id, error)
            else:
                new = QAJudgement(
                    **common,
                    metric="correctness",
                    reference_hash=reference,
                    score=score,
                    model=judge.model,
                    prompt_version=CORRECTNESS_PROMPT,
                    reason=reason,
                    labeled_at=now(),
                )
                append_judgement(path, new)
                records.append(new)
                made += 1
                logger.info("%s correctness %d (%s): %s", question.id, score, judge.model, reason)
        claim_list = answer_claims(record)
        if claim_list and not find_judgement(
            records, run, question.id, hash_, "faithfulness", judge.model, FAITHFULNESS_PROMPT
        ):
            facts = [(f.id, f.text) for f in record.facts]
            try:
                verdicts = judge.faithfulness(question.question, facts, claim_list)
            except (LLMError, ValueError) as error:
                logger.warning("%s: no faithfulness judgement (%r)", question.id, error)
                continue
            new = QAJudgement(
                **common,
                metric="faithfulness",
                score=faithfulness_score(verdicts),
                claims=verdicts,
                model=judge.model,
                prompt_version=FAITHFULNESS_PROMPT,
                labeled_at=now(),
            )
            append_judgement(path, new)
            records.append(new)
            made += 1
            logger.info(
                "%s faithfulness %s (%s, %d claims)",
                question.id,
                new.score,
                judge.model,
                len(verdicts),
            )
    return made


def run_judge(args: argparse.Namespace, settings: Settings, questions: list[QAQuestion]) -> str:
    """The `judge` step: grade the answers with each judge; returns a summary."""
    answers = read_answers(answers_path(settings, args.name))
    if not answers:
        raise SystemExit(f"No answers for {args.name!r} yet (run the answer step).")
    lines = []
    for model in args.judges:
        started = time.monotonic()
        with get_client(model, settings) as client:
            judge = QAJudge(client)
            made = judge_all(
                judge, questions, answers, args.name, args.judgements, lambda: datetime.now(UTC)
            )
        lines.append(f"{model}: {made} judgements in {time.monotonic() - started:.0f} s")
    return "Judged. " + "; ".join(lines)


# --- Step 3: report -----------------------------------------------------------------------

ENTITY_NAMES = """
MATCH (n:Entity) WHERE n.wikidata_id IN $ids
RETURN n.wikidata_id AS qid, n.name AS name, coalesce(n.aliases, []) AS aliases
"""
EVENT_TITLES = "MATCH (ev:Event) WHERE ev.key IN $keys RETURN ev.key AS key, ev.title AS title"
SNAPSHOT_COUNTS = """
MATCH (n) UNWIND labels(n) AS label
RETURN label, count(*) AS count ORDER BY label
"""


def lookups(
    snapshot: Snapshot, questions: Sequence[QAQuestion]
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Names and aliases of the expected entities, titles of the expected events."""
    ids = sorted({qid for q in questions for qid in q.expected_entities})
    keys = sorted({key for q in questions for key in q.expected_events})
    names: dict[str, list[str]] = {}
    for row in snapshot.rows(ENTITY_NAMES, ids=ids):
        names.setdefault(row["qid"], [])
        names[row["qid"]] += [row["name"], *row["aliases"]]
    titles = {row["key"]: row["title"] for row in snapshot.rows(EVENT_TITLES, keys=keys)}
    return {qid: list(dict.fromkeys(n)) for qid, n in names.items()}, titles


def report_config(
    args: argparse.Namespace, settings: Settings, snapshot: Snapshot, answers: list[AnswerRecord]
) -> dict[str, Any]:
    """The settings and snapshot behind a QA report."""
    retrieval = RetrievalConfig.from_settings(settings)
    counts = {row["label"]: row["count"] for row in snapshot.rows(SNAPSHOT_COUNTS)}
    models = sorted({a.model for a in answers if a.model})
    return {
        "snapshot": args.neo4j_uri or settings.eval_qa_neo4j_uri,
        "snapshot_now": snapshot.clock().isoformat(),
        "snapshot_nodes": counts,
        "questions": str(args.questions),
        "qa_model": models or [args.model or settings.qa_model],
        "prompts": [CYPHER_PROMPT, ANSWER_PROMPT, CORRECTNESS_PROMPT, FAITHFULNESS_PROMPT],
        "channels": sorted(args.channels),
        "retrieval": {
            "max_rows": retrieval.max_rows,
            "top_k": retrieval.top_k,
            "min_cosine": retrieval.min_cosine,
            "max_facts": retrieval.max_facts,
        },
        **git_commit(),
        "note": args.note,
    }


def run_report(args: argparse.Namespace, settings: Settings, questions: list[QAQuestion]) -> str:
    """The `report` step: score and write the report; returns a summary."""
    answers = read_answers(answers_path(settings, args.name))
    if not answers:
        raise SystemExit(f"No answers for {args.name!r} yet (run the answer step).")
    snapshot = Snapshot.connect(args.neo4j_uri or settings.eval_qa_neo4j_uri, settings)
    try:
        names, titles = lookups(snapshot, questions)
        config = report_config(args, settings, snapshot, list(answers.values()))
    finally:
        snapshot.close()
    report = build_report(
        args.name,
        questions,
        answers,
        load_judgements(args.judgements),
        args.judges,
        names,
        titles,
        config,
    )
    stem = args.results_dir / f"{datetime.now(UTC):%Y-%m-%d}_{args.name}"
    stem.parent.mkdir(parents=True, exist_ok=True)
    json_path, md_path = stem.with_suffix(".json"), stem.with_suffix(".md")
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n"
    json_path.write_text(text, encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return f"{summary_line(report)}\nReport: {md_path}"


# --- Your own ratings ---------------------------------------------------------------------


def rate_manually(
    questions: Sequence[QAQuestion],
    answers: dict[str, AnswerRecord],
    run: str,
    path: Path,
    judge: str,
    limit: int,
    now: Callable[[], datetime],
    ask: Callable[[str], str] = input,
    show: Callable[[str], None] = print,
) -> tuple[int, int, int]:
    """Rate answers' correctness in the terminal, like the judge does. Returns (rated,
    compared with the judge, agreed). The judge's score is shown after yours."""
    records = load_judgements(path)
    rated = compared = agreed = 0
    for question in questions:
        record = answers.get(question.id)
        if rated >= limit or record is None or record.error:
            continue
        hash_ = answer_hash(record.answered, record.fact_text or record.text)
        reference = reference_hash(question)
        if find_judgement(
            records, run, question.id, hash_, "correctness", "human", None, reference
        ):
            continue
        status = "answerable" if question.answerable else "NOT answerable"
        shown = judge_text(record) if record.answered else f"(refused) {judge_text(record)}"
        show(
            f"\n{'=' * 78}\n{question.id} ({question.type}) {question.question}\n\n"
            f"Expected ({status}): {question.expected_answer}\n\nAnswer: {shown}\n"
        )
        reply = ""
        while reply not in {"0", "1", "2", "k", "q"}:
            reply = ask("Correctness 0 / 1 / 2, skip (k), quit (q)? ").strip().lower()
        if reply == "q":
            break
        if reply == "k":
            continue
        note = ask("Optional note: ").strip()
        mine = QAJudgement(
            run=run,
            question_id=question.id,
            answer_hash=hash_,
            reference_hash=reference,
            metric="correctness",
            score=int(reply),
            labeler="human",
            reason=note,
            labeled_at=now(),
        )
        append_judgement(path, mine)
        records.append(mine)
        rated += 1
        theirs = find_judgement(
            records, run, question.id, hash_, "correctness", judge, CORRECTNESS_PROMPT, reference
        )
        if theirs is not None and theirs.score is not None:
            compared += 1
            agreed += int(theirs.score) == int(reply)
            show(f"You: {reply}. {judge}: {int(theirs.score)} ({theirs.reason})")
    if compared:
        show(f"\nYou agreed with {judge} on {agreed} of {compared} answers.")
    return rated, compared, agreed


# --- Command line -------------------------------------------------------------------------


def parse_list(text: str) -> list[str]:
    """A comma-separated list (argparse type)."""
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_channels(text: str) -> list[Channel]:
    """A comma-separated list of retrieval channels (argparse type)."""
    channels = parse_list(text)
    unknown = set(channels) - set(ALL_CHANNELS)
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown channels: {', '.join(sorted(unknown))}")
    return channels  # type: ignore[return-value]


def parse_args(
    argv: list[str] | None = None, settings: Settings | None = None
) -> argparse.Namespace:
    settings = settings or get_settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True, help="name of this evaluation run")
    parser.add_argument("--steps", type=parse_list, default=list(STEPS), help="answer,judge,report")
    parser.add_argument(
        "--judges",
        type=parse_list,
        default=parse_list(settings.eval_qa_judges),
        help="backend:model,... (default: EVAL_QA_JUDGES); the first gives the scores",
    )
    parser.add_argument("--model", help="QA model as backend:model (default: QA_MODEL)")
    parser.add_argument(
        "--channels", type=parse_channels, default=sorted(ALL_CHANNELS), help="graph,cypher,vector"
    )
    parser.add_argument("--neo4j-uri", help="the snapshot (default: EVAL_QA_NEO4J_URI)")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS_PATH)
    parser.add_argument("--judgements", type=Path, default=DEFAULT_JUDGEMENTS_PATH)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--note", default="")
    parser.add_argument("--force", action="store_true", help="answer every question again")
    parser.add_argument("--rate", action="store_true", help="rate the answers yourself")
    parser.add_argument("--limit", type=int, default=15, help="answers to rate with --rate")
    args = parser.parse_args(argv)
    unknown = set(args.steps) - set(STEPS)
    if unknown:
        parser.error(f"unknown steps: {', '.join(sorted(unknown))}")
    return args


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    args = parse_args(argv, settings)
    setup_logging()
    for noisy in ("httpx", "neo4j", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    questions = load_questions(args.questions)
    if not questions:
        raise SystemExit(f"No questions in {args.questions}.")
    if args.rate:
        answers = read_answers(answers_path(settings, args.name))
        rated, _, _ = rate_manually(
            questions,
            answers,
            args.name,
            args.judgements,
            args.judges[0],
            args.limit,
            lambda: datetime.now(UTC),
        )
        print(f"Rated {rated} answers. Run `--steps report` to see the agreement.")
        return 0
    for step, run in (("answer", run_answer), ("judge", run_judge), ("report", run_report)):
        if step in args.steps:
            print(run(args, settings, questions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
