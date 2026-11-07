"""Ground truth: portal titles, link extraction, lead links and redirects (no network)."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx

from wikipulse.eval.ground_truth import (
    extract_entries,
    extract_links,
    fetch_portal_day,
    fetch_portal_days,
    freshness_rule,
    lead_links,
    normalize_title,
    parse_portal_response,
    parse_redirect_response,
    portal_title,
)
from wikipulse.eval.wiki_client import WikiClient

FIXTURES = Path(__file__).parent.parent / "fixtures" / "eval"
API = "https://en.wikipedia.org/w/api.php"
NOV6 = date(2026, 11, 6)

WIKITEXT = """{{Current events|year=2026|month=11|day=6|top=yes}}
<!-- All news items below this line -->
'''Armed conflicts and attacks'''
*[[Kivu conflict]]
**[[Allied Democratic Forces insurgency]]
***The death toll from an attack by [[Allied Democratic Forces|ADF]] in [[North Kivu]] rises.
*Two people die in a bus crash in [[Lent, Ain|Lent]], [[Ain]], [[France]]. [https://x.org (AP)]
*[[File:Example.jpg|thumb]] [[Category:Disasters]] [[thomas_Markle#Arrest|Markle]] is arrested.
*A sentence without links.
<!-- All news items above this line -->
{{Current events|year=2026|month=11|day=6|bottom=yes}}
* [[Not news]] outside the news section
"""


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_portal_title_has_no_leading_zero() -> None:
    assert portal_title(date(2026, 11, 6)) == "Portal:Current events/2026 November 6"
    assert portal_title(date(2027, 1, 15)) == "Portal:Current events/2027 January 15"


def test_normalize_title() -> None:
    assert normalize_title("thomas_Markle#Arrest") == "Thomas Markle"
    assert normalize_title("  Lent,  Ain ") == "Lent, Ain"
    assert normalize_title("Ramayana: Part 1") == "Ramayana: Part 1"
    assert normalize_title(":Paris") == "Paris"
    for other in ("File:Example.jpg", "Category:Disasters", "Portal:Current events", "#Top"):
        assert normalize_title(other) is None


def test_entries_are_the_bullets_of_the_news_section() -> None:
    entries = extract_entries(WIKITEXT)
    assert [e.depth for e in entries] == [1, 2, 3, 1, 1, 1]
    assert entries[2].links == ("Allied Democratic Forces", "North Kivu")
    assert entries[4].links == ("Thomas Markle",)  # File: and Category: links dropped
    assert entries[5].lead is None


def test_lead_link_is_the_first_link_of_each_entry() -> None:
    assert lead_links(WIKITEXT) == [
        "Kivu conflict",
        "Allied Democratic Forces insurgency",
        "Allied Democratic Forces",
        "Lent, Ain",
        "Thomas Markle",
    ]


def test_links_from_the_saved_api_response() -> None:
    body = load("portal-2026-11-06.json")
    links = extract_links(body["parse"])
    assert len(links) == 107  # namespace-0 links only; the 3 Portal: links are left out
    assert "Thomas Markle" in links
    assert not any(link.startswith("Portal:") for link in links)


def test_parse_portal_response_keeps_leads_the_api_knows() -> None:
    portal = parse_portal_response(NOV6, load("portal-2026-11-06.json"))
    assert not portal.missing
    assert len(portal.leads) == 26
    assert portal.leads[:3] == [
        "Kivu conflict",
        "Allied Democratic Forces insurgency",
        "Allied Democratic Forces",
    ]
    assert "2026 Paraguay banking crisis" in portal.leads
    assert set(portal.leads) <= set(portal.links)


def test_missing_portal_page_gives_an_empty_day() -> None:
    body = {"error": {"code": "missingtitle", "info": "The page you specified doesn't exist."}}
    portal = parse_portal_response(date(2026, 11, 9), body)
    assert portal.missing and portal.links == [] and portal.leads == []


def test_redirects_are_followed_through_normalization_and_chains() -> None:
    body = {
        "query": {
            "normalized": [{"from": "kenyan", "to": "Kenyan"}],
            "redirects": [
                {"from": "Kenyan", "to": "Kenyans"},
                {"from": "Kenyans", "to": "Demographics of Kenya"},
            ],
        }
    }
    resolved = parse_redirect_response(body)
    assert resolved["kenyan"] == "Demographics of Kenya"
    assert resolved["Kenyan"] == "Demographics of Kenya"


def test_redirects_from_the_saved_api_response() -> None:
    resolved = parse_redirect_response(load("redirects-2026-11-06-1.json"))
    assert resolved["Kenyan"] == "Demographics of Kenya"
    assert resolved["Tampa"] == "Tampa, Florida"


def fixture_transport(requests: list[httpx.Request]) -> httpx.MockTransport:
    """Serves the saved responses for 2026-11-06 and "missing" for any other day."""
    batches = [load(f"redirects-2026-11-06-{n}.json") for n in range(3)]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if params["action"] == "parse":
            if params["page"] == portal_title(NOV6):
                return httpx.Response(200, json=load("portal-2026-11-06.json"))
            return httpx.Response(200, json={"error": {"code": "missingtitle"}})
        first = params["titles"].split("|")[0]
        for body in batches:
            if any(page["title"] == first for page in body["query"]["pages"]):
                return httpx.Response(200, json=body)
        return httpx.Response(200, json={"query": {"pages": []}})

    return httpx.MockTransport(handler)


def make_client(tmp_path: Path, requests: list[httpx.Request]) -> WikiClient:
    return WikiClient(
        "WikiPulse-test/0.1 (test@example.org)",
        tmp_path,
        min_interval_s=0,
        transport=fixture_transport(requests),
    )


def test_fetch_portal_day_resolves_links_and_caches(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    with make_client(tmp_path, requests) as client:
        portal = fetch_portal_day(client, API, NOV6)
        assert len(requests) == 4  # the page, then 107 links in batches of 50
        assert requests[0].headers["User-Agent"].startswith("WikiPulse-test")
        assert portal.mentions("Tampa, Florida")  # through the redirect from "Tampa"
        assert portal.mentions("Tampa")
        assert "Demographics of Kenya" in portal.articles(leads_only=True)

        again = fetch_portal_day(client, API, NOV6)
        assert len(requests) == 4  # everything came from the cache
        assert again.links == portal.links and again.resolve == portal.resolve
        assert client.cache_hits == 4


def test_fetch_portal_days_covers_the_range(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    with make_client(tmp_path, requests) as client:
        days = fetch_portal_days(client, API, date(2026, 11, 5), date(2026, 11, 7))
    assert [d.day for d in days] == [date(2026, 11, 5), NOV6, date(2026, 11, 7)]
    assert [d.missing for d in days] == [True, False, True]


def test_recent_days_are_refreshed_and_old_days_are_final() -> None:
    now = datetime.now(UTC)
    old_day = (now - timedelta(days=30)).date()
    assert freshness_rule(old_day)(now - timedelta(days=20))  # fetched well after the day
    today = now.date()
    is_fresh = freshness_rule(today)
    assert is_fresh(now - timedelta(hours=1))
    assert not is_fresh(now - timedelta(hours=7))
