"""Joining grouping rows with the graph: pages, ids, briefings."""

import pytest

from tests.api.helpers import BRIEFING, crisis_row, make_parts, page
from wikipulse.api.events import (
    briefing_out,
    group_pages,
    page_url,
    parse_event_id,
)
from wikipulse.qa.graph import EventOverview


def test_group_pages_counts_distinct_pages_strongest_first() -> None:
    groups = group_pages(crisis_row().pages)
    assert [(g.title, g.spikes, g.score) for g in groups] == [
        ("Ueno Bank", 1, 9.19),
        ("Santiago Peña", 2, 6.4),
    ]
    pena = groups[1]
    assert pena.first_started_at == crisis_row().pages[1].started_at
    assert pena.last_ended_at == crisis_row().pages[2].ended_at


def test_a_page_with_an_active_spike_has_not_ended() -> None:
    pages = [page("A", 0, 3.0), page("A", 60, 4.0, ended=False)]
    assert group_pages(pages)[0].last_ended_at is None


@pytest.mark.parametrize(
    ("event_id", "expected"),
    [
        ("12", ("live", 12)),
        (" 12 ", ("live", 12)),
        ("03b-default-fix:341", ("03b-default-fix", 341)),
        ("test.run_1:7", ("test.run_1", 7)),
    ],
)
def test_parse_event_id(event_id, expected) -> None:
    assert parse_event_id(event_id, "live") == expected


@pytest.mark.parametrize("event_id", ["", "abc", "run:", ":12", "run:12:3", "1" * 10, "a b:1"])
def test_parse_event_id_rejects_other_text(event_id) -> None:
    with pytest.raises(ValueError, match="is not an event id"):
        parse_event_id(event_id, "live")


def test_page_url_quotes_the_title() -> None:
    assert page_url("Santiago Peña") == "https://en.wikipedia.org/wiki/Santiago_Pe%C3%B1a"
    assert page_url("AC/DC") == "https://en.wikipedia.org/wiki/AC%2FDC"


def test_briefing_out_without_metadata_keeps_the_text() -> None:
    briefing = briefing_out(BRIEFING, None)
    assert briefing.text == "Santiago Peña resigned after Ueno Bank collapsed [1][2]."
    assert briefing.sources == [] and briefing.verified is None
    assert briefing_out(None, None) is None and briefing_out("", None) is None


def test_briefing_out_tolerates_fewer_urls_than_sources() -> None:
    overview = EventOverview("k", "t", None, briefing_sources=("A", "B"), briefing_urls=("u",))
    sources = briefing_out("Text [1][2].", overview).sources
    assert [(s.n, s.title, s.url) for s in sources] == [(1, "A", "u"), (2, "B", None)]


def test_the_graph_cache_only_answers_while_the_graph_is_down() -> None:
    parts = make_parts()
    reader = parts.reader
    assert set(reader.overviews(["run:341", "run:407"])) == {"run:341", "run:407"}
    del parts.graph.overviews["run:407"]  # e.g. the event was merged and left the graph
    assert set(reader.overviews(["run:341", "run:407"])) == {"run:341"}
    parts.graph.down = True
    assert set(reader.overviews(["run:341", "run:407"])) == {"run:341"}


def test_health_reports_each_service() -> None:
    parts = make_parts()
    assert parts.reader.health() == (True, True)
    parts.rows.down = True
    parts.graph.down = True
    assert parts.reader.health() == (False, False)
