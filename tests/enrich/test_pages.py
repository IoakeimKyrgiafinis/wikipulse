"""Parsing pages from saved API responses: redirects, missing, disambiguation, links."""

import httpx
import pytest

from tests.enrich.helpers import API_URL, REST_URL, FixtureTransport, make_client
from wikipulse.enrich.pages import (
    Enricher,
    PageStatus,
    enrich,
    resolve_title,
    summary_url,
)


async def run_enrich(title: str, transport: FixtureTransport | None = None, **kwargs):
    transport = transport or FixtureTransport()
    async with make_client(transport) as client:
        info = await enrich(title, client, API_URL, REST_URL, **kwargs)
    return info, transport


async def test_ordinary_page() -> None:
    info, transport = await run_enrich("Syed Soharwardy")
    assert info.status is PageStatus.OK
    assert info.title == info.requested_title == "Syed Soharwardy"
    assert not info.is_redirect
    assert info.wikidata_id == "Q19898422"
    assert info.page_id is not None
    assert info.summary and "Soharwardy" in info.summary
    assert len(info.links) == 47
    assert len(info.categories) == 12
    assert not any(c.startswith("Category:") for c in info.categories)
    assert not info.links_truncated
    assert len(transport.requests) == 2  # one query, one summary


async def test_redirect_is_followed_and_recorded() -> None:
    info, _ = await run_enrich("Susan Wiles")
    assert info.requested_title == "Susan Wiles"
    assert info.title == "Susie Wiles"
    assert info.is_redirect
    assert info.status is PageStatus.OK
    assert info.wikidata_id == "Q113560461"
    assert info.summary


async def test_normalized_title_is_not_a_redirect() -> None:
    info, transport = await run_enrich("frozen yogurt")
    assert info.title == "Frozen yogurt"
    assert not info.is_redirect
    assert transport.requests[1].url.path.endswith("/page/summary/Frozen_yogurt")


async def test_disambiguation_page() -> None:
    info, _ = await run_enrich("Shipley")
    assert info.status is PageStatus.DISAMBIGUATION
    assert info.links  # the pages it points to, useful later


async def test_missing_page_is_recorded_without_a_summary_request() -> None:
    info, transport = await run_enrich("WikiPulse test page that does not exist")
    assert info.status is PageStatus.MISSING
    assert info.summary is None and info.wikidata_id is None and info.links == ()
    assert len(transport.requests) == 1


async def test_invalid_title() -> None:
    info, transport = await run_enrich("Invalid [title]")
    assert info.status is PageStatus.INVALID
    assert len(transport.requests) == 1


async def test_empty_title_needs_no_request() -> None:
    info, transport = await run_enrich("  ")
    assert info.status is PageStatus.INVALID
    assert transport.requests == []


async def test_slash_in_title_is_escaped_in_the_rest_url() -> None:
    info, transport = await run_enrich("OS/2")
    assert info.status is PageStatus.OK and info.wikidata_id == "Q189794"
    assert summary_url(REST_URL, "OS/2").endswith("/page/summary/OS%2F2")
    assert transport.requests[1].url.raw_path.endswith(b"/page/summary/OS%2F2")


async def test_long_link_lists_follow_continue() -> None:
    info, transport = await run_enrich("Stephen Miller")
    assert len(info.links) == 1342
    assert len(set(info.links)) == len(info.links)
    assert len(info.categories) == 28
    assert not info.links_truncated
    assert len(transport.requests) == 4  # three query pages, one summary


async def test_links_are_capped() -> None:
    info, transport = await run_enrich("Stephen Miller", max_links=600)
    assert len(info.links) == 600
    assert info.links_truncated
    assert len(transport.requests) == 3  # stops after the second query page


async def test_missing_summary_keeps_the_query_facts() -> None:
    transport = FixtureTransport(
        overrides={"/page/summary/": lambda request: httpx.Response(404, json={"type": "x"})}
    )
    info, _ = await run_enrich("Syed Soharwardy", transport)
    assert info.status is PageStatus.OK
    assert info.summary is None
    assert info.wikidata_id == "Q19898422"  # from the Action API's pageprops


async def test_enricher_binds_the_settings() -> None:
    async with make_client(FixtureTransport()) as client:
        info = await Enricher(client, API_URL, REST_URL).enrich("Shipley")
    assert info.status is PageStatus.DISAMBIGUATION


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({}, ("a b", False, None)),
        ({"normalized": [{"from": "a b", "to": "A b"}]}, ("A b", False, None)),
        (
            {
                "normalized": [{"from": "a b", "to": "A b"}],
                "redirects": [{"from": "A b", "to": "C"}],
            },
            ("C", True, None),
        ),
        (
            {"redirects": [{"from": "a b", "to": "C", "tofragment": "Early life"}]},
            ("C", True, "Early life"),
        ),
    ],
)
def test_resolve_title(query: dict, expected: tuple[str, bool, str | None]) -> None:
    assert resolve_title("a b", query) == expected


SECTION_QUERY = {
    "batchcomplete": True,
    "query": {
        "redirects": [
            {
                "from": "Methos",
                "to": "List of Highlander characters",
                "tofragment": "Recurring characters",
            }
        ],
        "pages": [
            {
                "pageid": 1234,
                "ns": 0,
                "title": "List of Highlander characters",
                "pageprops": {"wikibase_item": "Q6593960"},
                "links": [{"ns": 0, "title": "Duncan MacLeod"}],
                "categories": [{"ns": 14, "title": "Category:Highlander (franchise) characters"}],
            }
        ],
    },
}
SECTION_SUMMARY = {
    "type": "standard",
    "title": "List of Highlander characters",
    "pageid": 1234,
    "wikibase_item": "Q6593960",
    "description": "Wikimedia list article",
    "extract": "This is a list of characters from the Highlander franchise.",
}


async def test_redirect_to_a_section_has_no_wikidata_id() -> None:
    """The article's Wikidata item is not the identity of a topic inside one section."""
    transport = FixtureTransport(
        overrides={
            "/w/api.php": lambda request: httpx.Response(200, json=SECTION_QUERY),
            "/page/summary/": lambda request: httpx.Response(200, json=SECTION_SUMMARY),
        }
    )
    info, _ = await run_enrich("Methos", transport)
    assert info.status is PageStatus.OK
    assert info.title == "List of Highlander characters"
    assert info.is_redirect and info.redirect_fragment == "Recurring characters"
    assert info.wikidata_id is None and info.description is None
    assert info.summary  # the article's lead is still useful context
    assert info.links == ("Duncan MacLeod",)
    assert info.categories == ("Highlander (franchise) characters",)


async def test_redirect_to_a_whole_page_has_no_fragment() -> None:
    info, _ = await run_enrich("Susan Wiles")
    assert info.redirect_fragment is None
    assert info.wikidata_id == "Q113560461"
