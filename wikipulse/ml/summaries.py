"""A spike's page summary as it was at the cutoff, and its embedding.

The neural model (spec 07b) reads a *summary embedding* next to the 43 features: the
meaning of what the page is about, as 384 numbers from the same local model that groups
events (`EVENTS_EMBED_MODEL`, `BAAI/bge-small-en-v1.5`, see `wikipulse.events.embed`).

Which summary? Stage 3 stores each page's summary as it is *today*, and today's summary
of a person who died during the spike says so: that would leak the label. So, like the
page features of spec 07a, the summary comes from the revision that was current at the
cutoff (`page_revision_id` in the dataset): its wikitext is fetched once (20 revisions per
request, through `WikiClient`), and the short description and the lead section (the text
before the first heading) are pulled out of it and cached per revision in
`data/cache/ml/leads/` (an old revision never changes). The text that is embedded is
`page_text(title, short description, lead)`, the same recipe as event grouping.

Turning wikitext into plain text properly needs a full parser (templates expand into
text on Wikipedia's servers). `lead_of()` is a deliberately simple approximation: it drops
templates, tables, references, files and comments, keeps the visible text of links, and
takes the first paragraphs. That is plenty for an embedding of "what is this page about".
"""

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from wikipulse.eval.wiki_client import WikiClient
from wikipulse.events.embed import Embedder, page_text
from wikipulse.ml.pages import REVISIONS_PER_REQUEST, revisions_params

logger = logging.getLogger(__name__)

LEAD_MAX_CHARS = 1200
# Raise when `lead_of()` changes: cached leads of another version are parsed again.
LEAD_PARSER = 2

SHORT_DESCRIPTION = re.compile(r"\{\{\s*short description\s*\|\s*([^}|]*)", re.IGNORECASE)
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
REF_PAIR = re.compile(r"<ref[^>/]*>.*?</ref\s*>", re.IGNORECASE | re.DOTALL)
REF_SINGLE = re.compile(r"<ref[^>]*/\s*>", re.IGNORECASE)
INNER_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
INNER_TABLE = re.compile(r"\{\|(?:(?!\{\|).)*?\|\}", re.DOTALL)
INNER_FILE = re.compile(r"\[\[\s*(?:file|image|category)\s*:[^\[\]]*\]\]", re.IGNORECASE)
INNER_LINK = re.compile(r"\[\[(?:[^\[\]|]*\|)?([^\[\]]*)\]\]")
EXTERNAL_LINK = re.compile(r"\[(?:https?:)?//[^\s\]]+\s*([^\]]*)\]")
HTML_TAG = re.compile(r"<[^>]+>")
MAGIC_WORD = re.compile(r"__[A-Z]+__")
HEADING = re.compile(r"^=+.*=+\s*$", re.MULTILINE)


def short_description(wikitext: str) -> str | None:
    """The page's `{{Short description|...}}`, if it has one ("none" counts as none)."""
    match = SHORT_DESCRIPTION.search(wikitext)
    if not match:
        return None
    text = match.group(1).strip()
    return text if text and text.lower() != "none" else None


def _remove_nested(pattern: re.Pattern[str], text: str, replacement: str = "") -> str:
    """Apply `pattern` (which matches only innermost pieces) until nothing changes."""
    while True:
        new = pattern.sub(replacement, text)
        if new == text:
            return text
        text = new


