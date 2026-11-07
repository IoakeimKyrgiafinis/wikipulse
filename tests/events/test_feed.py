"""The event feed counts distinct pages, not spikes."""

from datetime import timedelta

from tests.events.helpers import T0
from wikipulse.db.events import FeedEvent, FeedPage
from wikipulse.events.feed import by_size, distinct_pages, page_count, render_event, stats_line


def spike(title: str, minute: float, score: float, canonical: str | None = None) -> FeedPage:
    start = T0 + timedelta(minutes=minute)
    return FeedPage(title, canonical or title, start, start, score, 1.0)


def event(number: int, *spikes: FeedPage, score: float = 5.0) -> FeedEvent:
    return FeedEvent(
        number=number,
        status="ended",
        title=spikes[0].canonical_title,
        started_at=min(s.started_at for s in spikes),
        last_spike_at=max(s.started_at for s in spikes),
        ended_at=max(s.started_at for s in spikes),
        page_count=len(spikes),  # the database counts spikes
        score=score,
        pages=tuple(sorted(spikes, key=lambda s: -s.score)),
    )


# "Ramayana: Part 1" spiked four times (once under a redirect), plus its VFX studio.
RAMAYANA = event(
    1,
    spike("Ramayana: Part 1", 0, 9.0),
    spike("Ramayana: Part 1", 60, 7.0),
    spike("Ramayana Part 1", 120, 6.0, canonical="Ramayana: Part 1"),
    spike("Ramayana: Part 1", 180, 4.0),
    spike("DNEG", 30, 5.0),
)
ELECTION = event(2, spike("Election", 0, 3.0), spike("Candidate", 5, 2.0), spike("Party", 9, 1.0))


def test_a_page_that_spiked_several_times_is_one_page() -> None:
    assert page_count(RAMAYANA) == 2
    pages = distinct_pages(RAMAYANA)
    assert [(p.canonical_title, p.score, n) for p, n in pages] == [
        ("Ramayana: Part 1", 9.0, 4),
        ("DNEG", 5.0, 1),
    ]


def test_render_lists_each_page_once_with_its_spikes() -> None:
    lines = render_event(RAMAYANA, max_pages=10)
    assert "[2 page(s), 5 spikes," in lines[0]
    assert len(lines) == 3 and "(4 spikes)" in lines[1] and "DNEG" in lines[2]


def test_stats_and_order_use_distinct_pages() -> None:
    events = by_size([RAMAYANA, ELECTION])
    assert [e.number for e in events] == [2, 1]  # 3 pages beat 2 pages (5 spikes)
    line = stats_line(events)
    assert "5 pages (8 spikes)" in line and "mean 2.50" in line and "max 3" in line
