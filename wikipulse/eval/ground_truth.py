"""Ground truth for detection: Wikipedia's "Current events" portal, one page per day.

Each day has a page `Portal:Current events/<YYYY> <Month> <D>` listing that day's news
as bullet points, with links to the articles involved. For each day we fetch, through
the MediaWiki Action API (`action=parse`, `prop=links|wikitext`):

- **all links**: every article (namespace 0) the page links to,
- **lead links**: the first link of each bullet (each portal entry), usually its subject.

Links are resolved through redirects (`action=query&redirects=1`), because the detector
sees edits to the redirect *target*: the portal may link "Kenyan", the article is "Kenya".

Responses are cached in `data/ground_truth/` (see `WikiClient`). A day's page keeps
changing for a while, so a cached copy is used for good only once it was fetched at
least `FINAL_AFTER` after the day; before that it is refreshed after `RECENT_TTL`.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from wikipulse.eval.wiki_client import WikiClient

logger = logging.getLogger(__name__)

PORTAL_PREFIX = "Portal:Current events/"
FINAL_AFTER = timedelta(days=3)  # a portal day is rarely edited after this
RECENT_TTL = timedelta(hours=6)  # how long a copy of a still-changing day is reused
REDIRECT_BATCH = 50  # the API's limit on titles per query for normal clients

# `[[Target]]`, `[[Target|label]]`, `[[Target#Section|label]]`. Nested brackets don't
# occur in link targets, so a simple pattern is enough.
WIKILINK = re.compile(r"\[\[([^\[\]|]+)(?:\|[^\[\]]*)?\]\]")
BULLET = re.compile(r"^(\*+)\s*(.*)$")
NEWS_START = "<!-- All news items below this line -->"
NEWS_END = "<!-- All news items above this line -->"


def portal_title(day: date) -> str:
    """`Portal:Current events/2026 November 6` (no leading zero on the day)."""
    return f"{PORTAL_PREFIX}{day.year} {day:%B} {day.day}"


def normalize_title(raw: str) -> str | None:
    """A link target as MediaWiki would title the article, or None if it isn't one.

    Drops the section (`#...`), turns underscores into spaces, collapses whitespace and
    upper-cases the first letter. Links into other namespaces (`File:`, `Category:`,
    `Portal:`...) and interwiki links (`:fr:...`) return None.
    """
    title = raw.split("#", 1)[0].replace("_", " ")
    title = " ".join(title.split())
    if title.startswith(":"):
        title = title[1:].strip()
    if not title or (":" in title and _has_namespace_prefix(title)):
        return None
    return title[0].upper() + title[1:]


# Namespace prefixes that make a link point outside the article namespace. Article
# titles may contain colons ("Ramayana: Part 1"), so only these prefixes are excluded.
# Interwiki links to other languages (`[[:fr:Paris]]`) are rare on the portal; the
# lead links are checked against the API's own link list anyway.
_NAMESPACES = {
    "file", "image", "category", "portal", "wikipedia", "wp", "template", "help", "user",
    "talk", "draft", "module", "special", "media", "mediawiki", "wikt", "wiktionary",
    "commons", "meta", "species", "wikinews", "wikiquote", "wikisource", "wikidata",
    "wikivoyage",
}  # fmt: skip


def _has_namespace_prefix(title: str) -> bool:
    prefix = title.split(":", 1)[0].strip().lower()
    return prefix in _NAMESPACES or prefix.endswith(" talk")


@dataclass(frozen=True)
class PortalEntry:
    """One bullet of a portal page: its depth (number of `*`) and its article links."""

    depth: int
    text: str
    links: tuple[str, ...]

    @property
    def lead(self) -> str | None:
        return self.links[0] if self.links else None


def news_section(wikitext: str) -> str:
    """The part of the page between the "All news items" comments (all of it if absent)."""
    start = wikitext.find(NEWS_START)
    end = wikitext.find(NEWS_END)
    if start == -1 or end == -1 or end < start:
        return wikitext
    return wikitext[start + len(NEWS_START) : end]


def extract_entries(wikitext: str) -> list[PortalEntry]:
    """Every bullet line of the news section, with its article links in page order."""
    entries = []
    for line in news_section(wikitext).splitlines():
        match = BULLET.match(line.strip())
        if not match:
            continue  # category headings ('''Law and crime'''), blank lines, templates
        links = []
        for target in WIKILINK.findall(match.group(2)):
            title = normalize_title(target)
            if title is not None:
                links.append(title)
        entries.append(PortalEntry(len(match.group(1)), match.group(2), tuple(links)))
    return entries


def lead_links(wikitext: str) -> list[str]:
    """The first article link of each portal entry (bullet), without duplicates."""
    return _unique(entry.lead for entry in extract_entries(wikitext) if entry.lead)


def extract_links(parse: dict[str, Any]) -> list[str]:
    """All article links (namespace 0) from an `action=parse&prop=links` response."""
    return _unique(link["title"] for link in parse.get("links", []) if link.get("ns") == 0)


def _unique(items: Any) -> list[str]:
    return list(dict.fromkeys(items))


@dataclass
class PortalDay:
    """The ground truth for one day. `resolve` maps each link to its article title."""

    day: date
    links: list[str]
    leads: list[str]
    resolve: dict[str, str] = field(default_factory=dict)
    missing: bool = False  # the portal page doesn't exist (yet)

    def article(self, link: str) -> str:
        return self.resolve.get(link, link)

    def articles(self, leads_only: bool = False) -> list[str]:
        """Distinct article titles (after redirects) of all links or of the lead links."""
        return _unique(self.article(link) for link in (self.leads if leads_only else self.links))

    def mentions(self, title: str) -> bool:
        """Does this day's portal link to `title` (directly or through a redirect)?"""
        return title in self._all_titles()

    def _all_titles(self) -> set[str]:
        return set(self.links) | set(self.leads) | {self.article(t) for t in self.links}