def lead_of(wikitext: str, max_chars: int = LEAD_MAX_CHARS) -> str:
    """The lead section as plain text (an approximation; see the module docstring)."""
    text = COMMENT.sub("", wikitext)
    text = REF_PAIR.sub("", text)
    text = REF_SINGLE.sub("", text)
    text = _remove_nested(INNER_TEMPLATE, text)
    text = _remove_nested(INNER_TABLE, text)
    while True:  # files without links inside, then the innermost links, and again
        new = INNER_LINK.sub(r"\1", INNER_FILE.sub("", text))
        if new == text:
            break
        text = new
    text = EXTERNAL_LINK.sub(r"\1", text)
    text = HTML_TAG.sub("", MAGIC_WORD.sub("", text))
    text = text.replace("'''", "").replace("''", "")
    lead = HEADING.split(text, maxsplit=1)[0]
    paragraphs = []
    for block in re.split(r"\n\s*\n", lead):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not ln.startswith(("|", "!", "{", "}"))]
        paragraph = " ".join(" ".join(lines).split())
        if len(paragraph) > 1:
            paragraphs.append(paragraph)
    return " ".join(paragraphs)[:max_chars].strip()


@dataclass(frozen=True)
class Lead:
    """What the embedding needs from one old revision."""

    revid: int
    hidden: bool = False  # text deleted or suppressed: nothing is known
    description: str | None = None
    lead: str = ""
    parser: int = LEAD_PARSER  # the `lead_of()` version that made it

    @classmethod
    def from_wikitext(cls, revid: int, text: str | None) -> "Lead":
        if text is None:
            return cls(revid, hidden=True)
        return cls(revid, False, short_description(text), lead_of(text))


class LeadCache:
    """One small JSON file per revision id (`<folder>/<revid>.json`)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def get(self, revid: int) -> Lead | None:
        path = self.folder / f"{revid}.json"
        if not path.exists():
            return None
        try:
            lead = Lead(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            logger.warning("Ignoring unreadable cache file %s", path)
            return None
        return lead if lead.parser == LEAD_PARSER else None  # made by an older lead_of()

    def put(self, lead: Lead) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{lead.revid}.json"
        path.write_text(json.dumps(asdict(lead)), encoding="utf-8", newline="\n")


def parse_leads(body: dict[str, Any]) -> list[Lead]:
    """The revisions in a `prop=revisions&revids=...` answer, as leads."""
    found = []
    for page in body.get("query", {}).get("pages", []):
        for rev in page.get("revisions", []):
            main = rev.get("slots", {}).get("main", {})
            text = None if main.get("texthidden") or "content" not in main else main["content"]
            found.append(Lead.from_wikitext(rev["revid"], text))
    return found


def fetch_leads(
    client: WikiClient, api_url: str, revids: Iterable[int], cache: LeadCache
) -> dict[int, Lead]:
    """Leads by revision id: from the cache, the rest `REVISIONS_PER_REQUEST` per request.

    Revisions the API no longer returns (deleted since) are missing from the result.
    """
    revids = set(revids)
    found = {r: lead for r in revids if (lead := cache.get(r)) is not None}
    wanted = sorted(revids - set(found))
    if wanted:
        logger.info("Fetching the wikitext of %d revisions", len(wanted))
    for start in range(0, len(wanted), REVISIONS_PER_REQUEST):
        batch = wanted[start : start + REVISIONS_PER_REQUEST]
        params = revisions_params(batch)
        while True:
            body = client.get_json(api_url, params, cache=False)
            for lead in parse_leads(body):
                cache.put(lead)
                found[lead.revid] = lead
            if "continue" not in body:
                break
            params = {**revisions_params(batch), **body["continue"]}
    return found


def summary_text(title: str, lead: Lead | None) -> str:
    """The text embedded for one spike: title, short description and lead at the cutoff."""
    if lead is None or lead.hidden:
        return page_text(title, None, None)
    return page_text(title, lead.description, lead.lead or None)


def summary_texts(
    titles: Sequence[str], revisions: Sequence[int | None], leads: dict[int, Lead]
) -> list[str]:
    """The summary text of each spike's page at its revision."""
    return [
        summary_text(title, leads.get(rev) if rev is not None else None)
        for title, rev in zip(titles, revisions, strict=True)
    ]


def embed_texts(texts: Sequence[str], embedder: Embedder) -> np.ndarray:
    """An (n, dimensions) matrix of unit vectors, one row per text."""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    return np.vstack(embedder.embed_many(texts)).astype(np.float32)
