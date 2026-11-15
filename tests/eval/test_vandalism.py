"""The vandalism analysis (spec 11c): revert matching, signals, AUC, label sets, rules."""

import itertools
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.eval.helpers import at, spike
from wikipulse.config import DetectorConfig
from wikipulse.eval import vandalism
from wikipulse.eval.label_store import LabelRecord
from wikipulse.ml.features import PageEdit

ROOT = Path(__file__).resolve().parents[2]
T = at(7, 1)


def edit(minute: float, user: str, comment: str = "", rev: int | None = None, bot: bool = False):
    return PageEdit(
        time=T + timedelta(minutes=minute), user=user, comment=comment, rev_new=rev, is_bot=bot
    )


def record(
    title: str, hour: float, label: str, labeler: str = "llm", run: str = "02b-baseline"
) -> LabelRecord:
    first = at(7, hour)
    return LabelRecord(
        title=title,
        run_id=run,
        started_at=first + timedelta(minutes=5),
        first_edit_at=first,
        end_at=first + timedelta(hours=1),
        label=label,
        labeler=labeler,
        model="ollama:test" if labeler == "llm" else None,
        labeled_at=datetime(2026, 11, 15, tzinfo=UTC),
    )


# --- Revert matching ---


def test_an_undo_finds_the_revision_it_undid() -> None:
    edits = [
        edit(0, "Alice", rev=101),
        edit(1, "~2026-1-1", rev=102),
        edit(2, "Bob", rev=103),
        edit(3, "Carol", "Undid revision 102 by ~2026-1-1", rev=104),
    ]
    assert vandalism.reverted_at(edits) == {1: edits[3].time}


def test_a_rollback_undoes_the_last_editors_run_of_edits() -> None:
    edits = [
        edit(0, "Alice"),
        edit(1, "Vandal"),
        edit(2, "Vandal"),
        edit(3, "Patroller", "Reverted edits by Vandal to last version by Alice"),
    ]
    assert set(vandalism.reverted_at(edits)) == {1, 2}


def test_reverted_n_edits_undoes_at_most_n() -> None:
    edits = [
        edit(0, "Vandal"),
        edit(1, "Vandal"),
        edit(2, "Vandal"),
        edit(3, "Patroller", "Reverted 2 edits by Vandal (talk): test"),
    ]
    assert set(vandalism.reverted_at(edits)) == {1, 2}


def test_the_reverters_own_edits_are_skipped() -> None:
    edits = [
        edit(0, "Vandal"),
        edit(1, "Patroller"),
        edit(2, "Patroller", "rvv"),
    ]
    assert set(vandalism.reverted_at(edits)) == {0}


# --- Signals ---


def test_period_signals_count_shares_and_editors_never_names() -> None:
    edits = [
        edit(0, "~2026-1-1"),
        edit(1, "Alice"),
        edit(2, "~2026-2-1"),
        edit(3, "Bob", "rv vandalism"),  # a revert: not a content edit
        edit(4, "Alice"),
        edit(5, "ClueBot", "Reverting possible vandalism", bot=True),
    ]
    reverted = vandalism.reverted_at(edits)
    period = (T, T + timedelta(minutes=10), True)
    values = vandalism.period_signals(edits, reverted, period, T + timedelta(minutes=10))
    assert values["anon_share"] == pytest.approx(2 / 4)
    assert values["anon_editors"] == 2 and values["registered_editors"] == 1
    assert values["revert_share"] == pytest.approx(1 / 6)  # the bot's summary isn't one
    assert values["reverted_share"] == pytest.approx(1 / 4)  # edit 2, by Bob's revert
    assert values["revert_minutes"] == pytest.approx(1.0)
    assert values["top_editor_share"] == pytest.approx(2 / 4)
    assert values["vandal_word_share"] == pytest.approx(1 / 5)
    assert all(isinstance(v, int | float) for v in values.values())


def test_a_revert_after_the_decision_moment_is_not_known_yet() -> None:
    edits = [edit(0, "Vandal"), edit(1, "Alice"), edit(30, "Bob", "Reverted edits by Alice")]
    reverted = vandalism.reverted_at(edits)
    period = (T, T + timedelta(minutes=10), True)
    early = vandalism.period_signals(edits, reverted, period, T + timedelta(minutes=10))
    late = vandalism.period_signals(edits, reverted, period, T + timedelta(minutes=40))
    assert early["reverted_share"] == 0 and late["reverted_share"] == pytest.approx(0.5)


