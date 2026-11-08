"""What the extraction sees about an event: its pages and the text of their recent changes.

An event (stage 3) is a set of spikes, and a spike is a burst of edits to one page. For
each page of an event this module gathers

- what stage 3 stored in the `pages` table: summary, Wikidata ID, outgoing links, and
- the text added during the burst: the revisions between the spike's first and last
  edit (MediaWiki `prop=revisions`), then the diff from the revision before the burst to
  the burst's last revision (`action=compare`), reduced to the added lines and cleaned of
  wiki markup, then trimmed.

The newest revision of the burst is the `source_revision` of everything extracted from
the page. The times come from the spikes (event time), so a replay asks for exactly the
revisions the live run asked for, and gets the same (cached) answers.
"""

import asyncio
import html
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from wikipulse.enrich.client import WikiApiError, WikimediaClient

logger = logging.getLogger(__name__)

# Most revisions of one burst looked at (the diff spans all of them anyway).
MAX_REVISIONS = 50
# Revisions this close before the first / after the last edit still count (clock skew).
MARGIN = timedelta(minutes=1)


@dataclass(frozen=True)
class EventPage:
    """One page of an event, with what extraction needs to know about it."""

    title: str  # canonical title (after redirects)
    spike_titles: tuple[str, ...]  # the titles its spikes used
    wikidata_id: str | None
    description: str | None
    summary: str | None
    links: tuple[str, ...]
    first_edit_at: datetime  # earliest edit of its spikes in the event
    last_edit_at: datetime  # latest edit of its spikes in the event
    score: float  # highest spike score
    revision: int | None = None  # newest revision of the burst (filled by `with_changes`)
    changes: str = ""  # text added during the burst, cleaned and trimmed


@dataclass(frozen=True)
class EventInput:
    """An event of a grouping run with its pages, as read from Postgres."""

    run_id: str
    number: int
    status: str
    title: str  # stage 3's provisional title (the highest-scoring page)
    started_at: datetime
    ended_at: datetime | None
    fingerprint: str  # its spike ids: when they change, the event is extracted again
    pages: tuple[EventPage, ...]

    @property
    def key(self) -> str:
        """The event's ID in the graph: grouping run and event number."""
        return event_key(self.run_id, self.number)


def event_key(run_id: str, number: int) -> str:
    return f"{run_id}:{number}"


def pages_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[EventPage]:
    """One EventPage per canonical title, highest spike score first.

    `rows` are the event's spikes joined with their `pages` rows (see
    `wikipulse.db.graph.event_spike_rows`); several spikes of one page are merged.
    """
    merged: dict[str, EventPage] = {}
    for row in rows:
        usable = row.get("page_status") in ("ok", "disambiguation")
        title = (row.get("canonical_title") if usable else None) or row["title"]
        page = EventPage(
            title=title,
            spike_titles=(row["title"],),
            # A redirect to a section has no ID of its own (see stage 3).
            wikidata_id=row.get("wikidata_id") if row.get("page_status") == "ok" else None,
            description=row.get("description") if usable else None,
            summary=row.get("summary") if usable else None,
            links=tuple(row.get("links") or ()) if usable else (),
            first_edit_at=row["first_edit_at"],
            last_edit_at=row["last_edit_at"],
            score=row["score"],
        )
        if title in merged:
            page = merge_pages(merged[title], page)
        merged[title] = page
    return sorted(merged.values(), key=lambda p: (-p.score, p.title))


def merge_pages(a: EventPage, b: EventPage) -> EventPage:
    """Two spikes of the same page: one page spanning both bursts."""
    titles = a.spike_titles + tuple(t for t in b.spike_titles if t not in a.spike_titles)
    return replace(
        a,
        spike_titles=titles,
        wikidata_id=a.wikidata_id or b.wikidata_id,
        description=a.description or b.description,
        summary=a.summary or b.summary,
        links=a.links or b.links,
        first_edit_at=min(a.first_edit_at, b.first_edit_at),
        last_edit_at=max(a.last_edit_at, b.last_edit_at),
        score=max(a.score, b.score),
    )


# --- Recent changes ------------------------------------------------------------------


def mw_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def revisions_params(page: EventPage) -> dict[str, Any]:
    """`prop=revisions`: the burst's revisions of the page, newest first."""
    return {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "prop": "revisions",
        "titles": page.title,
        "redirects": "1",
        "rvprop": "ids|timestamp",
        "rvstart": mw_time(page.last_edit_at + MARGIN),
        "rvend": mw_time(page.first_edit_at - MARGIN),
        "rvdir": "older",
        "rvlimit": str(MAX_REVISIONS),
    }


def compare_params(from_revision: int, to_revision: int) -> dict[str, Any]:
    """`action=compare`: the diff between two revisions, as an HTML table."""
    return {
        "action": "compare",
        "format": "json",
        "formatversion": "2",
        "fromrev": str(from_revision),
        "torev": str(to_revision),
        "prop": "diff",
        "difftype": "table",
    }


