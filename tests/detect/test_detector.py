"""Spike lifecycle: start rules, cold start, bots, reverts, ending, determinism."""

import json
from datetime import timedelta

import pytest

from tests.detect.helpers import T0, burst, config, fixture_events, make_edit
from wikipulse.detect.detector import SpikeDetector, SpikeRecord, is_revert, poisson_z
from wikipulse.stream.models import EditEvent


def run(events: list[EditEvent], **overrides) -> tuple[SpikeDetector, list[SpikeRecord]]:
    """Process events in time order; return the detector and the final spike records."""
    detector = SpikeDetector(config(**overrides))
    for event in sorted(events, key=lambda e: e.event_time):
        detector.process(event)
    return detector, final_records(detector.pop_changes())


def final_records(changes: list[SpikeRecord]) -> list[SpikeRecord]:
    """Last version of each spike, as the database would hold it."""
    latest = {(s.title, s.started_at): s for s in changes}
    return sorted(latest.values(), key=lambda s: (s.started_at, s.title))


def test_burst_from_several_editors_starts_a_spike() -> None:
    detector, spikes = run(burst("News", 0, edits=5, editors=3))
    assert [s.title for s in spikes] == ["News"]
    spike = spikes[0]
    assert spike.status == "active"
    assert spike.started_at == T0 + timedelta(minutes=4)  # the 5th edit
    assert spike.first_edit_at == T0
    assert (spike.total_edits, spike.distinct_editors, spike.peak_edits_10m) == (5, 3, 5)
    assert spike.score == pytest.approx(poisson_z(5, 0.5))  # cold start: expected = floor
    assert [s.title for s in detector.active_spikes()] == ["News"]


def test_a_story_building_over_an_hour_starts_a_spike_on_the_long_window() -> None:
    _, spikes = run(burst("Slow story", 0, edits=12, editors=4, every_min=5))
    assert [s.title for s in spikes] == ["Slow story"]
    spike = spikes[0]
    assert spike.started_at == T0 + timedelta(minutes=45)  # the 10th edit
    assert spike.first_edit_at == T0
    assert (spike.total_edits, spike.distinct_editors) == (12, 4)
    assert spike.peak_edits_10m == 2


def test_peak_counts_a_dense_stretch_before_a_long_window_start() -> None:
    # Review case: 4 edits in 2 minutes (too few for the short rule), then 6 spread-out
    # edits. The spike starts on the long rule at the 10th edit; its busiest 10 minutes
    # were the first 4 edits, not the 2 edits in the window ending at the start.
    dense = [make_edit("Build-up", m, user=f"User{i}") for i, m in enumerate([0, 0.5, 1, 2])]
    spread = [
        make_edit("Build-up", m, user=f"User{i % 4}")
        for i, m in enumerate([15, 22, 29, 36, 43, 50])
    ]
    _, spikes = run([*dense, *spread])
    assert [s.title for s in spikes] == ["Build-up"]
    spike = spikes[0]
    assert spike.started_at == T0 + timedelta(minutes=50)
    assert spike.first_edit_at == T0
    assert (spike.total_edits, spike.peak_edits_10m) == (10, 4)


def test_peak_counts_a_late_edit_in_an_earlier_stretch() -> None:
    detector, _ = run(burst("News", 0, edits=5, editors=3))  # spike, peak 5
    detector.process(make_edit("News", 12, user="User9"))  # window (2, 12]: 3 edits
    detector.process(make_edit("News", 3.5, user="User8"))  # late: (-6, 4] now holds 6
    spike = final_records(detector.pop_changes())[0]
    assert (spike.total_edits, spike.peak_edits_10m) == (7, 6)


def test_one_busy_editor_is_not_a_spike() -> None:
    _, spikes = run(burst("Draft work", 0, edits=20, editors=1, every_min=0.4))
    assert spikes == []


def test_a_few_edits_are_not_a_spike() -> None:
    _, spikes = run(burst("Quiet page", 0, edits=4, editors=4))
    assert spikes == []


