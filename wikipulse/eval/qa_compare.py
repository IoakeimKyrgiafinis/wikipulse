"""Compare QA evaluation runs on the same questions: before and after a change.

    uv run python -m wikipulse.eval.qa_compare --name 12a-cypher \\
        evals/results/2026-11-08_05b-v1.json \\
        evals/results/2026-11-16_12a-before.json \\
        evals/results/2026-11-16_12a-after.json

The reports are the JSON files of `wikipulse.eval.qa`. The last two are "before" and
"after"; any earlier ones are shown for reference (for spec 12a: the committed 05b run).

Twenty-five questions are few: one answer more or less right moves correctness by 0.08
and "fully correct" by 4 points. So every change comes with a **margin**: a paired
bootstrap over the questions (the same resampled questions for both runs, many times)
gives the 95% interval of after − before. An interval that includes 0 means the change
can't be told apart from the luck of which questions were asked. It says nothing about
run-to-run noise of the models; comparing a rerun with its original does that.
"""

import argparse
import json
import random
import statistics
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wikipulse.eval.qa_report import num

DEFAULT_RESULTS_DIR = Path("evals") / "results"
RESAMPLES = 2000
SEED = 12

Row = dict[str, Any]  # one question of a report


def _primary(report: dict[str, Any]) -> str:
    return report["config"]["primary_judge"]


def _correctness(report: dict[str, Any]) -> Callable[[Row], float | None]:
    judge = _primary(report)
    return lambda q: q["correctness"].get(judge)


def _fully_correct(report: dict[str, Any]) -> Callable[[Row], float | None]:
    judge = _primary(report)
    return lambda q: (
        None if q["correctness"].get(judge) is None else float(q["correctness"][judge] == 2)
    )


def _false_refusal(q: Row) -> float | None:
    return float(not q["answered"]) if q["answerable"] else None


def _correct_refusal(q: Row) -> float | None:
    return None if q["answerable"] else float(not q["answered"])


def _failed_query(q: Row) -> float | None:
    outcome = q.get("cypher_outcome")
    return None if outcome is None else float(outcome == "failed")


def _seconds(q: Row) -> float | None:
    return q["seconds"].get("total")


def mean_of(rows: Sequence[Row], value: Callable[[Row], float | None]) -> float | None:
    """The mean of `value` over the rows where it is defined."""
    values = [v for v in (value(q) for q in rows) if v is not None]
    return sum(values) / len(values) if values else None


def claim_share(rows: Sequence[Row]) -> float | None:
    """Supported claims / judged claims (the report's faithfulness)."""
    judged = sum(q.get("claims_judged", 0) for q in rows)
    return sum(q.get("claims_supported", 0) for q in rows) / judged if judged else None


def median_of(rows: Sequence[Row], value: Callable[[Row], float | None]) -> float | None:
    """The median of `value` over the rows where it is defined."""
    values = [v for v in (value(q) for q in rows) if v is not None]
    return statistics.median(values) if values else None


def count_of(rows: Sequence[Row], value: Callable[[Row], float | None]) -> float | None:
    """How many rows have a true `value` (None when no row has one)."""
    values = [v for v in (value(q) for q in rows) if v is not None]
    return float(sum(values)) if values else None


# name, label, aggregate(rows, report) -> value, format
METRICS: list[tuple[str, str, Callable[[Sequence[Row], dict], float | None], str]] = [
    ("correctness", "Correctness (0-2)", lambda r, rep: mean_of(r, _correctness(rep)), "num"),
    ("fully_correct", "Fully correct", lambda r, rep: mean_of(r, _fully_correct(rep)), "pct"),
    ("faithfulness", "Faithfulness (claims)", lambda r, rep: claim_share(r), "pct"),
    (
        "entity_recall",
        "Entity recall",
        lambda r, rep: mean_of(r, lambda q: q["entity_recall"]),
        "pct",
    ),
    ("event_recall", "Event recall", lambda r, rep: mean_of(r, lambda q: q["event_recall"]), "pct"),
    ("correct_refusals", "Correct refusals", lambda r, rep: mean_of(r, _correct_refusal), "pct"),
    ("false_refusals", "False refusals", lambda r, rep: mean_of(r, _false_refusal), "pct"),
    (
        "failed_queries",
        "Cypher queries that never ran",
        lambda r, rep: count_of(r, _failed_query),
        "int",
    ),
    ("latency_median_s", "Latency median (s)", lambda r, rep: median_of(r, _seconds), "sec"),
]


def show(value: float | None, kind: str, signed: bool = False) -> str:
    """A metric value for the table."""
    if value is None:
        return "n/a"
    sign = "+" if signed and value > 0 else ""
    if kind == "pct":
        return f"{sign}{100 * value:.1f}" + ("" if signed else "%")
    if kind == "int":
        return f"{sign}{value:.0f}"
    if kind == "sec":
        return f"{sign}{value:.1f}"
    return f"{sign}{num(value)}"


def paired_rows(before: dict[str, Any], after: dict[str, Any]) -> tuple[list[Row], list[Row]]:
    """The questions both reports have, in the same order."""
    by_id = {q["id"]: q for q in after["questions"]}
    common = [q for q in before["questions"] if q["id"] in by_id]
    return common, [by_id[q["id"]] for q in common]