def parse_revisions(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The revisions (`revid`, `parentid`, `timestamp`) in a response, newest first."""
    revisions: list[dict[str, Any]] = []
    for page in body.get("query", {}).get("pages", []):
        revisions.extend(page.get("revisions", []))
    return revisions


_ADDED_CELL = re.compile(r'<td class="diff-addedline[^"]*"[^>]*>(.*?)</td>', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)


def added_lines(diff_html: str) -> list[str]:
    """The lines on the "added" side of a MediaWiki table diff, as wikitext."""
    return [html.unescape(_TAG.sub("", cell)) for cell in _ADDED_CELL.findall(diff_html)]


_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.DOTALL | re.IGNORECASE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_FILE_LINK = re.compile(r"\[\[(?:File|Image|Category):[^\]]*\]\]", re.IGNORECASE)
_PIPED_LINK = re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]")
_LINK = re.compile(r"\[\[([^\]]*)\]\]")
_EXTERNAL_LINK = re.compile(r"\[https?://\S+\s*([^\]]*)\]")
_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^=+\s*(.*?)\s*=+$")


def clean_wikitext(line: str) -> str:
    """Readable text from one line of wikitext: no references, templates or markup."""
    text = _COMMENT.sub("", _REF.sub("", line))
    for _ in range(5):  # nested templates, innermost first
        text, count = _TEMPLATE.subn("", text)
        if not count:
            break
    text = _FILE_LINK.sub("", text)
    text = _PIPED_LINK.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _EXTERNAL_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _TAG.sub("", _BREAK.sub(", ", text)).replace("'''", "").replace("''", "")
    text = " ".join(text.split())
    heading = _HEADING.match(text)
    if heading:
        text = heading.group(1) + ":"
    if text.startswith(("{{", "}}", "{|", "|}", "|-")):
        return ""  # the edge of a template or table that spans several lines
    return text.lstrip("|*#: ").strip()


def trim(text: str, max_chars: int) -> str:
    """At most `max_chars` characters, cut at a word boundary."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut.rstrip() + " …"


_FIELD = re.compile(r"^([\w ]{1,40}?)\s*=\s*(.+)$")


def readable(line: str) -> str | None:
    """A cleaned line worth showing the model, or None.

    Infobox fields (`birth_place = Cox's Bazar`) become `birth place: Cox's Bazar`.
    Table rows, lists of template parameters and lines that are mostly numbers or
    symbols (`res9 = L/W/W/W`, `! align=left| Total !! 50`) are dropped.
    """
    if len(line.split()) < 3 or "|" in line or line.count("=") > 1:
        return None
    if sum(char.isalpha() for char in line) < 0.5 * len(line):
        return None
    field = _FIELD.match(line)
    if field:
        return f"{field.group(1).replace('_', ' ').strip()}: {field.group(2)}"
    return line


def changes_text(diff_html: str, max_chars: int) -> str:
    """The cleaned text added in a diff: distinct readable lines, sentences first, then
    infobox fields (so trimming cuts the fields, not the prose)."""
    seen: set[str] = set()
    prose, fields = [], []
    for raw in added_lines(diff_html):
        line = readable(clean_wikitext(raw))
        if line is None or line in seen:
            continue
        seen.add(line)
        (fields if _FIELD.match(clean_wikitext(raw)) else prose).append(line)
    return trim("\n".join(prose + fields), max_chars)


async def fetch_changes(
    client: WikimediaClient, api_url: str, page: EventPage, max_chars: int
) -> EventPage:
    """The page with its burst's newest revision and the text added during the burst.

    Raises `WikiApiError` if the revisions can't be fetched (the event is retried later).
    A failed diff only leaves `changes` empty: the summary is still there.
    """
    body = (await client.get_json(api_url, revisions_params(page))).body
    revisions = parse_revisions(body)
    if not revisions:
        logger.info("No revisions of %r during its burst", page.title)
        return page
    newest, oldest = revisions[0], revisions[-1]
    page = replace(page, revision=newest["revid"])
    parent = oldest.get("parentid") or 0
    if parent == 0:  # the page was created during the burst: its summary is its text
        return page
    try:
        diff = (await client.get_json(api_url, compare_params(parent, newest["revid"]))).body
    except WikiApiError as error:  # e.g. a revision hidden since (revision deletion)
        logger.warning("No diff for %r: %s", page.title, error)
        return page
    diff_html = diff.get("compare", {}).get("body", "") if isinstance(diff, dict) else ""
    return replace(page, changes=changes_text(diff_html, max_chars))


async def with_changes(
    client: WikimediaClient, api_url: str, pages: Iterable[EventPage], max_chars: int
) -> list[EventPage]:
    """`fetch_changes` for every page (the client limits requests in flight)."""
    return list(
        await asyncio.gather(*(fetch_changes(client, api_url, p, max_chars) for p in pages))
    )
