"""Metric calculations on hand-made spikes, portal days and labels."""

from datetime import date

import pytest

from tests.eval.helpers import at, portal_day, spike
from wikipulse.eval.metrics import (
    Label,
    by_title,
    distribution,
    gap_hours,
    label_precision,
    match_spikes,
    percentile,
    pick_label,
    pooled_recall,
    portal_precision,
    recall_for_day,
    spike_end,
    time_to_detection,
)

NOV6 = date(2026, 11, 6)


def test_gap_is_zero_when_the_spike_overlaps_the_day() -> None:
    assert gap_hours(at(6, 10), at(6, 11), NOV6) == 0
    assert gap_hours(at(5, 23), at(6, 1), NOV6) == 0  # crosses midnight into the day


def test_gap_counts_hours_to_the_nearer_edge_of_the_day() -> None:
    assert gap_hours(at(5, 10), at(5, 20), NOV6) == 4  # ends 4 h before the day starts
    assert gap_hours(at(7, 6), at(7, 8), NOV6) == 6  # starts 6 h after the day ends
    assert gap_hours(at(8, 0), at(8, 1), NOV6) == 24


def test_recall_over_all_links_and_over_lead_links() -> None:
    portal = portal_day(
        6,
        links=["Storm Ava", "France", "Paris", "Jane Doe"],
        leads=["Storm Ava", "Jane Doe"],
    )
    spikes = by_title(
        [
            spike("Storm Ava", at(6, 9)),
            spike("Paris", at(7, 20)),  # 20 h after the day: inside the 24 h window
            spike("Jane Doe", at(9, 2)),  # 50 h after the day: too late
        ]
    )
    everything = recall_for_day(portal, spikes)
    assert everything.found == ["Storm Ava", "Paris"]
    assert everything.recall == pytest.approx(2 / 4)
    leads = recall_for_day(portal, spikes, leads_only=True)
    assert leads.found == ["Storm Ava"]
    assert leads.recall == pytest.approx(1 / 2)


def test_recall_uses_the_redirect_target_and_counts_it_once() -> None:
    portal = portal_day(
        6, links=["Tampa", "Tampa, Florida"], leads=["Tampa"], resolve={"Tampa": "Tampa, Florida"}
    )
    result = recall_for_day(portal, by_title([spike("Tampa, Florida", at(6, 3))]))
    assert result.articles == ["Tampa, Florida"]
    assert result.recall == 1.0


def test_a_spike_before_the_day_counts_within_the_window() -> None:
    portal = portal_day(6, links=["Jane Doe"], leads=["Jane Doe"])
    early = recall_for_day(portal, by_title([spike("Jane Doe", at(5, 2), duration_h=1)]))
    assert early.recall == 1.0  # ended 21 h before the day began
    too_early = recall_for_day(portal, by_title([spike("Jane Doe", at(4, 20), duration_h=1)]))
    assert too_early.recall == 0.0  # ended 27 h before


def test_pooled_recall_weights_every_link_equally() -> None:
    four_links = portal_day(6, ["A", "B", "C", "D"], ["A"])
    day_a = recall_for_day(four_links, by_title([spike("A", at(6))]))
    day_b = recall_for_day(portal_day(7, ["E"], ["E"]), by_title([spike("E", at(7))]))
    assert pooled_recall([day_a, day_b]) == pytest.approx(2 / 5)  # not the mean of 25% and 100%
    assert pooled_recall([]) is None


def test_portal_precision_matches_within_48_hours() -> None:
    portal = [
        portal_day(5, ["Storm Ava"], ["Storm Ava"]),
        portal_day(6, ["France"], []),
    ]
    spikes = [
        spike("Storm Ava", at(7, 12)),  # 36 h after Nov 5 ended: matches
        spike("France", at(6, 8)),  # same day
        spike("Quiet Page", at(6, 8)),  # never on the portal
        spike("Storm Ava", at(8, 6)),  # 54 h after Nov 5: no match
    ]
    matches = match_spikes(spikes, portal)
    assert [m.portal_days for m in matches] == [[date(2026, 11, 5)], [NOV6], [], []]
    assert portal_precision(matches) == pytest.approx(2 / 4)
    assert portal_precision([]) is None


def test_portal_precision_matches_through_redirects() -> None:
    portal = [portal_day(6, ["Kenyan"], ["Kenyan"], {"Kenyan": "Demographics of Kenya"})]
    matches = match_spikes([spike("Demographics of Kenya", at(6, 1))], portal)
    assert matches[0].matched


def test_percentile_interpolates_between_ranks() -> None:
    values = [10, 1, 4, 2, 3]
    assert percentile(values, 50) == 3
    assert percentile(values, 90) == pytest.approx(4 + 0.6 * 6)
    assert percentile([7], 90) == 7
    assert percentile([], 50) is None


def test_time_to_detection_uses_only_spikes_on_the_portal() -> None:
    portal = [portal_day(6, ["A", "B", "C"], ["A"])]
    spikes = [
        spike("A", at(6, 1), delay_min=2),
        spike("B", at(6, 2), delay_min=10),
        spike("C", at(6, 3), delay_min=30),
        spike("Not on portal", at(6, 4), delay_min=500),
    ]
    result = time_to_detection(match_spikes(spikes, portal))
    assert result.count == 3
    assert result.median == pytest.approx(10)
    assert result.p90 == pytest.approx(10 + 0.8 * 20)
    assert (result.minimum, result.maximum) == (2, 30)


def test_empty_distribution() -> None:
    result = distribution([])
    assert result.count == 0 and result.median is None and result.p90 is None


def label(title: str, s, value: str, labeler: str = "llm") -> Label:
    return Label(title, s.first_edit_at, spike_end(s), value, labeler, "ollama:test")


def test_label_precision_leaves_unsure_out() -> None:
    a, b, c, d = (spike(t, at(6, h)) for t, h in [("A", 1), ("B", 2), ("C", 3), ("D", 4)])
    labels = [
        label("A", a, "news"),
        label("B", b, "news"),
        label("C", c, "not news"),
        label("D", d, "unsure"),
    ]
    result = label_precision([a, b, c, d, spike("Unlabeled", at(6, 5))], labels)
    assert result.labeled == 4
    assert result.counts == {"news": 2, "not news": 1, "unsure": 1}
    assert result.precision == pytest.approx(2 / 3)
    assert result.labelers == {"llm": 4}


def test_a_human_label_beats_an_llm_label() -> None:
    s = spike("A", at(6, 1))
    labels = [label("A", s, "not news", "human"), label("A", s, "news", "llm")]
    assert pick_label(labels).label == "not news"
    result = label_precision([s], labels)
    assert result.labelers == {"human": 1}
    assert result.precision == 0.0


def test_labels_carry_over_to_overlapping_spikes_of_another_run() -> None:
    loose = spike("A", at(6, 1), duration_h=3)
    labels = [label("A", loose, "news")]
    strict_same_burst = spike("A", at(6, 2), duration_h=1)
    strict_other_burst = spike("A", at(7, 1))
    result = label_precision([strict_same_burst, strict_other_burst], labels)
    assert result.labeled == 1
    assert result.precision == 1.0
