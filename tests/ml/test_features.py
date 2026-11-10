"""Features of a spike, and the leakage guard: nothing after the cutoff may matter."""

import math
from datetime import UTC, datetime, timedelta

import pytest

import wikipulse.ml.features as features_module
from wikipulse.ml.features import (
    FEATURE_NAMES,
    FEATURES,
    LeakageError,
    PageEdit,
    PageSnapshot,
    SpikeWindow,
    check_no_leakage,
    compute_features,
    is_anonymous,
    peak_count,
    visible_edits,
)

START = datetime(2026, 11, 6, 14, 0, tzinfo=UTC)  # the spike starts (started_at)
FIRST = START - timedelta(minutes=8)  # its burst began 8 minutes earlier
WINDOW = SpikeWindow("2026 Example election", FIRST, START, window_min=10)
CUTOFF = START + timedelta(minutes=10)


def at(minutes: float) -> datetime:
    """`minutes` after the spike started (negative: before)."""
    return START + timedelta(minutes=minutes)


def edit(minutes: float, user: str, **kwargs) -> PageEdit:
    defaults = {"len_old": 1000, "len_new": 1100, "rev_new": 500 + int(minutes * 10)}
    return PageEdit(time=at(minutes), user=user, **{**defaults, **kwargs})


def synthetic_spike() -> list[PageEdit]:
    """A burst before and after the start, some history, and a long life after the cutoff."""
    history = [edit(-600, "Old editor"), edit(-120, "Old editor", comment="tidy")]
    burst = [
        edit(-8, "Alice", comment="Results announced"),
        edit(-6, "Bob", comment="update"),
        edit(-5, "~2026-12345-67", comment=""),
        edit(-3, "Carol", comment="Reverted edits by ~2026-12345-67", len_old=1100, len_new=1000),
        edit(-1, "Alice", comment="sources", is_minor=True),
        edit(0, "Dave", comment="elected"),
        edit(2, "ExampleBot", is_bot=True, comment="bot: dates"),
        edit(4, "Erin", comment="infobox"),
        edit(9, "Bob", comment="vandalism removed", len_old=1100, len_new=900),
    ]
    later = [  # after the cutoff: the spike goes on for hours
        edit(11 + i * 3, f"Late editor {i}", comment="died" if i % 2 else "Undid revision")
        for i in range(30)
    ]
    return history + burst + later


def test_every_feature_is_described_and_computed_in_order():
    values = compute_features(WINDOW, synthetic_spike())
    assert list(values) == FEATURE_NAMES == list(FEATURES)
    assert all(isinstance(v, int | float) for v in values.values())


def test_burst_features_count_only_up_to_the_cutoff():
    values = compute_features(WINDOW, synthetic_spike())
    assert values["edits"] == 8  # human edits from -8 to +9 minutes, not the 30 later ones
    assert values["editors"] == 6  # Alice, Bob, the temporary account, Carol, Dave, Erin
    assert values["edits_before_start"] == 6  # up to and including started_at
    assert values["edits_after_start"] == 2
    assert values["editors_after_start"] == 2
    assert values["bot_edits"] == 1
    assert values["top_editor_share"] == pytest.approx(2 / 8)
    assert values["anon_share"] == pytest.approx(1 / 8)
    assert values["revert_share"] == pytest.approx(1 / 8)
    assert values["minor_share"] == pytest.approx(1 / 8)
    assert values["comment_empty_share"] == pytest.approx(1 / 8)
    assert values["news_word_share"] == pytest.approx(3 / 8)  # announced, update, elected
    assert values["vandal_word_share"] == pytest.approx(1 / 8)
    assert values["burst_minutes"] == pytest.approx(18)
    assert values["detect_delay_min"] == pytest.approx(8)
    assert values["edits_per_hour"] == pytest.approx(8 / 0.3)
    assert values["bytes_added"] == 6 * 100
    assert values["bytes_removed"] == 100 + 200
    assert values["page_len"] == 900  # after the last edit before the cutoff
    assert values["peak_10m"] == 6  # -8 .. 0: six human edits within 10 minutes
    assert values["last_10m_share"] == pytest.approx(2 / 8)  # after 14:00, up to 14:10


def test_history_features_look_at_the_24_hours_before_the_burst():
    values = compute_features(WINDOW, synthetic_spike())
    assert values["prior_edits_24h"] == 2
    assert values["prior_editors_24h"] == 1
    assert values["prior_edits_1h"] == 0
    assert values["hours_since_prior_edit"] == pytest.approx(112 / 60)
    assert values["rate_ratio"] == pytest.approx((8 / 0.3) / (3 / 24))


