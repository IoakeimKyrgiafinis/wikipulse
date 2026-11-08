"""Event pages and the text of their recent changes (saved Wikimedia answers, no network)."""

from datetime import UTC, datetime, timedelta

import httpx

from tests.enrich.helpers import make_client
from tests.graph.helpers import API_URL, SavedWikimedia, saff_page
from wikipulse.graph.inputs import (
    changes_text,
    clean_wikitext,
    fetch_changes,
    pages_from_rows,
    revisions_params,
    trim,
)

T = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def row(title: str, minute: int, score: float, **page) -> dict:
    values = {
        "title": title,
        "first_edit_at": T + timedelta(minutes=minute),
        "last_edit_at": T + timedelta(minutes=minute + 20),
        "score": score,
        "page_status": "ok",
        "canonical_title": title,
        "wikidata_id": None,
        "description": None,
        "summary": f"About {title}.",
        "links": [],
    }
    return values | page


def test_two_spikes_of_one_page_become_one_page_spanning_both_bursts() -> None:
    rows = [
        row("Susie Wiles", 0, 4.0, wikidata_id="Q113560461"),
        row("Susan Wiles", 90, 9.0, canonical_title="Susie Wiles"),
        row("Donald Trump", 10, 6.0),
    ]
    pages = pages_from_rows(rows)
    assert [p.title for p in pages] == ["Susie Wiles", "Donald Trump"]  # by score
    wiles = pages[0]
    assert wiles.spike_titles == ("Susie Wiles", "Susan Wiles")
    assert wiles.wikidata_id == "Q113560461"
    assert wiles.first_edit_at == T and wiles.last_edit_at == T + timedelta(minutes=110)
    assert wiles.score == 9.0


def test_pages_that_are_not_enriched_or_failed_keep_only_their_spike_title() -> None:
    rows = [
        row("Unknown page", 0, 3.0, page_status=None, canonical_title=None, summary=None),
        row("Broken", 0, 2.0, page_status="error", wikidata_id="Q1", summary="old"),
    ]
    unknown, broken = pages_from_rows(rows)
    assert unknown.title == "Unknown page" and unknown.summary is None
    assert broken.wikidata_id is None and broken.summary is None


def test_revisions_are_asked_for_the_burst_in_event_time() -> None:
    params = revisions_params(saff_page())
    assert params["rvstart"] == "2026-11-05T16:03:04Z"  # last edit + 1 minute
    assert params["rvend"] == "2026-11-05T15:50:54Z"  # first edit - 1 minute
    assert params["rvdir"] == "older" and params["titles"] == "2026 SAFF Championship"


def test_clean_wikitext_keeps_the_words_and_drops_the_markup() -> None:
    line = (
        "'''[[Santiago Peña|Peña]]''' dismissed the [[Central Bank of Paraguay]] chief"
        '<ref name="abc">{{cite web|url=https://x.org|title=T}}</ref> on '
        "{{date|5 November 2026}}[[File:Pena.jpg|thumb]]<br />see [https://example.org the report]"
    )
    assert clean_wikitext(line) == (
        "Peña dismissed the Central Bank of Paraguay chief on , see the report"
    )
    assert clean_wikitext("== Aftermath ==") == "Aftermath:"
    assert clean_wikitext("{{Infobox person") == ""


def test_changes_text_puts_sentences_before_fields_and_drops_tables() -> None:
    def added(text: str) -> str:
        return f'<td class="diff-addedline diff-side-added"><div>{text}</div></td>'

    diff = "".join(
        [
            added("| birth_place = [[Cox&#039;s Bazar]], Bangladesh"),
            added("He scored the winning goal against [[Bhutan national football team|Bhutan]]."),
            added('rowspan="2"|Mohun Bagan Super Giant'),
            added("res9 = L/W/W/W/W///////"),
            added("He scored the winning goal against [[Bhutan]]."),  # same text again
            '<td class="diff-deletedline diff-side-deleted"><div>Removed text here.</div></td>',
        ]
    )
    assert changes_text(diff, 1000) == (
        "He scored the winning goal against Bhutan.\nbirth place: Cox's Bazar, Bangladesh"
    )


def test_trim_cuts_at_a_word_boundary() -> None:
    assert trim("one two three", 100) == "one two three"
    assert trim("one two three", 9) == "one two …"


async def test_fetch_changes_reads_the_burst_from_saved_answers() -> None:
    transport = SavedWikimedia()
    async with make_client(transport) as client:
        page = await fetch_changes(client, API_URL, saff_page(), max_chars=2000)
    assert page.revision == 1383918312  # the newest revision of the burst
    assert transport.actions() == ["query/revisions", "compare/diff"]
    compare = transport.requests[1].url.params
    assert (compare["fromrev"], compare["torev"]) == ("1383911557", "1383918312")
    assert "matches: 2" in page.changes
    assert "<" not in page.changes and "{{" not in page.changes


async def test_a_page_created_during_its_burst_has_no_diff() -> None:
    body = {"query": {"pages": [{"title": "New", "revisions": [{"revid": 5, "parentid": 0}]}]}}
    seen = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["action"])
        return httpx.Response(200, json=body)

    async with make_client(httpx.MockTransport(answer)) as client:
        page = await fetch_changes(client, API_URL, saff_page(title="New"), max_chars=500)
    assert (page.revision, page.changes) == (5, "")
    assert seen == ["query"]


async def test_a_failed_diff_leaves_the_changes_empty() -> None:
    revisions = {"query": {"pages": [{"title": "X", "revisions": [{"revid": 9, "parentid": 8}]}]}}

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params["action"] == "compare":
            return httpx.Response(200, json={"error": {"code": "nosuchrevid", "info": "gone"}})
        return httpx.Response(200, json=revisions)

    async with make_client(httpx.MockTransport(answer)) as client:
        page = await fetch_changes(client, API_URL, saff_page(title="X"), max_chars=500)
    assert (page.revision, page.changes) == (9, "")
