"""Entity linking: match extracted names to Wikidata IDs.

Three steps, from the most to the least trustworthy evidence:

1. **page**: the name is the title of one of the event's own pages, whose Wikidata ID
   stage 3 already stored ("Santiago Peña" in an event with the page *Santiago Peña*).
2. **link**: the name is the title of an article one of the event's pages links to.
   The page's editors chose that link, so it names the right *Georgia* or *Paris* for
   this story. Their Wikidata IDs come from one batched `action=query&prop=pageprops`
   per 50 titles (redirects followed).
3. **search**: Wikidata's own search (`wbsearchentities`) for the rest. Only a result
   whose label or alias is exactly the name counts, never a disambiguation page or a
   family name, and never a creative work for a person, organization, place or event;
   the best-ranked such result wins unless another fits the event's text clearly better.

A name none of them resolves stays unlinked (`Link.method == "none"`). Every request goes
through the polite, cached `WikimediaClient`.
"""

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wikipulse.enrich.client import WikimediaClient
from wikipulse.graph.inputs import EventPage
from wikipulse.graph.names import normalize_name, without_qualifier

logger = logging.getLogger(__name__)

TITLES_PER_REQUEST = 50  # the Action API's limit for normal clients
SEARCH_LIMIT = 5
# Wikidata items that share a name with everything: never the entity itself.
REJECTED_DESCRIPTIONS = (
    "disambiguation page",
    "family name",
    "given name",
    "surname",
    "list article",
    "wikimedia category",
)


@dataclass(frozen=True)
class Link:
    """How a name was linked: its Wikidata ID, by which step, and the title it matched."""

    wikidata_id: str | None
    method: str  # "page", "link", "search" or "none"
    label: str | None = None  # the Wikipedia title or Wikidata label that matched


UNLINKED = Link(None, "none")