def test_moments_use_the_window_the_start_rule_saw() -> None:
    config = DetectorConfig()
    short = vandalism.LabeledBurst("A", T, T + timedelta(minutes=4), T + timedelta(hours=1),
                                   "news", "human", None)  # fmt: skip
    long = vandalism.LabeledBurst("A", T, T + timedelta(minutes=40), T + timedelta(hours=1),
                                  "news", "human", None)  # fmt: skip
    (s_start, s_end, closed), known = vandalism.moments_of(short, config)["start"]
    assert (s_end - s_start, closed, known) == (timedelta(minutes=10), False, short.started_at)
    (l_start, l_end, _), _ = vandalism.moments_of(long, config)["start"]
    assert l_end - l_start == timedelta(minutes=60)
    (_, cutoff, _), _ = vandalism.moments_of(short, config)["cutoff"]
    assert cutoff == short.started_at + timedelta(minutes=10)


def test_a_burst_without_edits_in_the_lake_has_no_signals() -> None:
    burst = vandalism.LabeledBurst("A", T, T + timedelta(minutes=4), T + timedelta(hours=1),
                                   "news", "human", None)  # fmt: skip
    assert vandalism.burst_signals(burst, [], DetectorConfig()) is None


# --- AUC ---


def brute_auc(news: list[float], not_news: list[float]) -> float:
    pairs = list(itertools.product(not_news, news))
    return sum(1.0 if a > b else 0.5 if a == b else 0.0 for a, b in pairs) / len(pairs)


def test_auc_extremes_and_ties() -> None:
    assert vandalism.auc([0.0, 0.1], [0.5, 0.9]) == 1.0
    assert vandalism.auc([0.5, 0.9], [0.0, 0.1]) == 0.0
    assert vandalism.auc([0.0, 0.0], [0.0, 0.0]) == 0.5
    assert vandalism.auc([], [1.0]) is None
    assert vandalism.auc([0.0, math.nan], [1.0]) == 1.0


def test_auc_from_ranks_equals_counting_pairs() -> None:
    rng = random.Random(7)
    news = [rng.choice([0, 0.2, 0.4, 0.6]) for _ in range(40)]
    not_news = [rng.choice([0, 0.4, 0.6, 0.8, 1]) for _ in range(30)]
    assert vandalism.auc(news, not_news) == pytest.approx(brute_auc(news, not_news))


def test_the_verdict_needs_the_whole_interval_on_one_side() -> None:
    assert vandalism.verdict(0.55, 0.8) == "higher in not news"
    assert vandalism.verdict(0.2, 0.45) == "higher in news"
    assert vandalism.verdict(0.45, 0.6) == "no clear separation"
    assert vandalism.verdict(None, None) == "too few values"


# --- Label sets ---


def test_each_labeler_gets_its_own_latest_verdict_per_burst() -> None:
    records = [
        record("Page", 1, "news"),  # LLM, then the human, then a later LLM label
        record("Page", 1.5, "not news", labeler="human"),
        record("Page", 1.2, "not news", run="07a-default"),
        record("Other", 3, "unsure", labeler="human"),
    ]
    human, human_unsure = vandalism.labeled_bursts(records, "human")
    llm, llm_unsure = vandalism.labeled_bursts(records, "llm")
    assert [(b.title, b.label) for b in human] == [("Page", "not news")]
    assert human_unsure == 1
    assert [(b.title, b.label) for b in llm] == [("Page", "not news")]
    assert llm_unsure == 0


def test_separate_periods_of_one_article_are_separate_bursts() -> None:
    records = [record("Page", 1, "news"), record("Page", 5, "not news")]
    bursts, _ = vandalism.labeled_bursts(records, "llm")
    assert [b.label for b in bursts] == ["news", "not news"]


# --- Rules ---


def burst(title: str, hour: float, label: str) -> vandalism.LabeledBurst:
    first = at(7, hour)
    return vandalism.LabeledBurst(
        title, first, first + timedelta(minutes=5), first + timedelta(hours=1), label, "human",
        None,
    )  # fmt: skip


