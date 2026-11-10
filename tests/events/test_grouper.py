"""Online grouping: assignment, merging, ending, titles and determinism."""

from dataclasses import replace

import pytest

from tests.events.helpers import ELECTION, ELECTION_ISH, FOOTBALL, at, page, unit
from wikipulse.config import EventsConfig
from wikipulse.events.grouper import ACTIVE, ENDED, MERGED, Event, Grouper, Member
from wikipulse.events.similarity import SpikePage

CONFIG = EventsConfig()  # join threshold 0.55; a spike is over 30 min after its last edit


def test_the_first_spike_starts_event_one() -> None:
    grouper = Grouper(CONFIG)
    assignment = grouper.add(page(1, "2026 Ohio gubernatorial election", embedding=ELECTION))
    assert assignment.created and assignment.event_number == 1 and assignment.score == 1.0
    assert list(grouper.active) == [1]


def test_a_related_spike_joins_and_an_unrelated_one_starts_a_new_event() -> None:
    grouper = Grouper(CONFIG)
    grouper.add(page(1, "Ohio election", links=("Vivek Ramaswamy",), embedding=ELECTION))
    joined = grouper.add(page(2, "Vivek Ramaswamy", minute=20, embedding=ELECTION_ISH))
    assert not joined.created and joined.event_number == 1 and joined.score >= 0.55
    other = grouper.add(page(3, "Benfica", minute=25, embedding=FOOTBALL))
    assert other.created and other.event_number == 2
    assert [p.spike_id for p in grouper.active[1].pages] == [1, 2]


def test_timing_alone_is_not_enough() -> None:
    grouper = Grouper(CONFIG)
    grouper.add(page(1, "A", embedding=ELECTION))
    assert grouper.add(page(2, "B", minute=1, embedding=FOOTBALL)).created


def test_a_deaths_list_does_not_chain_unrelated_deaths_into_one_event() -> None:
    """The 2026-11-05 case: *Deaths in 2026* spikes, then several people on it die/spike
    within the hour. Their summaries are unlike the list's and unlike each other's."""
    names = ("Herbert Blomstedt", "Kimmarie Johnson", "Ali-Asghar Fani")
    deaths_links = tuple(f"Person {i}" for i in range(384)) + names
    grouper = Grouper(CONFIG)
    grouper.add(page(1, "Deaths in 2026", links=deaths_links, embedding=unit(1, 0, 0, 0)))
    people = [
        page(2, names[0], minute=10, embedding=unit(0.45, 0.89, 0, 0)),  # conductor
        page(3, names[1], minute=20, embedding=unit(0.40, 0, 0.92, 0)),  # TV
        page(4, names[2], minute=30, embedding=unit(0.49, 0, 0, 0.87)),  # Iranian politician
    ]
    assignments = [grouper.add(person) for person in people]
    assert all(a.created for a in assignments)
    assert sorted(len(e.pages) for e in grouper.active.values()) == [1, 1, 1, 1]
    # The same day with the gate off: each joins the list's event through its link.
    chained = Grouper(EventsConfig(link_min_cosine=0))
    chained.add(page(1, "Deaths in 2026", links=deaths_links, embedding=unit(1, 0, 0, 0)))
    for person in people:
        chained.add(person)
    assert [len(e.pages) for e in chained.active.values()] == [4]


def test_a_death_with_a_related_summary_still_joins_the_deaths_list() -> None:
    grouper = Grouper(CONFIG)
    grouper.add(page(1, "Deaths in 2026", links=("Lloyd Geering",), embedding=unit(1, 0)))
    joined = grouper.add(page(2, "Lloyd Geering", minute=10, embedding=unit(0.7, 0.714)))
    assert not joined.created and joined.event_number == 1


def test_a_spike_similar_to_two_events_merges_them_into_the_oldest() -> None:
    grouper = Grouper(CONFIG)
    grouper.add(page(1, "Election", embedding=unit(1, 0, 0)))
    grouper.add(page(2, "Candidate", minute=5, embedding=unit(0, 1, 0)))
    assert set(grouper.active) == {1, 2}
    grouper.take_changes()
    # The bridge links to both, and its summary is somewhat like both (cosine 0.71).
    bridge = page(3, "Results", minute=10, links=("Election", "Candidate"), embedding=unit(1, 1, 0))
    assignment = grouper.add(bridge)
    assert assignment.event_number == 1 and assignment.merged == (2,)
    assert list(grouper.active) == [1]
    assert sorted(p.spike_id for p in grouper.active[1].pages) == [1, 2, 3]
    changed = {e.number: e for e in grouper.take_changes()}
    assert changed[2].status == MERGED and changed[2].merged_into == 1
    assert changed[2].members == [] and changed[2].title == "Candidate"  # kept for reading
    assert changed[1].page_count == 3
    assert grouper.event_of[2] == 1


