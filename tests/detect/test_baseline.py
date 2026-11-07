"""Baselines: decaying counts, cold-start correction, daily rhythm, pruning."""

import json
import math

import pytest

from wikipulse.detect.baseline import Baselines, DecayingCount

HOUR = 3600.0
DAY = 24 * HOUR


def test_level_decays_by_e_per_time_constant() -> None:
    count = DecayingCount(tau_s=HOUR)
    count.add(0.0)
    assert count.level_at(0.0) == 1.0
    assert count.level_at(HOUR) == pytest.approx(math.exp(-1))
    assert count.level_at(-10.0) == 1.0  # never grows for an earlier time


def test_order_of_events_does_not_change_the_level() -> None:
    times = [0.0, 50.0, 10.0, 3000.0, 2999.0]
    in_order, as_given = DecayingCount(HOUR), DecayingCount(HOUR)
    for t in sorted(times):
        in_order.add(t)
    for t in times:
        as_given.add(t)
    assert as_given.level_at(4000.0) == pytest.approx(in_order.level_at(4000.0))


def test_cold_start_correction_gives_the_plain_average_for_short_runs() -> None:
    count = DecayingCount(tau_s=DAY)
    for i in range(6):  # 6 edits spread over one hour
        count.add(i * HOUR / 6)
    rate_per_hour = count.rate(now=HOUR, observed_s=HOUR) * HOUR
    assert rate_per_hour == pytest.approx(6, rel=0.05)
    uncorrected = count.level_at(HOUR) / DAY * HOUR
    assert uncorrected < 0.3  # what a plain decaying average would say


def test_long_runs_converge_to_the_true_rate() -> None:
    count = DecayingCount(tau_s=DAY)
    for hour in range(24 * 10):  # one edit per hour for 10 days
        count.add(hour * HOUR)
    now = 24 * 10 * HOUR
    assert count.rate(now, observed_s=now) * HOUR == pytest.approx(1.0, rel=0.05)


def test_zero_observation_means_zero_rate() -> None:
    count = DecayingCount(tau_s=DAY)
    count.add(0.0)
    assert count.rate(0.0, observed_s=0.0) == 0.0


def make_baselines(**kwargs) -> Baselines:
    return Baselines(tau_s=DAY, rhythm_tau_s=HOUR, page_delay_s=HOUR, **kwargs)


def test_unknown_page_expects_nothing() -> None:
    baselines = make_baselines()
    baselines.record_overall_edit(0.0)
    assert baselines.expected("Never edited", now=10 * HOUR, window_s=600) == 0.0


def test_expected_edits_scale_with_window_and_history() -> None:
    baselines = make_baselines(rhythm_min=1.0, rhythm_max=1.0)  # no rhythm here
    baselines.record_overall_edit(0.0)
    for i in range(60):  # one edit per minute, fed one hour late (the page delay)
        baselines.record_page_edit("Busy", HOUR + i * 60)
    now = 2 * HOUR
    per_10_min = baselines.expected("Busy", now, window_s=600)
    assert per_10_min == pytest.approx(10, rel=0.05)
    assert baselines.expected("Busy", now, window_s=1200) == pytest.approx(2 * per_10_min)


def test_early_page_edits_are_not_read_as_a_huge_rate() -> None:
    baselines = make_baselines(rhythm_min=1.0, rhythm_max=1.0)
    baselines.record_overall_edit(0.0)
    baselines.record_page_edit("Page", HOUR + 1)  # one edit, right after feeding begins
    # assumed observation is at least one long window (1 h): about 1 edit per hour
    assert baselines.expected("Page", HOUR + 2, window_s=HOUR) == pytest.approx(1.0, rel=0.05)


def test_rhythm_is_above_one_when_the_last_hour_is_busier() -> None:
    baselines = make_baselines(rhythm_max=5.0)
    for minute in range(0, 23 * 60, 10):  # quiet: one edit per 10 minutes for 23 hours
        baselines.record_overall_edit(minute * 60.0)
    for second in range(0, 3600, 60):  # busy: one per minute in the last hour
        baselines.record_overall_edit(23 * HOUR + second)
    assert baselines.rhythm(24 * HOUR) > 2


def test_rhythm_is_clamped_and_neutral_without_data() -> None:
    baselines = make_baselines(rhythm_min=0.5, rhythm_max=2.0)
    assert baselines.rhythm(0.0) == 1.0
    for minute in range(0, 60 * 23, 1):
        baselines.record_overall_edit(minute * 60.0)
    assert baselines.rhythm(30 * HOUR) == 0.5  # nothing for 7 hours: very quiet, clamped


def test_prune_forgets_idle_pages_and_reports_them() -> None:
    baselines = make_baselines()
    baselines.record_page_edit("Idle", 0.0)
    baselines.record_page_edit("Active", 10 * DAY)
    assert baselines.prune(now=10 * DAY, min_per_day=0.05) == 1
    assert set(baselines.pages) == {"Active"}
    assert baselines.removed == {"Idle"}
    assert "Idle" not in baselines.changed


def test_snapshot_round_trip_through_json() -> None:
    baselines = make_baselines()
    baselines.record_overall_edit(5.0)
    baselines.record_page_edit("Page", 7.0)
    restored = make_baselines()
    restored.load_overall(json.loads(json.dumps(baselines.overall_to_dict())))
    restored.load_pages(
        {t: json.loads(json.dumps(c.to_list())) for t, c in baselines.pages.items()}
    )
    assert restored.expected("Page", 2 * HOUR, 600) == baselines.expected("Page", 2 * HOUR, 600)
    assert restored.rhythm(2 * HOUR) == baselines.rhythm(2 * HOUR)