def test_edits_spread_over_more_than_the_short_window_are_not_a_spike() -> None:
    _, spikes = run(burst("Slow page", 0, edits=8, editors=4, every_min=3))
    assert spikes == []


def test_bot_edits_are_excluded() -> None:
    bots = [make_edit("Bot page", i, user=f"Bot{i % 4}", is_bot=True) for i in range(10)]
    detector, spikes = run(bots)
    assert spikes == []
    assert detector.clock.stats["bots"] == 10


def test_reverts_do_not_start_a_spike_but_are_counted_in_one() -> None:
    edit_war = [
        make_edit("Vandalised", i, user=f"User{i % 3}", comment="Reverted edits by X")
        for i in range(6)
    ]
    assert run(edit_war)[1] == []
    news = burst("News", 0, edits=5, editors=3)
    revert = make_edit("News", 6, user="Patroller", comment="Undid revision 123 by Vandal")
    _, spikes = run([*news, revert])
    assert (spikes[0].total_edits, spikes[0].revert_edits) == (5, 1)


def test_reverts_count_as_edits_when_configured() -> None:
    edit_war = [
        make_edit("Vandalised", i, user=f"User{i % 3}", comment="Reverted edits by X")
        for i in range(6)
    ]
    _, spikes = run(edit_war, count_reverts=True)
    assert [s.title for s in spikes] == ["Vandalised"]


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("Reverted edits by [[Special:Contribs/X|X]] to last version by Y", True),
        ("Reverted 1 edit by X (talk): unsourced", True),
        ("Undid revision 123 by X", True),
        ("rv vandalism", True),
        ("Restored revision 123 by X", True),
        ("Added a revert-proof citation", False),
        ("Update results", False),
        ("", False),
    ],
)
def test_is_revert(comment: str, expected: bool) -> None:
    assert is_revert(comment) is expected


def test_a_busy_page_needs_more_than_its_normal_activity() -> None:
    # One regular editor edits every 2 minutes for 26 hours: 5 edits per 10 minutes is
    # normal for this page. Only one editor, so this alone never starts a spike.
    steady = [make_edit("Busy", m, user="Regular") for m in range(0, 26 * 60, 2)]
    start = 26 * 60 + 1
    small = burst("Busy", start, edits=6, editors=3, every_min=1)
    no_rhythm = {"rhythm_min": 1.0, "rhythm_max": 1.0}
    assert run([*steady, *small], **no_rhythm)[1] == []  # 5 + 6 edits < 5 x 5 expected
    big = burst("Busy", start, edits=30, editors=6, every_min=0.3)
    _, spikes = run([*steady, *big], **no_rhythm)
    assert [s.title for s in spikes] == ["Busy"]


def test_a_short_lull_keeps_one_spike() -> None:
    first = burst("News", 0, edits=6, editors=3)
    lull_then_more = burst("News", 20, edits=6, editors=3)  # 15 minutes of silence
    _, spikes = run([*first, *lull_then_more])
    assert len(spikes) == 1
    assert spikes[0].total_edits == 12


def test_spike_ends_after_the_quiet_period_and_a_new_burst_is_a_new_spike() -> None:
    first = burst("News", 0, edits=6, editors=3)  # last hot moment at minute 5
    elsewhere = [make_edit("Other", m, user="Z") for m in range(6, 40)]  # time goes on
    second = burst("News", 60, edits=6, editors=3)
    detector, spikes = run([*first, *elsewhere, *second])
    assert [s.status for s in spikes] == ["ended", "active"]
    assert spikes[0].ended_at == T0 + timedelta(minutes=5)  # its last edit
    assert spikes[1].started_at == T0 + timedelta(minutes=64)


def test_spike_ends_without_further_edits_to_its_page() -> None:
    first = burst("News", 0, edits=6, editors=3)
    elsewhere = [make_edit("Other", m, user="Z") for m in range(6, 40)]
    detector, spikes = run([*first, *elsewhere])
    assert spikes[0].status == "ended"
    assert detector.active == {}