def freshness_rule(day: date):
    """`is_fresh` for a cached response about portal day `day` (see module docstring)."""
    final_from = datetime(day.year, day.month, day.day, tzinfo=UTC) + FINAL_AFTER

    def is_fresh(fetched_at: datetime) -> bool:
        return fetched_at >= final_from or datetime.now(UTC) - fetched_at < RECENT_TTL

    return is_fresh


def parse_portal_response(day: date, body: dict[str, Any]) -> PortalDay:
    """Links and lead links from the API's answer (a missing page gives an empty day)."""
    if "error" in body:
        if body["error"].get("code") == "missingtitle":
            return PortalDay(day, [], [], missing=True)
        raise RuntimeError(f"MediaWiki API error for {portal_title(day)}: {body['error']}")
    parse = body["parse"]
    wikitext = parse.get("wikitext", "")
    if isinstance(wikitext, dict):  # formatversion=1 wraps it as {"*": text}
        wikitext = wikitext.get("*", "")
    links = extract_links(parse)
    leads = lead_links(wikitext)
    # Lead links come from the wikitext; keep only real article links the API also lists
    # (the API knows the exact titles), unless the API returned no links at all.
    if links:
        known = set(links)
        leads = [lead for lead in leads if lead in known]
    return PortalDay(day, links, leads)


def parse_redirect_response(body: dict[str, Any]) -> dict[str, str]:
    """`{link title: final article title}` from `action=query&redirects=1`."""
    query = body.get("query", {})
    steps = {item["from"]: item["to"] for item in query.get("normalized", [])}
    steps.update({item["from"]: item["to"] for item in query.get("redirects", [])})
    resolved = {}
    for start in steps:
        title, seen = start, set()
        while title in steps and title not in seen:  # follow chains, stop on loops
            seen.add(title)
            title = steps[title]
        resolved[start] = title
    return resolved


def fetch_portal_day(client: WikiClient, api_url: str, day: date) -> PortalDay:
    """Fetch (or load from the cache) one day's portal page and resolve its links."""
    params = {
        "action": "parse",
        "page": portal_title(day),
        "prop": "links|wikitext",
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
    }
    is_fresh = freshness_rule(day)
    body = client.get_json(api_url, params, label=f"portal-{day.isoformat()}", is_fresh=is_fresh)
    portal = parse_portal_response(day, body)
    if portal.missing:
        logger.warning("No portal page for %s (yet)", day)
        return portal
    for start in range(0, len(portal.links), REDIRECT_BATCH):
        batch = portal.links[start : start + REDIRECT_BATCH]
        params = {
            "action": "query",
            "titles": "|".join(batch),
            "redirects": "1",
            "format": "json",
            "formatversion": "2",
        }
        label = f"redirects-{day.isoformat()}-{start // REDIRECT_BATCH}"
        body = client.get_json(api_url, params, label=label, is_fresh=is_fresh)
        portal.resolve.update(parse_redirect_response(body))
    return portal


def fetch_portal_days(client: WikiClient, api_url: str, first: date, last: date) -> list[PortalDay]:
    """Portal days from `first` to `last`, inclusive."""
    days = []
    day = first
    while day <= last:
        days.append(fetch_portal_day(client, api_url, day))
        day += timedelta(days=1)
    return days