def test_title_and_time_features():
    values = compute_features(WINDOW, [])
    assert values["hour_utc"] == 14
    assert values["title_words"] == 3
    assert values["title_has_year"] == 1
    assert values["title_has_spike_year"] == 1
    assert values["title_is_list"] == 0
    listing = SpikeWindow("Deaths in 2025", FIRST, START)
    values = compute_features(listing, [])
    assert values["title_is_list"] == 1 and values["title_has_spike_year"] == 0


def test_a_spike_with_no_edits_gives_nan_shares_not_errors():
    values = compute_features(WINDOW, [])
    assert values["edits"] == 0
    assert math.isnan(values["anon_share"]) and math.isnan(values["median_gap_min"])
    assert math.isnan(values["page_age_days"])  # no page snapshot


def test_page_features_from_the_snapshot_at_the_cutoff():
    page = PageSnapshot(
        revision_id=590,
        revision_time=at(9),
        categories=("Living people", "2026 deaths", "1950 births", "Elections in 2026"),
        templates=("current", "infobox person"),
        refs=12,
        created_at=START - timedelta(days=100),
    )
    values = compute_features(WINDOW, synthetic_spike(), page)
    assert values["page_age_days"] == pytest.approx(100 + 10 / 1440)
    assert values["categories"] == 4
    assert values["cat_living_people"] == 1
    assert values["cat_deaths_spike_year"] == 1
    assert values["cat_mentions_spike_year"] == 1
    assert values["is_biography"] == 1
    assert values["has_current_template"] == 1
    assert values["refs"] == 12


# --- the leakage guard ------------------------------------------------------------------


def test_features_ignore_everything_after_the_cutoff():
    """The requirement: no feature uses data from after the feature window."""
    edits = synthetic_spike()
    past = [e for e in edits if e.time <= CUTOFF]
    assert len(past) < len(edits)  # the synthetic spike really goes on after the cutoff
    assert compute_features(WINDOW, edits) == compute_features(WINDOW, past)
    assert check_no_leakage(WINDOW, edits) == compute_features(WINDOW, past)


def test_an_edit_exactly_at_the_cutoff_counts_and_one_just_after_does_not():
    base = synthetic_spike()
    on = compute_features(WINDOW, [*base, edit(10, "Zed")])
    after = compute_features(WINDOW, [*base, PageEdit(CUTOFF + timedelta(microseconds=1), "Zed")])
    assert on["edits"] == compute_features(WINDOW, base)["edits"] + 1
    assert after == compute_features(WINDOW, base)


def test_the_guard_catches_features_that_see_the_future(monkeypatch):
    """Break the one door edits come through: every edit, also later ones, gets in."""

    def everything(edits, cutoff):
        return sorted(edits, key=lambda e: e.time)

    monkeypatch.setattr(features_module, "visible_edits", everything)
    with pytest.raises(LeakageError):
        check_no_leakage(WINDOW, synthetic_spike())


def test_a_page_snapshot_from_after_the_cutoff_is_refused():
    later = PageSnapshot(revision_id=999, revision_time=CUTOFF + timedelta(minutes=1))
    with pytest.raises(LeakageError):
        compute_features(WINDOW, synthetic_spike(), later)


def test_a_creation_time_after_the_cutoff_is_treated_as_unknown():
    page = PageSnapshot(revision_id=1, revision_time=at(5), created_at=CUTOFF + timedelta(days=2))
    assert math.isnan(compute_features(WINDOW, [], page)["page_age_days"])


def test_visible_edits_are_sorted_and_end_at_the_cutoff():
    edits = [edit(12, "B"), edit(-3, "A"), edit(10, "C")]
    assert [e.user for e in visible_edits(edits, CUTOFF)] == ["A", "C"]


@pytest.mark.parametrize(
    ("user", "anonymous"),
    [
        ("~2026-12345-67", True),
        ("192.0.2.1", True),
        ("2001:db8::1", True),
        ("Alice", False),
        ("Bob 2026", False),
    ],
)
def test_anonymous_editors(user, anonymous):
    assert is_anonymous(user) is anonymous


def test_peak_count_uses_a_half_open_window():
    times = [at(0), at(5), at(10), at(10.5)]
    assert peak_count(times, timedelta(minutes=10)) == 3  # 5, 10, 10.5 (0 is 10 min before 10)