def test_isolated_late_edits_keep_counting_but_do_not_keep_a_spike_alive() -> None:
    first = burst("News", 0, edits=6, editors=3)
    trickle = [make_edit("News", m, user="Late") for m in (20, 36)]  # 1 edit per 10 min
    elsewhere = [make_edit("Other", m, user="Z") for m in range(6, 60)]
    _, spikes = run([*first, *trickle, *elsewhere])
    assert spikes[0].status == "ended"
    assert spikes[0].total_edits == 7  # the minute-20 edit; by 36 the spike had ended
    assert spikes[0].ended_at == T0 + timedelta(minutes=20)


def test_peak_and_score_keep_their_maximum() -> None:
    events = [
        *burst("News", 0, edits=5, editors=3),
        *burst("News", 5, edits=10, editors=5, every_min=0.2),
    ]
    _, spikes = run(events)
    assert spikes[0].peak_edits_10m == 15
    assert spikes[0].score == pytest.approx(poisson_z(15, 0.5))


def test_very_late_events_go_straight_to_the_baseline() -> None:
    detector, _ = run([make_edit("Other", 120, user="Z")])
    detector.process(make_edit("Old", 0, user="A"))  # two hours behind the watermark
    assert detector.clock.stats["late"] == 1
    assert "Old" in detector.baselines.pages
    assert detector.windows.counts("Old", detector.now or 0).edits_long == 0


def mixed_events() -> list[EditEvent]:
    """The recorded fixture plus synthetic bursts placed inside its time span."""
    real = fixture_events()
    start = real[0].event_time
    synthetic = [
        *[make_edit("Breaking", i * 0.2, user=f"U{i % 4}", start=start) for i in range(8)],
        *[make_edit("Edit war", i * 0.1, user=f"V{i % 3}", start=start) for i in range(3)],
    ]
    return sorted([*real, *synthetic], key=lambda e: e.event_time)


def test_same_input_gives_the_same_spikes() -> None:
    events = mixed_events()  # built once: make_edit() numbers its event ids
    first = run(events, min_edits=2, min_editors=2, min_ratio=2)[1]
    second = run(events, min_edits=2, min_editors=2, min_ratio=2)[1]
    assert first == second
    assert {"Breaking", "Edit war", "Heaven (nightclub)"} <= {s.title for s in first}


def test_snapshot_and_restore_half_way_gives_the_same_spikes() -> None:
    events = mixed_events() + burst("Later", 200, edits=6, editors=3)
    overrides = {"min_edits": 2, "min_editors": 2, "min_ratio": 2}
    _, uninterrupted = run(events, **overrides)

    before = SpikeDetector(config(**overrides))
    half = len(events) // 2
    for event in events[:half]:
        before.process(event)
    changes = before.pop_changes()
    state = json.loads(json.dumps(before.state_to_dict()))
    pages = {t: json.loads(json.dumps(c.to_list())) for t, c in before.baselines.pages.items()}

    after = SpikeDetector(config(**overrides))
    after.load_state(state, pages)
    for event in events[half:]:
        after.process(event)
    assert final_records(changes + after.pop_changes()) == uninterrupted


def test_a_spike_carries_the_id_of_the_edit_that_started_it() -> None:
    edits = burst("Breaking", 0, edits=6, editors=3)
    detector, spikes = run(edits, min_edits=5, min_editors=3)
    # The fifth edit is the first moment with 5 edits from 3 editors.
    assert [s.correlation_id for s in spikes] == [edits[4].event_id]


def test_old_snapshots_without_a_correlation_id_still_load() -> None:
    detector, _ = run(burst("Breaking", 0, edits=6, editors=3), min_edits=5, min_editors=3)
    state = json.loads(json.dumps(detector.state_to_dict()))
    for spike in state["active"]:
        spike.pop("correlation_id")
    restored = SpikeDetector(config(min_edits=5, min_editors=3))
    restored.load_state(state, {})
    assert [s.correlation_id for s in restored.active_spikes()] == [None]
