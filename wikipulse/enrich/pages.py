"""Enrich one page: canonical title, summary, Wikidata ID, categories, outgoing links.

Two APIs, two kinds of request:

1. MediaWiki Action API, `action=query` with `redirects=1` and
   `prop=pageprops|links|categories`. It resolves the title (normalization and
   redirects) to the canonical one, says whether the page exists, whether it is a
   disambiguation page, and lists its article links (namespace 0) and visible
   categories. Long link lists come in pages of 500 (`continue`); we follow them up to
   `max_links`.
2. REST API `page/summary/<canonical title>`: the short extract, Wikidata's short
   description and the Wikidata ID (`wikibase_item`). Skipped for missing pages.

Missing pages, invalid titles, redirects and disambiguation pages all produce a normal
`PageInfo` with a `status`; they are recorded, never raised.

A redirect to a *section* of another page (Methos -> List of Highlander
characters#Recurring characters) keeps the section in `redirect_fragment` and gets no
`wikidata_id` or `description`: those belong to the whole article, not to the topic the
spike is about, and stage 4 uses Wikidata IDs as entity identity.
"""

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from wikipulse.enrich.client import WikiApiError, WikimediaClient

logger = logging.getLogger(__name__)

LINKS_PER_REQUEST = "max"  # 500 for normal clients
CATEGORY_PREFIX = "Category:"


class PageStatus(StrEnum):
    OK = "ok"
    MISSING = "missing"  # no such page (deleted, never created, or a broken redirect)
    DISAMBIGUATION = "disambiguation"
    INVALID = "invalid"  # not a valid title at all
    ERROR = "error"  # enrichment failed (written by the runner, never by `enrich`)


class PageInfo(BaseModel):
    """Everything later stages need to know about one page."""

    model_config = ConfigDict(frozen=True)

    requested_title: str  # the title we were asked about (the spike's title)
    title: str  # canonical title after normalization and redirects
    status: PageStatus
    is_redirect: bool = False
    redirect_fragment: str | None = None  # the section a redirect points to, if any
    page_id: int | None = None
    wikidata_id: str | None = None
    description: str | None = None
    summary: str | None = None
    categories: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    links_truncated: bool = False


@dataclass
class QueryFacts:
    """What the Action API says about a title (merged over `continue` pages)."""

    title: str
    status: PageStatus
    is_redirect: bool = False
    redirect_fragment: str | None = None
    page_id: int | None = None
    wikidata_id: str | None = None
    categories: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    links_truncated: bool = False


def query_params(title: str) -> dict[str, Any]:
    """Parameters of the first `action=query` request for one title."""
    return {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "titles": title,
        "redirects": "1",
        "prop": "pageprops|links|categories",
        "ppprop": "wikibase_item|disambiguation",
        "plnamespace": "0",
        "pllimit": LINKS_PER_REQUEST,
        "cllimit": "max",
        "clshow": "!hidden",  # skip maintenance categories like "CS1 errors"
    }


def summary_url(rest_url: str, title: str) -> str:
    """REST URL of a page summary; the title goes in the path, so `/` must be escaped."""
    return f"{rest_url}/page/summary/{quote(title.replace(' ', '_'), safe='')}"


def strip_category_prefix(name: str) -> str:
    return name.removeprefix(CATEGORY_PREFIX)


def resolve_title(requested: str, query: dict[str, Any]) -> tuple[str, bool, str | None]:
    """Follow the `normalized` and `redirects` lists.

    Returns (final title, followed a redirect?, section the last redirect points to).
    """
    title = requested
    for item in query.get("normalized", []):
        if item.get("from") == title:
            title = item["to"]
    redirected, fragment = False, None
    for item in query.get("redirects", []):
        if item.get("from") == title:
            title = item["to"]
            redirected = True
            fragment = item.get("tofragment") or None
    return title, redirected, fragment


def find_page(title: str, query: dict[str, Any]) -> dict[str, Any] | None:
    pages = query.get("pages", [])
    for page in pages:
        if page.get("title") == title:
            return page
    return pages[0] if len(pages) == 1 else None