def unique_index(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """key -> value for keys with exactly one distinct value (ambiguous keys dropped)."""
    seen: dict[str, set] = {}
    for key, value in pairs:
        seen.setdefault(key, set()).add(value)
    return {key: next(iter(values)) for key, values in seen.items() if len(values) == 1}


def title_index(titles: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    """normalized title -> value, falling back on titles without their qualifier.

    An exact title always wins; "paris" finds *Paris (band)* only if no other linked
    title is also "Paris" without its qualifier.
    """
    titles = list(titles)
    exact = unique_index((normalize_name(t), v) for t, v in titles)
    loose = unique_index((normalize_name(without_qualifier(t)), v) for t, v in titles)
    return loose | exact


def page_index(pages: Sequence[EventPage]) -> dict[str, tuple[str, str]]:
    """normalized name -> (Wikidata ID, title) for the event's own pages."""
    pairs = []
    for page in pages:
        if page.wikidata_id:
            for title in (page.title, *page.spike_titles):
                pairs.append((title, (page.wikidata_id, page.title)))
    return title_index(pairs)


def link_index(pages: Sequence[EventPage]) -> dict[str, str]:
    """normalized name -> linked article title, over all links of the event's pages."""
    return title_index((title, title) for page in pages for title in page.links)


def pageprops_params(titles: Sequence[str]) -> dict[str, Any]:
    """Action API parameters for the Wikidata ids of page titles."""
    return {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "titles": "|".join(titles),
        "redirects": "1",
        "prop": "pageprops",
        "ppprop": "wikibase_item|disambiguation",
    }


def parse_pageprops(titles: Sequence[str], body: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """requested title -> (Wikidata ID, final title), for pages that have an ID.

    Redirects are followed, but not to a section (that names a part of an article, not
    the entity); missing and disambiguation pages have no usable ID.
    """
    query = body.get("query", {})
    renamed = {item["from"]: item["to"] for item in query.get("normalized", [])}
    redirects = {item["from"]: item for item in query.get("redirects", [])}
    pages = {page.get("title"): page for page in query.get("pages", [])}
    found = {}
    for requested in titles:
        title = renamed.get(requested, requested)
        redirect = redirects.get(title)
        if redirect is not None:
            if redirect.get("tofragment"):
                continue
            title = redirect["to"]
        props = pages.get(title, {}).get("pageprops", {})
        if props.get("wikibase_item") and "disambiguation" not in props:
            found[requested] = (props["wikibase_item"], title)
    return found


def search_params(name: str) -> dict[str, Any]:
    """Wikidata API parameters to search for a name."""
    return {
        "action": "wbsearchentities",
        "format": "json",
        "search": name,
        "language": "en",
        "uselang": "en",
        "type": "item",
        "limit": str(SEARCH_LIMIT),
    }


_WORD = re.compile(r"[^\W\d_]{4,}")


def context_words(pages: Sequence[EventPage]) -> frozenset[str]:
    """Words (4+ letters, lower case) of the event's titles, descriptions and summaries."""
    texts = (f"{p.title} {p.description or ''} {p.summary or ''}" for p in pages)
    return frozenset(word.lower() for text in texts for word in _WORD.findall(text))


# A Wikidata description that names a creative work or a part of a place ("1980 film
# directed by ...", "album of the band X", "painting by Rubens", "French magazine",
# "section of Charleroi"). Such an item is never the person, organization, place or
# event the model meant, even if its label is the same name.
_WORK = re.compile(
    r"\b(album|song|single|painting|film|magazine|novel|episode|television series|tv series|"
    r"video game|hotel|section|sculpture|poem)\b( by| of| directed| in| from|$)"
)
# How many more words with the event a lower-ranked result must share than the
# best-ranked one to win (one shared word, like "american", is too easily a coincidence).
CONTEXT_MARGIN = 2


def fits_type(entity_type: str | None, description: str) -> bool:
    """False if the item is a creative work or a section but the entity is not "other"."""
    return entity_type in (None, "other") or not _WORK.search(description)


def pick_search_result(
    name: str,
    body: Mapping[str, Any],
    context: frozenset[str] = frozenset(),
    entity_type: str | None = None,
) -> Link:
    """A result whose label or alias is exactly `name`.

    Never a disambiguation page or a family name, nor a creative work for a person,
    organization, place or event (`fits_type`). Among the rest Wikidata's best-ranked one
    wins, unless a lower-ranked one shares clearly more words with the event's text
    (`context`, at least CONTEXT_MARGIN more): "Bend" in an Oregon story is the city in
    Oregon, whatever Wikidata ranks first.
    """
    wanted = normalize_name(name)
    candidates = []
    for result in body.get("search", []):
        description = str(result.get("description", "")).lower()
        if any(word in description for word in REJECTED_DESCRIPTIONS):
            continue
        if not fits_type(entity_type, description):
            continue
        texts = (result.get("label", ""), result.get("match", {}).get("text", ""))
        if any(normalize_name(text) == wanted for text in texts if text):
            overlap = len(set(_WORD.findall(description)) & context)
            candidates.append((overlap, result))
    if not candidates:
        return UNLINKED
    best_overlap, best = candidates[0]
    # max() keeps the first (best-ranked) of equal overlaps.
    overlap, closest = max(candidates, key=lambda candidate: candidate[0])
    if overlap >= best_overlap + CONTEXT_MARGIN:
        best = closest
    return Link(best["id"], "search", best.get("label") or name)


class Linker:
    """Links names to Wikidata IDs: event pages, then their links, then Wikidata search."""

    def __init__(
        self, client: WikimediaClient, api_url: str, wikidata_api_url: str, search: bool = True
    ) -> None:
        self.client = client
        self.api_url = api_url
        self.wikidata_api_url = wikidata_api_url
        self.search = search

    async def link(
        self,
        names: Sequence[str],
        pages: Sequence[EventPage],
        types: Mapping[str, str] | None = None,
    ) -> dict[str, Link]:
        """name -> Link for every name; `types` (name -> entity type) guides the search.

        Raises `WikiApiError` if an API keeps failing.
        """
        types = types or {}
        links: dict[str, Link] = {}
        own_pages = page_index(pages)
        for name in names:
            found = own_pages.get(normalize_name(name))
            if found:
                links[name] = Link(found[0], "page", found[1])
        linked_titles = link_index(pages)
        candidates = {
            name: linked_titles[normalize_name(name)]
            for name in names
            if name not in links and normalize_name(name) in linked_titles
        }
        resolved = await self.resolve_titles(sorted(set(candidates.values())))
        for name, title in candidates.items():
            if title in resolved:
                links[name] = Link(resolved[title][0], "link", resolved[title][1])
        rest = [name for name in names if name not in links]
        if self.search and rest:
            context = context_words(pages)
            found = await asyncio.gather(
                *(self.search_wikidata(name, context, types.get(name)) for name in rest)
            )
            links.update(zip(rest, found, strict=True))
        return {name: links.get(name, UNLINKED) for name in names}

    async def resolve_titles(self, titles: Sequence[str]) -> dict[str, tuple[str, str]]:
        """title -> (Wikidata ID, final title) for Wikipedia titles, 50 per request."""
        found: dict[str, tuple[str, str]] = {}
        for start in range(0, len(titles), TITLES_PER_REQUEST):
            batch = titles[start : start + TITLES_PER_REQUEST]
            response = await self.client.get_json(self.api_url, pageprops_params(batch))
            found |= parse_pageprops(batch, response.body or {})
        return found

    async def search_wikidata(
        self, name: str, context: frozenset[str] = frozenset(), entity_type: str | None = None
    ) -> Link:
        response = await self.client.get_json(self.wikidata_api_url, search_params(name))
        link = pick_search_result(name, response.body or {}, context, entity_type)
        logger.debug("Wikidata search %r -> %s", name, link.wikidata_id)
        return link
