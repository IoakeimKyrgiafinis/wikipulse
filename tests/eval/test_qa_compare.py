"""Comparing QA evaluation runs: metrics side by side, change and bootstrap margin."""

import json
from pathlib import Path

import pytest

from wikipulse.eval.qa_compare import (
    bootstrap_interval,
    compare,
    main,
    paired_rows,
    render_markdown,
    show,
)

JUDGE = "fake:a"


def row(qid: str, score: int | None, answered: bool = True, **changes) -> dict:
    values = {
        "id": qid,
        "type": "time",
        "question": f"Question {qid}?",
        "answerable": True,
        "answered": answered,
        "entity_recall": None,
        "event_recall": 1.0,
        "correctness": {} if score is None else {JUDGE: score},
        "claims_supported": 1 if answered else 0,
        "claims_judged": 1 if answered else 0,
        "seconds": {"total": 2.0},
        "cypher_outcome": "ran",
    }
    return {**values, **changes}


def report(name: str, rows: list[dict]) -> dict:
    return {"name": name, "config": {"primary_judge": JUDGE, "prompts": ["p"]}, "questions": rows}


BEFORE = report(
    "before",
    [
        row("q1", 0, answered=False, cypher_outcome="failed"),
        row("q2", 2),
        row("q3", 1),
        row("q4", 2, answerable=False, answered=False, cypher_outcome="none"),
    ],
)
AFTER = report(
    "after",
    [
        row("q1", 2),
        row("q2", 2),
        row("q3", 1, cypher_outcome="repaired"),
        row("q4", 0, answerable=False),
    ],
)


def metric(comparison: dict, key: str) -> dict:
    return next(m for m in comparison["metrics"] if m["metric"] == key)


def test_compare_gives_every_metric_for_every_run_and_the_change() -> None:
    comparison = compare([BEFORE, AFTER], "test")
    assert comparison["runs"] == ["before", "after"]
    correctness = metric(comparison, "correctness")
    assert correctness["values"] == [1.25, 1.25] and correctness["change"] == 0
    assert metric(comparison, "fully_correct")["values"] == [0.5, 0.5]
    assert metric(comparison, "false_refusals")["values"] == [pytest.approx(1 / 3), 0.0]
    assert metric(comparison, "correct_refusals")["values"] == [1.0, 0.0]
    assert metric(comparison, "failed_queries")["values"] == [1.0, 0.0]
    assert metric(comparison, "faithfulness")["values"] == [1.0, 1.0]
    assert metric(comparison, "entity_recall")["values"] == [None, None]
    assert comparison["questions"][0]["runs"] == [
        {"correctness": 0, "answered": False, "cypher": "failed"},
        {"correctness": 2, "answered": True, "cypher": "ran"},
    ]


def test_the_bootstrap_is_paired_repeatable_and_brackets_the_change() -> None:
    aggregate = metric(compare([BEFORE, AFTER], "t"), "false_refusals")
    interval = bootstrap_interval(
        BEFORE, AFTER, lambda rows, rep: sum(not r["answered"] for r in rows) / len(rows)
    )
    assert interval == bootstrap_interval(
        BEFORE, AFTER, lambda rows, rep: sum(not r["answered"] for r in rows) / len(rows)
    )
    assert interval is not None and interval[0] <= -0.25 <= interval[1] <= 0
    assert aggregate["interval"][1] <= 0  # never worse after: q1 was refused, now answered
    # Identical runs: no change, and a margin of exactly zero.
    same = bootstrap_interval(BEFORE, BEFORE, lambda rows, rep: len(rows))
    assert same == (0, 0)


def test_only_questions_in_both_runs_are_paired() -> None:
    shorter = report("after", AFTER["questions"][:2])
    a, b = paired_rows(BEFORE, shorter)
    assert [q["id"] for q in a] == [q["id"] for q in b] == ["q1", "q2"]


def test_older_reports_without_query_outcomes_show_n_a() -> None:
    old = report(
        "05b", [{k: v for k, v in r.items() if k != "cypher_outcome"} for r in BEFORE["questions"]]
    )
    comparison = compare([old, BEFORE, AFTER], "t")
    assert metric(comparison, "failed_queries")["values"] == [None, 1.0, 0.0]
    text = render_markdown(comparison)
    assert "| Cypher queries that never ran | n/a | 1 | 0 | -1 |" in text
    assert "| q1 | time | Question q1? | 0 r, ? | 0 r, failed | 2, ran |" in text


def test_show_formats_values_and_changes() -> None:
    assert show(0.5, "pct") == "50.0%" and show(0.04, "pct", signed=True) == "+4.0"
    assert show(1.2, "num") == "1.20" and show(-0.08, "num", signed=True) == "-0.08"
    assert show(None, "pct") == "n/a" and show(3.0, "int") == "3"


def test_main_writes_markdown_and_json(tmp_path: Path) -> None:
    paths = []
    for data in (BEFORE, AFTER):
        path = tmp_path / f"{data['name']}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        paths.append(str(path))
    assert main([*paths, "--name", "cmp", "--results-dir", str(tmp_path / "out")]) == 0
    written = sorted(p.suffix for p in (tmp_path / "out").iterdir())
    assert written == [".json", ".md"]
    with pytest.raises(SystemExit):
        main([paths[0], "--name", "cmp"])
