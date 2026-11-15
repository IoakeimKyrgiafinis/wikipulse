"""Spec 11b: the retraining labels its new spikes, and tests only on days whose labels
are in (the label delay). No network, no LLM: a fake labeler, a local lake, a portal
given by the test, and `SpikeContext` instead of the Wikimedia API."""

import json
from datetime import UTC, datetime

import httpx

from tests.eval.helpers import at, portal_day, spike
from tests.eval.test_label import FixedLabeler
from tests.jobs.test_retrain import BUILD_SETTINGS, short_lake_build
from tests.llm.helpers import ScriptedClient, mock_ollama
from wikipulse.config import Settings
from wikipulse.eval.label import LLMLabeler, SpikeContext
from wikipulse.eval.label_store import append_record, load_records, record_for
from wikipulse.eval.metrics import Label
from wikipulse.jobs.retrain import (
    REJECT,
    Comparison,
    decide,
    step_build,
    step_label,
)
from wikipulse.ml.dataset import day_readiness
from wikipulse.ml.fresh_labels import (
    BUDGET,
    LLM_DOWN,
    TIME,
    WIKI_DOWN,
    label_order,
    label_within_budget,
    needs_label,
)

MODEL = "ollama:qwen3:4b"
NOW = datetime(2026, 11, 15, tzinfo=UTC)


def settings(**changes) -> Settings:
    return Settings(
        _env_file=None,
        **BUILD_SETTINGS,
        ml_retrain_label_model=MODEL,
        **changes,
    )


def labeled_titles(path) -> list[str]:
    return [r.title for r in load_records(path)]


# --- which spikes, in which order --------------------------------------------------


def test_a_spike_needs_a_label_only_if_nobody_judged_it():
    news, other, linked = (
        spike("News", at(5, 12)),
        spike("Other", at(5, 12)),
        spike("Big", at(5, 9)),
    )
    records = [record_for(news, "r", "unsure", "llm", NOW, model="ollama:qwen3:8b")]
    portal = [portal_day(d, ["Big"], []) for d in range(3, 9)]
    as_of = at(10)
    assert not needs_label(news, records, portal, as_of)  # any model, unsure included
    assert not needs_label(linked, records, portal, as_of)  # the portal says news
    assert needs_label(other, records, portal, as_of)


def test_days_that_can_be_test_days_come_first_newest_first():
    spikes = [spike(f"P{d}", at(d, 12)) for d in range(2, 10)]
    testable = {f"2026-11-0{d}" for d in range(2, 6)}
    order = [s.title for s in label_order(spikes, testable)]
    assert order == ["P5", "P4", "P3", "P2", "P6", "P7", "P8", "P9"]


# --- the budget and the failures -----------------------------------------------------


def test_the_budget_bounds_the_llm_calls_and_the_rest_waits(tmp_path):
    path = tmp_path / "labels.jsonl"
    todo = [spike(f"P{n}", at(5, n)) for n in range(5)]
    labeler = FixedLabeler("not news")
    run = label_within_budget(todo, SpikeContext, labeler, MODEL, "07a-default", path, budget=2)
    assert (run.asked, run.stopped, labeler.calls) == (2, BUDGET, 2)
    assert labeled_titles(path) == ["P0", "P1"]
    # the next run picks up where this one stopped
    run = label_within_budget(todo, SpikeContext, labeler, MODEL, "07a-default", path, budget=2)
    assert labeled_titles(path) == ["P0", "P1", "P2", "P3"]
    run = label_within_budget(todo, SpikeContext, labeler, MODEL, "07a-default", path, budget=9)
    assert (run.asked, run.stopped) == (1, "")
    assert [r.model for r in load_records(path)] == [MODEL] * 5


def test_a_time_limit_stops_the_run_and_keeps_what_was_labeled(tmp_path):
    path = tmp_path / "labels.jsonl"
    todo = [spike(f"P{n}", at(5, n)) for n in range(4)]
    ticks = iter([True, True, False])
    run = label_within_budget(
        todo,
        SpikeContext,
        FixedLabeler(),
        MODEL,
        "r",
        path,
        budget=10,
        time_left=lambda: next(ticks),
    )
    assert (run.asked, run.stopped) == (2, TIME)
    assert labeled_titles(path) == ["P0", "P1"]


def test_an_invalid_answer_costs_budget_but_the_run_goes_on(tmp_path):
    path = tmp_path / "labels.jsonl"
    todo = [spike("P0", at(5, 0)), spike("P1", at(5, 1))]
    client = ScriptedClient(["oops", "still oops", '{"label": "news", "reason": "a death"}'])
    run = label_within_budget(todo, SpikeContext, LLMLabeler(client), MODEL, "r", path, budget=5)
    assert (run.asked, run.failed, dict(run.added)) == (2, 1, {"news": 1})
    assert labeled_titles(path) == ["P1"]