def bootstrap_interval(
    before: dict[str, Any],
    after: dict[str, Any],
    aggregate: Callable[[Sequence[Row], dict], float | None],
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> tuple[float, float] | None:
    """The 95% interval of after − before over questions resampled with replacement
    (the same questions for both runs: a paired bootstrap)."""
    rows_before, rows_after = paired_rows(before, after)
    if not rows_before:
        return None
    rng = random.Random(seed)
    n = len(rows_before)
    differences = []
    for _ in range(resamples):
        picks = [rng.randrange(n) for _ in range(n)]
        a = aggregate([rows_after[i] for i in picks], after)
        b = aggregate([rows_before[i] for i in picks], before)
        if a is not None and b is not None:
            differences.append(a - b)
    if not differences:
        return None
    differences.sort()
    low = differences[int(0.025 * (len(differences) - 1))]
    high = differences[int(0.975 * (len(differences) - 1))]
    return low, high


def summary_value(
    report: dict[str, Any], key: str, aggregate: Callable[[Sequence[Row], dict], float | None]
) -> float | None:
    """A metric of a whole report, from its questions; a report written before its
    questions carried the data (e.g. claim counts) gives its own summary number instead.
    The bootstrap margin needs the questions, so it stays n/a for such a report."""
    value = aggregate(report["questions"], report)
    if value is None:
        value = report.get("metrics", {}).get(key)
    return value


def compare(reports: Sequence[dict[str, Any]], name: str) -> dict[str, Any]:
    """Every metric for every report, and after − before with its bootstrap margin."""
    before, after = reports[-2], reports[-1]
    metrics = []
    for key, label, aggregate, kind in METRICS:
        values = [summary_value(r, key, aggregate) for r in reports]
        change = None if None in values[-2:] else values[-1] - values[-2]
        metrics.append(
            {
                "metric": key,
                "label": label,
                "kind": kind,
                "values": values,
                "change": change,
                "interval": bootstrap_interval(before, after, aggregate),
            }
        )
    return {
        "name": name,
        "runs": [r["name"] for r in reports],
        "configs": [r["config"] for r in reports],
        "metrics": metrics,
        "questions": question_changes(reports),
        "resamples": RESAMPLES,
        "seed": SEED,
    }


def question_changes(reports: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per question: the primary judge's correctness and the query's fate in each run."""
    rows: dict[str, dict[str, Any]] = {}
    for report in reports:
        judge = _primary(report)
        for q in report["questions"]:
            row = rows.setdefault(
                q["id"],
                {"id": q["id"], "type": q["type"], "question": q["question"], "runs": []},
            )
            row["runs"].append(
                {
                    "correctness": q["correctness"].get(judge),
                    "answered": q["answered"],
                    "cypher": q.get("cypher_outcome") or "?",
                }
            )
    return list(rows.values())


def render_markdown(comparison: dict[str, Any]) -> str:
    """The comparison as Markdown."""
    runs = comparison["runs"]
    lines = [
        f"# QA evaluation comparison: {comparison['name']}",
        "",
        *([comparison["note"], ""] if comparison.get("note") else []),
        f"Runs: {', '.join(f'`{r}`' for r in runs)}. Before = `{runs[-2]}`, after = "
        f"`{runs[-1]}`. Margin: 95% interval of after − before from a paired bootstrap over "
        f"the questions ({comparison['resamples']} resamples, seed {comparison['seed']}); an "
        "interval that includes 0 is within noise. Percentage changes are in points.",
        "",
        "| Metric | " + " | ".join(runs) + " | Change | Margin (95%) |",
        "|---|" + "---:|" * (len(runs) + 2),
    ]
    for m in comparison["metrics"]:
        interval = m["interval"]
        margin = (
            "n/a"
            if interval is None
            else f"{show(interval[0], m['kind'], True)} to {show(interval[1], m['kind'], True)}"
        )
        cells = " | ".join(show(v, m["kind"]) for v in m["values"])
        lines.append(
            f"| {m['label']} | {cells} | {show(m['change'], m['kind'], True)} | {margin} |"
        )
    lines += ["", "## Configurations", ""]
    for run, config in zip(runs, comparison["configs"], strict=True):
        prompts = ", ".join(config.get("prompts", []))
        retrieval = config.get("retrieval", {})
        lines.append(
            f"- `{run}`: prompts {prompts}; Cypher repairs "
            f"{retrieval.get('cypher_repairs', 'none (before 12a)')}; commit "
            f"{str(config.get('commit', ''))[:10]}; {config.get('note', '')}"
        )
    lines += [
        "",
        "## Questions",
        "",
        "Correctness by the primary judge in each run (r = refused); generated Cypher: ran, "
        "repaired (ran after one repair), failed (never ran), none (no query).",
        "",
        "| Id | Type | Question | " + " | ".join(runs) + " |",
        "|---|---|---|" + "---|" * len(runs),
    ]
    for q in comparison["questions"]:
        cells = []
        for run in q["runs"]:
            score = "–" if run["correctness"] is None else str(run["correctness"])
            refused = "" if run["answered"] else " r"
            cells.append(f"{score}{refused}, {run['cypher']}")
        question = q["question"] if len(q["question"]) <= 60 else q["question"][:59] + "…"
        lines.append(f"| {q['id']} | {q['type']} | {question} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", nargs="+", type=Path, help="report JSON files, oldest first")
    parser.add_argument("--name", required=True, help="name of the comparison")
    parser.add_argument("--note", default="", help="a remark printed under the title")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    args = parser.parse_args(argv)
    if len(args.reports) < 2:
        parser.error("give at least two reports (before and after)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in args.reports]
    comparison = compare(reports, args.name)
    comparison["note"] = args.note
    stem = args.results_dir / f"{datetime.now(UTC):%Y-%m-%d}_{args.name}"
    stem.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(comparison, indent=2, ensure_ascii=False) + "\n"
    stem.with_suffix(".json").write_text(text, encoding="utf-8", newline="\n")
    markdown = render_markdown(comparison)
    stem.with_suffix(".md").write_text(markdown, encoding="utf-8", newline="\n")
    print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