def test_an_event_ends_when_all_its_spikes_are_over() -> None:
    grouper = Grouper(CONFIG)
    grouper.add(page(1, "A", links=("B",), ended_minute=40))
    grouper.add(page(2, "B", minute=10, ended_minute=None))  # still active
    assert grouper.advance(at(80)) == []  # B has not ended
    grouper.update_end(2, at(60))
    assert grouper.advance(at(89)) == []  # B is over only 30 minutes after its last edit
    (ended,) = grouper.advance(at(90))
    assert ended.status == ENDED and ended.ended_at == at(60)
    assert grouper.active == {} and grouper.event_of == {}


def test_a_spike_after_the_event_ended_starts_a_new_event() -> None:
    grouper = Grouper(CONFIG)
    grouper.add(page(1, "A", links=("B",), ended_minute=20))
    late = grouper.add(page(2, "B", minute=51))  # A was over at minute 50
    assert late.created and late.event_number == 2
    early = Grouper(CONFIG)
    early.add(page(1, "A", links=("B",), ended_minute=20))
    assert not early.add(page(2, "B", minute=49)).created


def test_the_title_is_the_highest_scoring_page() -> None:
    grouper = Grouper(CONFIG)
    grouper.add(page(1, "Ohio election", links=("Vivek Ramaswamy",), score=4.0))
    assert grouper.active[1].title == "Ohio election"
    grouper.add(page(2, "Vivek Ramaswamy", minute=5, score=9.5))
    event = grouper.active[1]
    assert event.title == "Vivek Ramaswamy" and event.score == 9.5
    assert event.started_at == at(0) and event.last_spike_at == at(5)


def test_the_same_input_order_gives_the_same_events() -> None:
    def spikes():
        return [
            page(1, "Election", embedding=unit(1, 0, 0), ended_minute=30),
            page(2, "Candidate", minute=5, embedding=unit(0, 1, 0), ended_minute=50),
            page(3, "Results", minute=10, links=("Election", "Candidate"), embedding=unit(0, 0, 1)),
            page(4, "Match", minute=11, embedding=FOOTBALL, ended_minute=20),
            page(5, "Club", minute=90, links=("Match",), embedding=FOOTBALL),
        ]

    def run():
        grouper = Grouper(CONFIG)
        return [grouper.add(p) for p in spikes()], [
            (e.number, e.status, e.title, [p.spike_id for p in e.pages])
            for e in grouper.take_changes()
        ]

    assert run() == run()


def test_a_spike_cannot_be_added_twice() -> None:
    grouper = Grouper(CONFIG)
    grouper.add(page(1, "A"))
    with pytest.raises(ValueError):
        grouper.add(page(1, "A"))


def test_the_clock_never_goes_backwards() -> None:
    grouper = Grouper(CONFIG)
    grouper.advance(at(100))
    grouper.add(page(1, "A", minute=50))  # slightly out of order: still accepted
    assert grouper.clock == at(100)


def test_a_restored_grouper_continues_numbering_and_events() -> None:
    first = page(1, "Election", links=("Candidate",), embedding=ELECTION)
    event = Event(7, [Member(first, 1.0)])
    event.refresh()
    grouper = Grouper(CONFIG, next_number=8, events=[event], clock=at(0))
    assert grouper.add(page(2, "Candidate", minute=3)).event_number == 7
    assert grouper.add(page(3, "Unrelated", minute=4)).event_number == 8
    assert grouper.active[7].status == ACTIVE


def with_id(spike: SpikePage, correlation_id: str) -> SpikePage:
    return replace(spike, correlation_id=correlation_id)


def test_an_event_carries_the_correlation_id_of_its_earliest_spike() -> None:
    grouper = Grouper(CONFIG)
    grouper.add(with_id(page(1, "Election", embedding=unit(1, 0, 0)), "edit-1"))
    grouper.add(with_id(page(2, "Candidate", minute=5, embedding=unit(0, 1, 0)), "edit-2"))
    assert grouper.active[2].correlation_id == "edit-2"
    bridge = page(3, "Results", minute=10, links=("Election", "Candidate"), embedding=unit(1, 1, 0))
    grouper.add(with_id(bridge, "edit-3"))  # merges event 2 into event 1
    assert grouper.active[1].correlation_id == "edit-1"