def test_ollama_down_ends_the_labeling_without_an_error(tmp_path):
    def refuse(body):
        raise httpx.ConnectError("connection refused")

    client, _ = mock_ollama(refuse)
    path = tmp_path / "labels.jsonl"
    todo = [spike("P0", at(5, 0)), spike("P1", at(5, 1))]
    run = label_within_budget(todo, SpikeContext, LLMLabeler(client), MODEL, "r", path, budget=5)
    assert (run.asked, run.stopped) == (0, LLM_DOWN)
    assert "Cannot reach Ollama" in run.detail
    assert not path.exists()


def test_wikimedia_failing_stops_further_requests(tmp_path):
    def contexts(s):
        raise httpx.ConnectError("no route")

    labeler = FixedLabeler()
    todo = [spike("P0", at(5, 0))]
    run = label_within_budget(todo, contexts, labeler, MODEL, "r", tmp_path / "l", budget=5)
    assert (run.stopped, labeler.calls) == (WIKI_DOWN, 0)


# --- the task: idempotent, human labels untouched, never failing for labels ----------


def test_the_task_is_idempotent_and_never_touches_a_human_label(tmp_path, capsys):
    options = short_lake_build(tmp_path, days=11, linked=False)
    human = record_for(spike("News 7", at(7, 12)), "07a-default", "not news", "human", NOW)
    append_record(options.labels, human)
    before = options.labels.read_text(encoding="utf-8")
    labeler = FixedLabeler("news")

    assert (
        step_label(settings(), None, options=options, labeler=labeler, contexts=SpikeContext) == 0
    )
    assert capsys.readouterr().out.strip().splitlines()[-1] == "labeled 9"
    after = options.labels.read_text(encoding="utf-8")
    assert after.startswith(before)  # appended only; the human line is untouched
    records = load_records(options.labels)
    assert records[0] == human
    llm = [r for r in records if r.labeler == "llm"]
    assert sorted(r.title for r in llm) == sorted(
        f"News {d}" for d in (2, 3, 4, 5, 6, 8, 9, 10, 11)
    )
    assert {(r.model, r.run_id) for r in llm} == {(MODEL, "07a-default")}

    assert (
        step_label(settings(), None, options=options, labeler=labeler, contexts=SpikeContext) == 0
    )
    assert capsys.readouterr().out.strip().splitlines()[-1] == "labeled 0"
    assert labeler.calls == 9
    assert options.labels.read_text(encoding="utf-8") == after


def test_the_task_labels_the_days_that_can_be_test_days_first(tmp_path, caplog):
    """As of the lake's end (12 Nov), days 2 to 7 can be test days: the budget of 3 goes
    to 7, 6 and 5 Nov, and the log says what was added, used and is still missing."""
    options = short_lake_build(tmp_path, days=11, linked=False)
    with caplog.at_level("INFO"):
        step_label(
            settings(ml_retrain_label_budget=3),
            None,
            options=options,
            labeler=FixedLabeler(),
            contexts=SpikeContext,
        )
    assert labeled_titles(options.labels) == ["News 7", "News 6", "News 5"]
    assert "Labels added: 3 (news 3)" in caplog.text
    assert "budget used: 3 of 3 spikes" in caplog.text
    assert "Still unlabeled: 7 spikes, 3 of them on days that could be test days" in caplog.text
    assert "the budget ran out" in caplog.text


def test_with_ollama_down_the_task_succeeds_and_the_build_goes_on(tmp_path, caplog, capsys):
    """Requirement 2: no labels at all this week. The task logs it and succeeds; the build
    uses the labels from earlier runs (here 2 to 6 Nov) and leaves the rest waiting."""
    options = short_lake_build(tmp_path, days=11, linked=False)
    for day in range(2, 7):
        s = spike(f"News {day}", at(day, 12))
        append_record(options.labels, record_for(s, "07a-default", "news", "llm", NOW, MODEL))

    def refuse(body):
        raise httpx.ConnectError("connection refused")

    client, _ = mock_ollama(refuse)
    with caplog.at_level("INFO"):
        code = step_label(
            settings(), None, options=options, labeler=LLMLabeler(client), contexts=SpikeContext
        )
    assert code == 0
    assert "the labeler is unavailable (LLMUnavailableError" in caplog.text
    assert "The build goes on with the labels there are" in caplog.text
    assert step_build(settings(), None, options) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "v1"
    split = options.datasets.manifest("v1")["split"]
    assert options.datasets.manifest("v1")["splits"]["test"]["days"] == ["2026-11-05", "2026-11-06"]
    assert split["delayed_days"] == [f"2026-11-{d:02d}" for d in range(7, 12)]
    assert split["days_not_ready"]["2026-11-07"] == ["labels (0 of 1 spikes)"]


# --- the label delay -----------------------------------------------------------------


