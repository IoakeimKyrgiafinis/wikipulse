"""Entity linking: the event's pages first, then their links, then Wikidata search."""

import httpx

from tests.enrich.helpers import make_client
from tests.graph.helpers import API_URL, WIKIDATA_URL, SavedWikimedia, saff_page
from wikipulse.graph.link import (
    UNLINKED,
    Link,
    Linker,
    context_words,
    link_index,
    page_index,
    parse_pageprops,
    pick_search_result,
)


async def link(names: list[str], pages, transport) -> dict[str, Link]:
    async with make_client(transport) as client:
        return await Linker(client, API_URL, WIKIDATA_URL).link(names, pages)


async def test_linking_tries_pages_then_links_then_search() -> None:
    transport = SavedWikimedia()
    pages = [saff_page()]
    names = [
        "2026 SAFF Championship",
        "Bangladesh Football Federation",
        "Bangladesh",
        "South Asian Football Federation",
        "Bend",
    ]
    links = await link(names, pages, transport)
    assert links["South Asian Football Federation"].method == "link"
    assert links["2026 SAFF Championship"] == Link("Q131753497", "page", "2026 SAFF Championship")
    assert links["Bangladesh Football Federation"] == Link(
        "Q806536", "link", "Bangladesh Football Federation"
    )
    assert links["Bangladesh"] == Link("Q902", "link", "Bangladesh")
    # "Bend" is not linked from the page, so only Wikidata search can find it.
    assert links["Bend"].method == "search"
    # One batched pageprops query for the linked titles, then one search, in that order.
    assert transport.actions() == ["query/pageprops", "wbsearchentities"]
    assert transport.requests[1].url.params["search"] == "Bend"


async def test_a_name_found_on_the_event_pages_needs_no_request() -> None:
    def no_requests(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.url}")

    page = saff_page(links=())
    links = await link(["2026 SAFF championship"], [page], httpx.MockTransport(no_requests))
    assert links["2026 SAFF championship"].wikidata_id == "Q131753497"


async def test_names_nothing_resolves_stay_unlinked() -> None:
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"search": []})

    links = await link(["Kaylub Giamanco"], [saff_page(links=())], httpx.MockTransport(empty))
    assert links["Kaylub Giamanco"] == UNLINKED


def test_page_titles_match_without_their_qualifier_unless_ambiguous() -> None:
    footballer = saff_page(
        title="Mohammad Ibrahim (footballer, born 1997)",
        spike_titles=("Mohammad Ibrahim (footballer, born 1997)",),
        wikidata_id="Q60734382",
    )
    assert page_index([footballer])["mohammad ibrahim"] == ("Q60734382", footballer.title)
    links = link_index([saff_page(links=("Paris", "Paris (band)", "Georgia (country)"))])
    assert links["paris"] == "Paris"  # the exact title wins
    assert links["paris (band)"] == "Paris (band)"
    assert links["georgia"] == "Georgia (country)"
    ambiguous = link_index([saff_page(links=("Georgia (country)", "Georgia (U.S. state)"))])
    assert "georgia" not in ambiguous


def test_pageprops_follow_redirects_but_not_to_sections_or_disambiguation_pages() -> None:
    body = {
        "query": {
            "normalized": [{"from": "susie wiles", "to": "Susie wiles"}],
            "redirects": [
                {"from": "Susie wiles", "to": "Susie Wiles"},
                {"from": "Methos", "to": "List of Highlander characters", "tofragment": "M"},
            ],
            "pages": [
                {"title": "Susie Wiles", "pageprops": {"wikibase_item": "Q113560461"}},
                {"title": "List of Highlander characters", "pageprops": {"wikibase_item": "Q9"}},
                {"title": "Shipley", "pageprops": {"wikibase_item": "Q5", "disambiguation": ""}},
                {"title": "Nowhere", "missing": True},
            ],
        }
    }
    found = parse_pageprops(["susie wiles", "Methos", "Shipley", "Nowhere"], body)
    assert found == {"susie wiles": ("Q113560461", "Susie Wiles")}


def test_search_takes_exact_matches_only_and_prefers_the_events_context() -> None:
    body = {
        "search": [
            {"id": "Q1", "label": "Bend", "description": "family name"},
            {"id": "Q2", "label": "Bending", "match": {"text": "Bending"}},
            {"id": "Q3", "label": "Bend", "description": "town in Victoria, Australia"},
            {"id": "Q4", "label": "Bend", "description": "city in Deschutes County, Oregon"},
        ]
    }
    assert pick_search_result("Bend", body).wikidata_id == "Q3"  # no context: best rank
    weak = context_words([saff_page(summary="A school shooting in Bend, Oregon.")])
    assert pick_search_result("Bend", body, weak).wikidata_id == "Q3"  # one word: too weak
    context = context_words(
        [saff_page(summary="A school shooting in Bend, Oregon, a city in Deschutes County.")]
    )
    assert pick_search_result("Bend", body, context) == Link("Q4", "search", "Bend")
    alias = {"search": [{"id": "Q30", "label": "United States", "match": {"text": "USA"}}]}
    assert pick_search_result("USA", alias).wikidata_id == "Q30"
    assert pick_search_result("Bendigo", body) == UNLINKED


def test_search_never_links_a_named_entity_to_a_creative_work() -> None:
    body = {
        "search": [
            {"id": "Q1", "label": "Los Angeles", "description": "album of the band X"},
            {"id": "Q2", "label": "Los Angeles", "description": "1980 film directed by Y"},
            {"id": "Q3", "label": "Los Angeles", "description": "Croatian TV series"},
            {"id": "Q65", "label": "Los Angeles", "description": "city in California"},
        ]
    }
    assert pick_search_result("Los Angeles", body, entity_type="place").wikidata_id == "Q65"
    assert pick_search_result("Los Angeles", body, entity_type="other").wikidata_id == "Q1"
    person = {"search": [{"id": "Q7", "label": "Ann Lee", "description": "American film director"}]}
    assert pick_search_result("Ann Lee", person, entity_type="person").wikidata_id == "Q7"
