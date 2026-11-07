"""Sliding-window counting."""

import json

import pytest

from wikipulse.detect.windows import PageWindows

MIN = 60.0


@pytest.fixture
def windows() -> PageWindows:
    return PageWindows(short_s=10 * MIN, long_s=60 * MIN)


def test_counts_edits_and_distinct_editors_in_both_windows(windows: PageWindows) -> None:
    for minute, user in [(0, "a"), (30, "b"), (51, "a"), (55, "c"), (58, "c")]:
        windows.add("Page", minute * MIN, user)
    counts = windows.counts("Page", now=59 * MIN)
    assert (counts.edits_short, counts.editors_short) == (3, 2)  # 51, 55, 58
    assert (counts.edits_long, counts.editors_long) == (5, 3)
    assert counts.first_edit_short == 51 * MIN
    assert counts.first_edit_long == 0.0


def test_window_is_half_open_at_its_start(windows: PageWindows) -> None:
    windows.add("Page", 0.0, "a")
    assert windows.counts("Page", now=10 * MIN - 1).edits_short == 1
    assert windows.counts("Page", now=10 * MIN).edits_short == 0  # exactly 10 min old: out
    assert windows.counts("Page", now=60 * MIN).edits_long == 0


def test_unknown_page_has_zero_counts(windows: PageWindows) -> None:
    counts = windows.counts("Nothing", now=0.0)
    assert (counts.edits_short, counts.edits_long, counts.first_edit_short) == (0, 0, None)


def test_pages_are_counted_separately(windows: PageWindows) -> None:
    windows.add("A", 0.0, "x")
    windows.add("B", 1.0, "x")
    assert windows.counts("A", now=2.0).edits_short == 1
    assert len(windows) == 2


def test_evict_returns_old_edit_times_and_forgets_empty_pages(windows: PageWindows) -> None:
    windows.add("Page", 0.0, "a")
    windows.add("Page", 5 * MIN, "b")
    assert windows.evict("Page", now=62 * MIN) == [0.0]
    assert windows.evict("Page", now=62 * MIN) == []
    assert windows.evict("Page", now=70 * MIN) == [5 * MIN]
    assert len(windows) == 0


def test_evict_all_goes_through_pages_in_title_order(windows: PageWindows) -> None:
    windows.add("B", 0.0, "a")
    windows.add("A", 1.0, "a")
    windows.add("A", 100 * MIN, "a")
    assert list(windows.evict_all(now=61 * MIN + 1)) == [("A", 1.0), ("B", 0.0)]
    assert len(windows) == 1


def test_late_edits_are_kept_in_time_order(windows: PageWindows) -> None:
    windows.add("Page", 5 * MIN, "a")
    windows.add("Page", 2 * MIN, "b")  # arrives late
    assert windows.counts("Page", now=11 * MIN).first_edit_short == 2 * MIN
    assert windows.evict("Page", now=62.5 * MIN) == [2 * MIN]


def test_editors_in_a_window(windows: PageWindows) -> None:
    for minute, user in [(0, "old"), (55, "a"), (56, "b"), (57, "a")]:
        windows.add("Page", minute * MIN, user)
    assert windows.editors("Page", now=58 * MIN, window_s=10 * MIN) == {"a", "b"}
    assert windows.editors("Page", now=58 * MIN, window_s=60 * MIN) == {"old", "a", "b"}


def test_busiest_short_finds_the_densest_stretch_not_the_latest(windows: PageWindows) -> None:
    for minute in [0, 0.5, 1, 2, 15, 22, 29, 36, 43, 50]:
        windows.add("P", minute * MIN, "a")
    assert windows.counts("P", 50 * MIN).edits_short == 2
    assert windows.busiest_short("P", since=0) == 4
    assert windows.busiest_short("P", since=1 * MIN) == 2  # edits before `since` ignored
    assert windows.busiest_short("Unknown", since=0) == 0


def test_busiest_short_window_is_half_open(windows: PageWindows) -> None:
    for minute in [0, 10, 20]:  # exactly one short window apart: never together
        windows.add("P", minute * MIN, "a")
    assert windows.busiest_short("P", since=0) == 1


def test_snapshot_round_trip_through_json(windows: PageWindows) -> None:
    windows.add("Page", 1.5, "a")
    windows.add("Other", 2.25, "b")
    restored = PageWindows(short_s=10 * MIN, long_s=60 * MIN)
    restored.load_dict(json.loads(json.dumps(windows.to_dict())))
    assert restored.to_dict() == windows.to_dict()
    assert restored.counts("Page", now=3.0) == windows.counts("Page", now=3.0)


def test_short_window_longer_than_long_window_is_rejected() -> None:
    with pytest.raises(ValueError):
        PageWindows(short_s=61 * MIN, long_s=60 * MIN)