def test_a_day_is_ready_for_testing_once_old_enough_final_and_labeled():
    spikes = [spike("A", at(5, 10)), spike("B", at(5, 14)), spike("C", at(8, 10))]
    portal = [portal_day(d, ["A"], []) for d in range(1, 12)]
    labels = [Label("B", at(5, 13), at(5, 16), "not news", "llm", MODEL)]
    ready = day_readiness(spikes, portal, labels, at(12), delay_days=4, min_share=1.0)
    assert ready["2026-11-05"].ready and ready["2026-11-05"].labeled == 2
    # 8 Nov: ended 9 Nov, so 4 days later is 13 Nov; its portal (to 10 Nov) is final on
    # 13 Nov too; and its spike has no label
    assert ready["2026-11-08"].waiting_for() == [
        "label delay",
        "portal not final",
        "labels (0 of 1 spikes)",
    ]
    # a longer label delay holds even a final, labeled day back
    later = day_readiness(spikes, portal, labels, at(12), delay_days=7, min_share=1.0)
    assert later["2026-11-05"].waiting_for() == ["label delay"]
    # the share: one of two labeled is enough at 0.5, not at 0.95
    half = [spike("A", at(5, 10)), spike("D", at(5, 20))]
    assert day_readiness(half, portal, [], at(12), 4, min_share=0.5)["2026-11-05"].ready
    assert not day_readiness(half, portal, [], at(12), 4, min_share=0.95)["2026-11-05"].ready


def test_the_test_split_moves_back_to_labeled_days_and_newer_days_wait(tmp_path):
    """7 Nov is final at the lake's end but unlabeled: the test split is 5 and 6 Nov, and
    7 Nov waits (it isn't put into training: that would come after the test days)."""
    options = short_lake_build(tmp_path, days=11, linked=False)
    for day in range(2, 7):
        s = spike(f"News {day}", at(day, 12))
        append_record(options.labels, record_for(s, "07a-default", "news", "llm", NOW, MODEL))
    assert step_build(settings(), None, options) == 0
    manifest = options.datasets.manifest("v1")
    assert manifest["splits"]["test"]["days"] == ["2026-11-05", "2026-11-06"]
    assert manifest["splits"]["valid"]["days"] == ["2026-11-04"]
    assert manifest["split"]["delayed_days"] == [f"2026-11-{d:02d}" for d in range(7, 12)]
    assert manifest["split"]["delayed_rows"] == 5
    assert manifest["split"]["label_delay_days"] == 4

    # after labeling, the next build tests on the newest ready days again
    step_label(settings(), None, options=options, labeler=FixedLabeler(), contexts=SpikeContext)
    assert step_build(settings(), None, options) == 0
    assert options.datasets.manifest("v2")["splits"]["test"]["days"] == [
        "2026-11-06",
        "2026-11-07",
    ]


def test_a_longer_label_delay_moves_the_test_split_back(tmp_path):
    options = short_lake_build(tmp_path, days=11)  # every burst linked: all labeled
    assert step_build(settings(ml_label_delay_days=5), None, options) == 0
    manifest = options.datasets.manifest("v1")
    assert manifest["splits"]["test"]["days"] == ["2026-11-05", "2026-11-06"]
    assert manifest["split"]["days_not_ready"]["2026-11-07"] == ["label delay"]


def test_without_labeled_days_the_build_says_so(tmp_path, caplog):
    from wikipulse.jobs.retrain import NO_DATASET

    options = short_lake_build(tmp_path, days=11, linked=False)
    with caplog.at_level("INFO"):
        assert step_build(settings(), None, options) == NO_DATASET
    assert "only 0 days ready for testing (a final portal and labeled spikes)" in caplog.text
    assert "label the newest days' spikes" in caplog.text


# --- requirement 3: the rejection for too few rows says why ---------------------------


def test_too_few_labeled_rows_says_how_many_were_labeled_unlabeled_and_delayed():
    c = Comparison(
        dataset="v9",
        rows=31,
        positives=12,
        champion_version="3",
        challenger_version="7",
        champion_ap=0.9,
        challenger_ap=0.95,
        gap=0.05,
        gap_low=0.0,
        gap_high=0.1,
        champion_precision=0.8,
        champion_recall=0.8,
        challenger_precision=0.8,
        challenger_recall=0.8,
        unlabeled_rows=4,
        delayed_rows=120,
        delayed_days=4,
    )
    decision, reason = decide(c, margin=0.01, min_rows=50)
    assert decision == REJECT
    assert "only 31 labeled test rows (fewer than 50)" in reason
    assert "4 unlabeled rows" in reason
    assert "120 rows on 4 delayed days" in reason


def test_a_label_record_line_is_plain_json(tmp_path):
    """The labels file stays one JSON object per line (append-only, reviewable)."""
    options = short_lake_build(tmp_path, days=11, linked=False)
    step_label(
        settings(ml_retrain_label_budget=1),
        None,
        options=options,
        labeler=FixedLabeler(),
        contexts=SpikeContext,
    )
    [line] = options.labels.read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["labeler"] == "llm" and record["model"] == MODEL


def test_a_zero_budget_asks_nobody(tmp_path):
    options = short_lake_build(tmp_path, days=11, linked=False)
    labeler = FixedLabeler()
    s = settings(ml_retrain_label_budget=0)
    assert step_label(s, None, options=options, labeler=labeler, contexts=SpikeContext) == 0
    assert labeler.calls == 0 and not options.labels.exists()
