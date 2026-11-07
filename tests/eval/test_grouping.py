"""Grouping evaluation: strata, sampling, pairwise metrics, pair labels and labelers."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tests.events.helpers import ELECTION, ELECTION_ISH, FOOTBALL, T0, page, unit
from wikipulse.eval.grouping import (
    DayData,
    GroupingMetrics,
    Pair,
    StratumCount,
    build_report,
    count_strata,
    event_sizes,
    key_of,
    render_markdown,
    sample_pairs,
    stratify,
)
from wikipulse.eval.grouping_labels import (
    OllamaPairLabeler,
    PageView,
    PairToLabel,
    append_pair_record,
    label_pairs_manually,
    label_pairs_with_llm,
    labels_by_pair,
    load_pair_records,
    make_record,
    needs_llm_label,
    pair_key,
)

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def day_pages():
    """Election (1) and candidate (2) grouped; a linked but separate page (3); a page
    with a similar summary (4); an unrelated page (5)."""
    return [
        page(1, "Election", 0, links=("Candidate",), embedding=ELECTION),
        page(2, "Candidate", 10, embedding=ELECTION),
        page(3, "Party", 20, links=("Election",), embedding=FOOTBALL),
        page(4, "Referendum", 30, embedding=ELECTION_ISH),
        page(5, "Club", 40, embedding=unit(0, 0, 1)),
    ]


EVENT_OF = {1: 1, 2: 1, 3: 2, 4: 3, 5: 4}


def view(title: str, minute: float = 0) -> PageView:
    return PageView(title, title, T0 + timedelta(minutes=minute), 5, "desc", "summary")


# --- strata and sampling ------------------------------------------------------------


def test_pairs_are_put_into_strata() -> None:
    pages = day_pages()
    strata = {(p.i, p.j): p.stratum for p in stratify(pages, EVENT_OF)}
    assert len(strata) == 10  # 5 * 4 / 2
    assert strata[(0, 1)] == "together"
    assert strata[(0, 2)] == "near"  # linked
    assert strata[(0, 3)] == "near"  # similar summaries
    assert strata[(1, 3)] == "near"
    assert strata[(0, 4)] == "far"
    assert strata[(2, 4)] == "far"


def test_sampling_is_fixed_per_stratum_and_shared_between_runs() -> None:
    pages = day_pages()
    pairs = stratify(pages, EVENT_OF)
    sample = sample_pairs(pairs, pages, {"together": 5, "near": 2, "far": 1}, seed=7)
    assert [p.stratum for p in sample] == ["together", "near", "near", "far"]
    assert sample == sample_pairs(pairs, pages, {"together": 5, "near": 2, "far": 1}, seed=7)
    # Another run puts Party into the election event; the far pairs it doesn't touch are
    # ranked the same, so the same far pair is sampled (and its label reused).
    other = stratify(pages, {**EVENT_OF, 3: 1})
    other_sample = sample_pairs(other, pages, {"far": 1}, seed=7)
    assert key_of(other_sample[0], pages) == key_of(sample[-1], pages)


def test_pair_keys_put_the_earlier_spike_first() -> None:
    a, b = ("B", T0), ("A", T0 + timedelta(minutes=1))
    assert pair_key(a, b) == pair_key(b, a) == (a, b)
    c = ("A", T0)
    assert pair_key(a, c) == (c, a)  # same start: by title


# --- metrics ------------------------------------------------------------------------


def metrics() -> GroupingMetrics:
    return GroupingMetrics(
        {
            "together": StratumCount("together", 100, sampled=10, same=8, different=2),
            "near": StratumCount("near", 1000, sampled=10, same=1, different=9),
            "far": StratumCount("far", 10000, sampled=11, same=0, different=10, unsure=1),
        }
    )


def test_precision_is_the_same_rate_of_together_pairs() -> None:
    m = metrics()
    assert m.precision == pytest.approx(0.8)
    assert m.precision_margin == pytest.approx(1.96 * (0.8 * 0.2 / 10) ** 0.5)


def test_recall_weights_each_stratum_by_its_size() -> None:
    m = metrics()
    # Found: 0.8 * 100 = 80 same pairs together; missed: 0.1 * 1000 = 100 in near, 0 in far.
    assert m.recall() == pytest.approx(80 / 180)
    assert m.recall(include_far=False) == pytest.approx(80 / 180)
    # Unweighted over the labeled pairs: 8 of 9 same pairs were together. Biased upward,
    # because together pairs are sampled far more densely than the others.
    assert m.labeled_pairs_recall() == pytest.approx(8 / 9)
    assert m.far_upper_bound() == pytest.approx(3 / 10 * 10000)  # unsure doesn't count


def test_metrics_without_labels_are_none() -> None:
    empty = GroupingMetrics({n: StratumCount(n, 10) for n in ("together", "near", "far")})
    assert empty.precision is None and empty.recall() is None
    assert empty.labeled_pairs_recall() is None and empty.far_upper_bound() is None


def test_count_strata_uses_the_labels_of_the_sample(tmp_path: Path) -> None:
    pages = day_pages()
    pairs = stratify(pages, EVENT_OF)
    sample = sample_pairs(pairs, pages, {"together": 1, "near": 3, "far": 2}, seed=1)
    path = tmp_path / "labels.jsonl"
    for pair, label in zip(sample[:3], ["same", "same", "different"], strict=True):
        a, b = pages[pair.i], pages[pair.j]
        pair_to_label = PairToLabel(
            view(a.title, (a.started_at - T0).total_seconds() / 60),
            view(b.title, (b.started_at - T0).total_seconds() / 60),
            pair.stratum,
        )
        append_pair_record(path, make_record(pair_to_label, label, "llm", "r", "g", NOW))
    counted = count_strata(pairs, sample, pages, labels_by_pair(load_pair_records(path)))
    assert counted.strata["together"].population == 1
    assert (counted.strata["together"].same, counted.strata["near"].same) == (1, 1)
    assert counted.strata["near"].different == 1 and counted.strata["near"].sampled == 3
    assert counted.strata["far"].labeled == 0


def test_event_sizes_count_the_days_spikes() -> None:
    assert event_sizes(day_pages(), EVENT_OF) == [2, 1, 1, 1]


def test_event_sizes_count_distinct_pages_not_spikes() -> None:
    pages = [
        *day_pages(),
        page(6, "Election", 50, embedding=ELECTION),  # Election spikes again, same event
        page(7, "Elections", 55, canonical="Election", embedding=ELECTION),  # a redirect
        page(8, "Club", 60, embedding=unit(0, 0, 1)),  # Club spikes again: new event
    ]
    event_of = {**EVENT_OF, 6: 1, 7: 1, 8: 5}
    assert event_sizes(pages, event_of) == [2, 1, 1, 1, 1]


def test_report_and_markdown() -> None:
    pages = day_pages()
    pairs = stratify(pages, EVENT_OF)
    data = DayData("02b-loose", [], pages, EVENT_OF, pairs)
    sample = sample_pairs(pairs, pages, {"together": 1, "near": 1, "far": 1}, seed=1)
    together = sample[0]
    record = make_record(
        PairToLabel(view("Election", 0), view("Candidate", 10), "together"),
        "different",
        "llm",
        "02b-loose",
        "g",
        NOW,
        reason="no",
    )
    labels = {key_of(together, pages): record}
    report = build_report("test", "g", T0.date(), data, sample, labels, {"k": 1}, NOW)
    assert report["metrics"]["precision"] == 0.0
    assert report["events"]["events"] == 4 and report["events"]["multi_page_events"] == 1
    text = render_markdown(report)
    assert "false merge: *Election* / *Candidate*" in text
    json.dumps(report)  # serializable


# --- labels and labelers ------------------------------------------------------------


def test_a_human_label_beats_an_llm_label(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    pair = PairToLabel(view("A"), view("B", 5), "near")
    append_pair_record(path, make_record(pair, "same", "llm", "r", "g", NOW, model="m"))
    append_pair_record(path, make_record(pair, "different", "human", "r", "g", NOW))
    append_pair_record(path, make_record(pair, "unsure", "llm", "r", "g", NOW, model="m2"))
    records = load_pair_records(path)
    assert labels_by_pair(records)[pair.key].label == "different"
    assert not needs_llm_label(pair.key, records, "other-model")  # a human labeled it
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["labeler"] == "llm"


def test_a_new_prompt_version_asks_the_model_again(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    pair = PairToLabel(view("A"), view("B", 5), "near")
    old = make_record(pair, "different", "llm", "r", "g", NOW, model="ollama:m")
    append_pair_record(path, old.model_copy(update={"prompt_version": None}))  # a v1 line
    assert needs_llm_label(pair.key, load_pair_records(path), "ollama:m")
    append_pair_record(path, make_record(pair, "same", "llm", "r", "g", NOW, model="ollama:m"))
    records = load_pair_records(path)
    assert not needs_llm_label(pair.key, records, "ollama:m")
    assert labels_by_pair(records)[pair.key].label == "same"  # the newest LLM label counts


def ollama(answers: list[str], status: int = 200) -> tuple[httpx.MockTransport, list[dict]]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        content = answers[min(len(seen), len(answers)) - 1]
        return httpx.Response(status, json={"message": {"content": content}})

    return httpx.MockTransport(handler), seen


def test_llm_labeler_retries_an_invalid_answer() -> None:
    transport, seen = ollama(["not json", '{"label": "same", "reason": "One election."}'])
    labeler = OllamaPairLabeler("http://ollama", "qwen3:4b", transport)
    assert labeler.label(view("A"), view("B")) == ("same", "One election.")
    assert len(seen) == 2
    body = seen[0]
    assert body["think"] is False and body["options"]["temperature"] == 0
    assert "Page A: A" in body["messages"][1]["content"]


def test_llm_labeler_explains_a_missing_model() -> None:
    transport, _ = ollama(["{}"], status=404)
    with pytest.raises(RuntimeError, match="ollama pull"):
        OllamaPairLabeler("http://ollama", "nope", transport).label(view("A"), view("B"))


def test_llm_labeling_skips_pairs_already_labeled_by_the_model(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    pairs = [PairToLabel(view("A"), view("B", 1), "near"), PairToLabel(view("C"), view("D"), "far")]
    transport, seen = ollama(['{"label": "different", "reason": "r"}'])
    labeler = OllamaPairLabeler("http://ollama", "m", transport)
    assert label_pairs_with_llm(pairs, labeler, "ollama:m", "run", "g", path, lambda: NOW) == 2
    assert label_pairs_with_llm(pairs, labeler, "ollama:m", "run", "g", path, lambda: NOW) == 0
    assert len(seen) == 2
    record = load_pair_records(path)[0]
    assert record.labeler == "llm" and record.model == "ollama:m" and record.stratum == "near"


def test_manual_labeling_shows_the_llm_answer_after_yours(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    first = PairToLabel(view("A"), view("B", 1), "near")
    second = PairToLabel(view("C"), view("D", 1), "far")
    append_pair_record(path, make_record(first, "same", "llm", "r", "g", NOW, model="m"))
    answers = iter(["x", "s", "a note", "q"])  # "x" is not a key: asked again
    shown: list[str] = []
    result = label_pairs_manually(
        [first, second], "r", "g", path, lambda: NOW, lambda _: next(answers), shown.append
    )
    assert result == (1, 1, 1)
    assert any("The LLM said: same" in s for s in shown)
    human = [r for r in load_pair_records(path) if r.labeler == "human"]
    assert len(human) == 1 and human[0].reason == "a note" and human[0].label == "same"


def test_pair_type_is_hashable() -> None:
    assert len({Pair(0, 1, "far"), Pair(0, 1, "far")}) == 1
