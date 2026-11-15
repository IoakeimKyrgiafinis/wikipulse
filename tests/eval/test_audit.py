"""Tests of the label audit (spec 12b): agreement numbers, pairing, the report, read-only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wikipulse.config import Settings
from wikipulse.eval import audit
from wikipulse.eval.audit import Comparison, Paired
from wikipulse.eval.grouping_labels import PairLabelRecord
from wikipulse.eval.label_store import LabelRecord
from wikipulse.eval.qa_judge import ClaimVerdict, QAJudgement

T0 = datetime(2026, 11, 5, 12, tzinfo=UTC)
LABELED_AT = datetime(2026, 11, 15, tzinfo=UTC)


def spike_label(
    title: str,
    label: str,
    labeler: str,
    run_id: str = "02b-baseline",
    start_h: float = 0,
    model: str | None = "ollama:qwen3:4b",
) -> LabelRecord:
    first = T0 + timedelta(hours=start_h)
    return LabelRecord(
        title=title,
        run_id=run_id,
        started_at=first + timedelta(minutes=5),
        first_edit_at=first,
        end_at=first + timedelta(hours=1),
        label=label,
        labeler=labeler,
        model=model if labeler == "llm" else None,
        labeled_at=LABELED_AT,
    )


def pair_label(a: str, b: str, label: str, labeler: str, version: int = 2) -> PairLabelRecord:
    return PairLabelRecord(
        title_a=a,
        started_a=T0,
        title_b=b,
        started_b=T0 + timedelta(hours=1),
        label=label,
        labeler=labeler,
        model="ollama:qwen3:4b" if labeler == "llm" else None,
        prompt_version=version if labeler == "llm" else None,
        spike_run_id="02b-loose",
        sampled_for="03b-default-fix",
        labeled_at=LABELED_AT,
    )


def judgement(
    question: str, labeler: str, score: float | None, metric: str = "correctness", **extra: Any
) -> QAJudgement:
    return QAJudgement(
        run="05b-v1",
        question_id=question,
        answer_hash="abc",
        metric=metric,
        score=score,
        labeler=labeler,
        model="ollama:qwen3:8b" if labeler == "llm" else None,
        prompt_version=f"{metric}_v1" if labeler == "llm" else None,
        labeled_at=LABELED_AT,
        **extra,
    )


# --- Intervals and agreement --------------------------------------------------------


def test_wilson_interval_is_sane_at_the_edges() -> None:
    assert audit.wilson(0, 0) is None
    low, high = audit.wilson(0, 10)
    assert low == pytest.approx(0.0) and high == pytest.approx(0.2775, abs=1e-3)
    low, high = audit.wilson(10, 10)
    assert high == pytest.approx(1.0) and low == pytest.approx(0.7225, abs=1e-3)


def test_bootstrap_is_reproducible_and_brackets_the_value() -> None:
    items = [True] * 30 + [False] * 20
    share = lambda s: sum(s) / len(s)  # noqa: E731
    first = audit.bootstrap(items, share, 500, seed=1)
    assert first == audit.bootstrap(items, share, 500, seed=1)
    assert first.low < 0.6 < first.high
    assert audit.bootstrap([], share, 500, seed=1).as_list() == [None, None]


def test_kappa_perfect_chance_and_undefined() -> None:
    perfect = [Paired("a", "news", "news"), Paired("b", "not news", "not news")]
    assert audit.kappa(perfect, ("news", "not news"), weighted=False) == pytest.approx(1.0)
    # The LLM says news to everything: it agrees with half, but no better than chance.
    chance = [Paired(str(i), "news" if i % 2 else "not news", "news") for i in range(10)]
    assert audit.kappa(chance, ("news", "not news"), weighted=False) == pytest.approx(0.0)
    # Both sides used one and the same category: kappa is 0/0, not 1.
    same = [Paired("a", "news", "news"), Paired("b", "news", "news")]
    assert audit.kappa(same, ("news", "not news"), weighted=False) is None


def test_unsure_is_counted_and_left_out_on_both_sides() -> None:
    comparison = Comparison("spikes", "run", "llm", ("news", "not news"), positive="news")
    comparison.pairs = [
        Paired("a", "news", "news"),
        Paired("b", "not news", "news"),
        Paired("c", "unsure", "news"),
        Paired("d", "news", "unsure"),
    ]
    row = audit.agreement(comparison, resamples=200, seed=1)
    assert (row["paired"], row["decisive"]) == (4, 2)
    assert (row["human_unsure"], row["llm_unsure"]) == (1, 1)
    assert row["accuracy"] == 0.5
    # rows: human news / not news / unsure; columns: LLM in the same order
    assert row["confusion"] == [[1, 0, 1], [1, 0, 0], [1, 0, 0]]
    assert row["disagreements"] == [{"item": "b", "human": "not news", "llm": "news"}]
    assert row["conclusion"].startswith("too few")


def test_no_pairs_says_no_human_labels_yet() -> None:
    row = audit.agreement(Comparison("gate", "g", "llm", ("news", "not news")), 100, 1)
    assert row["decisive"] == 0 and row["accuracy"] is None and row["kappa"] is None
    assert row["conclusion"] == "no human labels yet"


def test_rare_category_is_flagged() -> None:
    pairs = [Paired(str(i), "correct", "correct") for i in range(48)]
    pairs += [Paired("x", "wrong", "correct"), Paired("y", "correct", "wrong")]
    assert audit.rare_category(pairs, "correct")
    assert not audit.rare_category(pairs[:2] + [Paired("z", "wrong", "wrong")], "correct")


# --- Pairing ------------------------------------------------------------------------


def test_spike_pairing_overlaps_prefers_the_same_run_and_counts_unpaired() -> None:
    records = [
        spike_label("A", "news", "llm", run_id="02b-loose"),
        spike_label("A", "not news", "llm"),  # same run as the human label: wins
        spike_label("B", "news", "llm", start_h=10),  # no overlap with the human's B
        spike_label("A", "not news", "human"),
        spike_label("B", "news", "human"),
    ]
    [comparison] = audit.spike_comparisons(records)
    assert comparison.scope == "02b-baseline" and comparison.labeler == "ollama:qwen3:4b"
    assert [(p.human, p.llm) for p in comparison.pairs] == [("not news", "not news")]
    assert comparison.unpaired == 1


def test_latest_human_label_of_a_spike_counts() -> None:
    records = [
        spike_label("A", "news", "llm"),
        spike_label("A", "not news", "human"),
        spike_label("A", "news", "human"),
    ]
    [comparison] = audit.spike_comparisons(records)
    assert [p.human for p in comparison.pairs] == ["news"]


def test_gate_labels_pair_with_human_spike_labels_of_the_same_burst() -> None:
    gate = [
        spike_label("A", "news", "llm", run_id="09c-gate", model="ollama:qwen3:8b"),
        spike_label("B", "news", "llm", run_id="09c-gate", model="ollama:qwen3:8b"),
    ]
    spikes = [spike_label("A", "not news", "human", run_id="07a-candidates")]
    [comparison] = audit.gate_comparisons(gate, spikes)
    assert comparison.family == "gate"
    assert [(p.human, p.llm) for p in comparison.pairs] == [("not news", "news")]


def test_pair_labels_compare_per_prompt_version() -> None:
    records = [
        pair_label("A", "B", "different", "llm", version=1),
        pair_label("A", "B", "same", "llm", version=2),
        pair_label("A", "B", "same", "human"),
    ]
    comparisons = audit.keyed_comparisons(
        "grouping", records, ("same", "different"), "same",
        lambda r: r.sampled_for, audit.pair_item,
    )  # fmt: skip
    found = {c.labeler: [(p.human, p.llm) for p in c.pairs] for c in comparisons}
    assert found == {
        "ollama:qwen3:4b, prompt 1": [("same", "different")],
        "ollama:qwen3:4b, prompt 2": [("same", "same")],
    }


def test_qa_correctness_is_weighted_and_a_missing_score_is_unsure() -> None:
    records = [
        judgement("q1", "llm", 2.0),
        judgement("q1", "human", 1.0),
        judgement("q2", "llm", 0.0),
        judgement("q2", "human", None),
    ]
    [comparison] = audit.qa_comparisons(records)
    assert comparison.weighted and comparison.scope == "05b-v1 (correctness)"
    assert [(p.human, p.llm) for p in comparison.pairs] == [(1, 2), ("unsure", 0)]


def test_qa_faithfulness_pairs_claims_by_position() -> None:
    def claims(*verdicts: bool | None) -> list[ClaimVerdict]:
        return [ClaimVerdict(claim=f"c{n}", supported=v) for n, v in enumerate(verdicts)]

    records = [
        judgement("q1", "llm", 0.5, "faithfulness", claims=claims(True, False)),
        judgement("q1", "human", 0.5, "faithfulness", claims=claims(True, None)),
    ]
    [comparison] = audit.qa_comparisons(records)
    assert [(p.human, p.llm) for p in comparison.pairs] == [(True, True), ("unsure", False)]


# --- Headline numbers ---------------------------------------------------------------


def detection_report(titles: list[str]) -> dict[str, Any]:
    spikes = []
    for n, title in enumerate(titles):
        first = T0 + timedelta(hours=n)
        spikes.append(
            {
                "title": title,
                "first_edit_at": first.isoformat(),
                "started_at": (first + timedelta(minutes=5)).isoformat(),
                "end_at": (first + timedelta(hours=1)).isoformat(),
            }
        )
    return {
        "name": "02b-baseline",
        "metrics": {"label_sample": len(titles), "label_seed": 2026, "label_precision": 0.75},
        "spikes": spikes,
    }


def test_detection_headline_puts_llm_and_human_precision_side_by_side() -> None:
    report = detection_report(["A", "B", "C", "D"])
    report["_file"] = "evals/results/x_02b-baseline.json"
    records = [
        spike_label("A", "news", "llm", start_h=0),
        spike_label("B", "news", "llm", start_h=1),
        spike_label("C", "news", "llm", start_h=2),
        spike_label("D", "not news", "llm", start_h=3),
        spike_label("A", "news", "human", start_h=0),
        spike_label("B", "not news", "human", start_h=1),
        spike_label("C", "unsure", "human", start_h=2),
    ]
    row = audit.detection_headline(report, records)
    assert row["llm"]["value"] == 0.75 and row["llm"]["n"] == 4 and row["llm_matches_report"]
    assert row["human"]["value"] == 0.5 and row["human"]["n"] == 2
    assert row["human"]["unsure"] == 1
    assert row["llm_on_human_sample"]["n"] == 2  # A and B: the human-decided spikes
    assert row["llm_on_human_sample"]["value"] == 1.0


# --- The report and the command -----------------------------------------------------


def write_lines(path: Path, records: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8")


def settings(**changes: Any) -> Settings:
    return Settings(
        _env_file=None,
        **{"anthropic_api_key": None, "judge_audit_allow_paid": False, **changes},
    )


def test_paid_judge_needs_both_the_key_and_the_opt_in() -> None:
    assert audit.paid_judge_status(settings())["reason"].startswith("skipped")
    only_key = audit.paid_judge_status(settings(anthropic_api_key="x"))
    assert "JUDGE_AUDIT_ALLOW_PAID" in only_key["reason"] and not only_key["used"]
    both = audit.paid_judge_status(settings(anthropic_api_key="x", judge_audit_allow_paid=True))
    assert both["allowed"] and not both["used"]


def test_report_covers_every_family_and_never_writes_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    labels, results = tmp_path / "labels", tmp_path / "results"
    write_lines(labels / "spike_labels.jsonl", [
        spike_label("A", "news", "llm"), spike_label("A", "not news", "human"),
    ])  # fmt: skip
    results.mkdir()
    report = detection_report(["A"])
    report["metrics"]["label_precision"] = 1.0
    (results / "2026-11-07_02b-baseline.json").write_text(json.dumps(report), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in labels.iterdir()}

    monkeypatch.setattr(audit, "get_settings", settings)
    argv = ["--labels-dir", str(labels), "--results-dir", str(results), "--date", "2026-11-15"]
    assert audit.main([*argv, "--resamples", "50"]) == 0

    assert {p.name: p.read_bytes() for p in labels.iterdir()} == before  # read-only
    data = json.loads((results / "2026-11-15_audit.json").read_text(encoding="utf-8"))
    counts = {c["family"]: c for c in data["counts"]}
    assert set(counts) == set(audit.FILES)
    assert counts["spikes"]["human"] == 1 and counts["gate"]["records"] == 0
    assert counts["relations"]["human_by_value"] == {"correct": 0, "wrong": 0, "unsure": 0}
    [row] = data["agreement"]
    assert (row["family"], row["decisive"], row["accuracy"]) == ("spikes", 1, 0.0)
    [headline] = data["headlines"]["detection"]
    assert headline["llm"]["value"] == 1.0 and headline["human"]["value"] == 0.0
    text = (results / "2026-11-15_audit.md").read_text(encoding="utf-8")
    assert "Families without any human-LLM pair: gate, grouping, relations, qa." in text
    assert "Paid judge: skipped" in text