def parse_first_query(requested: str, body: dict[str, Any]) -> QueryFacts:
    """Read title, status, IDs, links and categories from the first query response."""
    query = body.get("query", {})
    title, redirected, fragment = resolve_title(requested, query)
    page = find_page(title, query)
    flags = {"is_redirect": redirected, "redirect_fragment": fragment}
    if page is None or page.get("invalid"):
        return QueryFacts(title=title, status=PageStatus.INVALID, **flags)
    title = page.get("title", title)
    if page.get("missing"):
        return QueryFacts(title=title, status=PageStatus.MISSING, **flags)
    props = page.get("pageprops", {})
    facts = QueryFacts(
        title=title,
        status=PageStatus.DISAMBIGUATION if "disambiguation" in props else PageStatus.OK,
        **flags,
        page_id=page.get("pageid"),
        wikidata_id=props.get("wikibase_item"),
    )
    add_lists(facts, page)
    return facts


def add_lists(facts: QueryFacts, page: dict[str, Any]) -> None:
    """Append the links (articles only) and categories of one response's page entry."""
    facts.links.extend(link["title"] for link in page.get("links", []) if link.get("ns") == 0)
    facts.categories.extend(strip_category_prefix(c["title"]) for c in page.get("categories", []))


async def fetch_query_facts(
    client: WikimediaClient, api_url: str, requested: str, max_links: int
) -> QueryFacts:
    """Run the `action=query` request, following `continue` until done or `max_links`."""
    params = query_params(requested)
    body = (await client.get_json(api_url, params)).body
    facts = parse_first_query(requested, body)
    while "continue" in body and facts.status in (PageStatus.OK, PageStatus.DISAMBIGUATION):
        if len(facts.links) >= max_links:
            facts.links_truncated = True
            break
        params = {**query_params(requested), **body["continue"]}
        body = (await client.get_json(api_url, params)).body
        page = find_page(facts.title, body.get("query", {}))
        if page is not None:
            add_lists(facts, page)
    if len(facts.links) > max_links:
        facts.links, facts.links_truncated = facts.links[:max_links], True
    return facts


async def fetch_summary(client: WikimediaClient, rest_url: str, title: str) -> dict[str, Any]:
    """The REST page summary, or {} if the REST API doesn't know the page (HTTP 404)."""
    response = await client.get_json(summary_url(rest_url, title))
    if response.status == 404 or not isinstance(response.body, dict):
        logger.info("No summary for %r (HTTP %d)", title, response.status)
        return {}
    return response.body


def build_page_info(requested: str, facts: QueryFacts, summary: dict[str, Any]) -> PageInfo:
    status = facts.status
    if summary.get("type") == "disambiguation":
        status = PageStatus.DISAMBIGUATION
    # A section of an article is not the article: its Wikidata item and description
    # would name the wrong entity. The summary, links and categories stay as context.
    whole_page = facts.redirect_fragment is None
    wikidata_id = summary.get("wikibase_item") or facts.wikidata_id
    description = summary.get("description") or None
    return PageInfo(
        requested_title=requested,
        title=facts.title,
        status=status,
        is_redirect=facts.is_redirect,
        redirect_fragment=facts.redirect_fragment,
        page_id=facts.page_id or summary.get("pageid"),
        wikidata_id=wikidata_id if whole_page else None,
        description=description if whole_page else None,
        summary=summary.get("extract") or None,
        categories=tuple(facts.categories),
        links=tuple(facts.links),
        links_truncated=facts.links_truncated,
    )


async def enrich(
    title: str,
    client: WikimediaClient,
    api_url: str,
    rest_url: str,
    max_links: int = 5000,
) -> PageInfo:
    """Everything about one page. Raises `WikiApiError` only if the APIs keep failing."""
    if not title.strip():
        return PageInfo(requested_title=title, title=title, status=PageStatus.INVALID)
    facts = await fetch_query_facts(client, api_url, title, max_links)
    if facts.status in (PageStatus.MISSING, PageStatus.INVALID):
        return build_page_info(title, facts, {})
    summary = await fetch_summary(client, rest_url, facts.title)
    return build_page_info(title, facts, summary)


class Enricher:
    """`enrich()` with the client and settings bound, so callers just pass a title."""

    def __init__(
        self, client: WikimediaClient, api_url: str, rest_url: str, max_links: int = 5000
    ) -> None:
        self.client = client
        self.api_url = api_url
        self.rest_url = rest_url
        self.max_links = max_links

    async def enrich(self, title: str) -> PageInfo:
        return await enrich(title, self.client, self.api_url, self.rest_url, self.max_links)


__all__ = ["Enricher", "PageInfo", "PageStatus", "WikiApiError", "enrich"]