def test_rule_results_count_precision_and_dropped_bursts() -> None:
    bursts = [
        burst("News", 1, "news"),
        burst("Vandal", 2, "not news"),
        burst("Other", 3, "not news"),
    ]
    off = [spike("News", at(7, 1.1)), spike("Vandal", at(7, 2.1)), spike("Unknown", at(7, 4))]
    on = [spike("News", at(7, 1.1)), spike("Unknown", at(7, 4))]
    rows = vandalism.rule_results({1.0: off, 0.5: on}, {"human": bursts}, at(7, 0))
    before, after = (row["sets"]["human"] for row in rows)
    assert (before["news"], before["not news"], before["unlabeled"]) == (1, 1, 1)
    assert before["precision"] == pytest.approx(0.5) and after["precision"] == 1.0
    assert (after["news_found"], after["news_found_before"]) == (1, 1)
    assert (after["not_news_found"], after["not_news_found_before"]) == (0, 1)
    assert [d["title"] for d in after["dropped"]] == ["Vandal"]
    assert rows[1]["spikes"] == 2


def test_start_changes_count_kept_delayed_and_dropped_spikes() -> None:
    before = [spike("A", at(7, 1)), spike("B", at(7, 2)), spike("C", at(7, 3)), spike("D", at(6))]
    after = [spike("A", at(7, 1)), spike("B", at(7, 2) + timedelta(minutes=6))]
    changes = vandalism.start_changes(before, after, at(7))
    assert (changes["same_start"], changes["later_start"], changes["dropped"]) == (1, 1, 1)
    assert changes["later_median_min"] == pytest.approx(6.0)


def test_a_dropped_burst_shows_the_other_labelers_verdict() -> None:
    human = [burst("Vandal", 2, "not news")]
    llm = [burst("Vandal", 2.2, "news"), burst("Elsewhere", 5, "news")]
    off = [spike("Vandal", at(7, 2.1)), spike("Elsewhere", at(7, 5.1))]
    rows = vandalism.rule_results({1.0: off, 0.5: off[1:]}, {"human": human, "llm": llm}, at(7))
    dropped = rows[1]["sets"]["llm"]["dropped"]
    assert [(d["title"], d["label"], d["other_labels"]) for d in dropped] == [
        ("Vandal", "news", {"human": "not news"})
    ]


def test_gate_results_with_the_rule_off_reproduce_the_old_baseline() -> None:
    rows = vandalism.gate_results(ROOT / "tests/fixtures/detection_gate/edits.jsonl.gz",
                                  ROOT / "evals/labels/detection_gate_labels.jsonl")  # fmt: skip
    off = next(r for r in rows if r["max_anon_share"] == 1.0)
    assert (off["spikes"], off["counts"]["news"], off["found"], off["news_bursts"]) == (
        19, 10, 8, 38,
    )  # fmt: skip
    assert [r["max_anon_share"] for r in rows] == list(vandalism.RULES)


# --- The report ---


def test_the_markdown_report_has_every_section(tmp_path: Path) -> None:
    rows = [(burst("News", 1, "news"), {m: {s: 0.0 for s in vandalism.SIGNALS}
                                         for m in vandalism.MOMENTS})]  # fmt: skip
    rows.append((burst("Vandal", 2, "not news"), {m: {s: 1.0 for s in vandalism.SIGNALS}
                                                    for m in vandalism.MOMENTS}))  # fmt: skip
    off = [spike("News", at(7, 1.1)), spike("Vandal", at(7, 2.1))]
    report = {
        "lake": {"prefix": "history/edits", "from": at(3), "to": at(10)},
        "sets": {"human": {"file": "evals/labels/spike_labels.jsonl", "labeler": "human",
                           "models": [], "news": 1, "not_news": 1, "unsure_left_out": 0}},
        "signals": {"human": vandalism.separation(rows, resamples=50, seed=1)},
        "current_max_anon_share": 0.5,
        "rules": {
            "scored_from": at(4),
            "lake": vandalism.rule_results({1.0: off, 0.5: off[:1]},
                                           {"human": [b for b, _ in rows]}, at(7, 0)),
            "gate": [{"max_anon_share": 1.0, "spikes": 19, "precision": 0.526, "recall": 0.211,
                      "found": 8, "news_bursts": 38}],
        },
    }  # fmt: skip
    text = vandalism.render_markdown(report)
    for heading in ("## Labels", "## Signals", "### human", "## The news classifier",
                    "## Candidate rule", "### Labeled bursts the rule drops"):  # fmt: skip
        assert heading in text
    assert "| anon_share | start | 1 / 1 | 0.00 | 1.00 | 1.00" in text
    assert "- Vandal (2026-11-07 02:00 UTC): not news" in text
    json_path, md_path = vandalism.write_report(report, tmp_path, at(16).date())
    assert md_path.name == "2026-11-16_vandalism.md" and json_path.exists()
