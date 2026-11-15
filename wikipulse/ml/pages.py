"""The page as it looked at prediction time: one old revision's wikitext, plus its age.

Enrichment (stage 3) stores a page's categories as they are *today*. For training data
that leaks the future: "2026 deaths" or "Living people" may have changed after the spike.
So the dataset fetches the exact revision that was current at the cutoff (the newest
`rev_new` among the page's edits up to the cutoff, from the lake) and reads its wikitext:

- `action=query&prop=revisions&revids=...&rvprop=ids|timestamp|content` takes up to 50
  revision ids per request (we ask for `REVISIONS_PER_REQUEST`, to keep answers small);
- the page's first revision (`pageids=<id>&rvdir=newer&rvlimit=1`) gives its creation
  time, one request per page.

Every request goes through `WikiClient` (User-Agent, one at a time, retries). An old
revision never changes, so each one is cached forever, but compactly: `RevisionCache`
keeps one small JSON file per revision with what the features need (categories,
templates, number of refs), not the whole wikitext. Caching per revision rather than
per request also means a rebuild with a few new spikes asks only for the new revisions.
"""

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from wikipulse.config import Settings
from wikipulse.eval.wiki_client import WikiClient
from wikipulse.ml.features import PageSnapshot

if TYPE_CHECKING:
    from wikipulse.ml.summaries import Lead

logger = logging.getLogger(__name__)

REVISIONS_PER_REQUEST = 20

CATEGORY = re.compile(r"\[\[\s*category\s*:\s*([^\]|\n]+)", re.IGNORECASE)
TEMPLATE = re.compile(r"\{\{\s*([^{}|\n<]+?)\s*(?:\||\}\}|\n)")
REF = re.compile(r"<ref[\s>/]", re.IGNORECASE)


@dataclass(frozen=True)
class Revision:
    """One old revision: which page, when, and what its wikitext contained."""

    revid: int
    pageid: int | None
    timestamp: datetime
    hidden: bool = False  # the text was deleted or suppressed: nothing is known
    categories: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()
    refs: int = 0

    @classmethod
    def from_wikitext(
        cls, revid: int, pageid: int | None, timestamp: datetime, text: str | None
    ) -> "Revision":
        if text is None:
            return cls(revid, pageid, timestamp, hidden=True)
        categories, templates, refs = parse_wikitext(text)
        return cls(revid, pageid, timestamp, False, categories, templates, refs)

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "timestamp": self.timestamp.isoformat()})

    @classmethod
    def from_json(cls, text: str) -> "Revision":
        data = json.loads(text)
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        data["categories"] = tuple(data["categories"])
        data["templates"] = tuple(data["templates"])
        return cls(**data)


class RevisionCache:
    """One small JSON file per revision id (`<folder>/<revid>.json`)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def get(self, revid: int) -> Revision | None:
        path = self.folder / f"{revid}.json"
        if not path.exists():
            return None
        try:
            return Revision.from_json(path.read_text(encoding="utf-8"))
        except (ValueError, KeyError, TypeError):
            logger.warning("Ignoring unreadable cache file %s", path)
            return None

    def put(self, revision: Revision) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{revision.revid}.json"
        path.write_text(revision.to_json(), encoding="utf-8", newline="\n")


def clean_name(name: str) -> str:
    """'living_people ' -> 'Living people' (MediaWiki capitalizes the first letter)."""
    name = " ".join(name.replace("_", " ").split())
    return name[:1].upper() + name[1:]


def parse_wikitext(text: str) -> tuple[tuple[str, ...], tuple[str, ...], int]:
    """(explicit categories, lower-case template names, number of <ref> tags)."""
    categories = tuple(dict.fromkeys(clean_name(c) for c in CATEGORY.findall(text)))
    templates = []
    for raw in TEMPLATE.findall(text):
        name = clean_name(raw).lower()
        name = name.removeprefix("template:").strip()
        if name and not name.startswith(("#", "subst:", "safesubst:")):
            templates.append(name)
    return categories, tuple(dict.fromkeys(templates)), len(REF.findall(text))


def parse_time(text: str) -> datetime:
    """A MediaWiki timestamp as a datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def revisions_params(revids: Iterable[int]) -> dict[str, Any]:
    """Action API parameters for the wikitext of revisions."""
    return {
        "action": "query",
        "prop": "revisions",
        "revids": "|".join(str(r) for r in sorted(revids)),
        "rvprop": "ids|timestamp|content",
        "rvslots": "main",
        "format": "json",
        "formatversion": "2",
    }


def parse_revisions(body: dict[str, Any]) -> list[Revision]:
    """The revisions in a `prop=revisions&revids=...` answer (deleted ones are absent)."""
    found = []
    for page in body.get("query", {}).get("pages", []):
        for rev in page.get("revisions", []):
            main = rev.get("slots", {}).get("main", {})
            text = None if main.get("texthidden") or "content" not in main else main["content"]
            timestamp = parse_time(rev["timestamp"])
            found.append(Revision.from_wikitext(rev["revid"], page.get("pageid"), timestamp, text))
    return found


def creation_params(pageid: int) -> dict[str, Any]:
    """Action API parameters for a page's first revision."""
    return {
        "action": "query",
        "prop": "revisions",
        "pageids": str(pageid),
        "rvprop": "ids|timestamp",
        "rvdir": "newer",
        "rvlimit": "1",
        "format": "json",
        "formatversion": "2",
    }


def parse_creation(body: dict[str, Any]) -> datetime | None:
    """The timestamp of the page's first revision, or None if the page is gone."""
    for page in body.get("query", {}).get("pages", []):
        revisions = page.get("revisions") or []
        if revisions:
            return parse_time(revisions[0]["timestamp"])
    return None


def fetch_revisions(
    client: WikiClient, api_url: str, revids: Iterable[int], cache: RevisionCache
) -> dict[int, Revision]:
    """Revisions by id: from the cache, the rest `REVISIONS_PER_REQUEST` per request.

    Revisions the API no longer returns (deleted since) are missing from the result.
    """
    revids = set(revids)
    found: dict[int, Revision] = {}
    for revid in revids:
        if (cached := cache.get(revid)) is not None:
            found[revid] = cached
    wanted = sorted(revids - set(found))
    for start in range(0, len(wanted), REVISIONS_PER_REQUEST):
        batch = wanted[start : start + REVISIONS_PER_REQUEST]
        params = revisions_params(batch)
        while True:
            body = client.get_json(api_url, params, cache=False)
            for revision in parse_revisions(body):
                cache.put(revision)
                found[revision.revid] = revision
            if "continue" not in body:
                break
            params = {**revisions_params(batch), **body["continue"]}
        if (start // REVISIONS_PER_REQUEST) % 10 == 9:
            logger.info(
                "Revisions: %d of %d fetched", min(start + len(batch), len(wanted)), len(wanted)
            )
    return found


def fetch_creation_times(
    client: WikiClient, api_url: str, pageids: Iterable[int]
) -> dict[int, datetime | None]:
    """Creation time of each page (its first revision), one request per page."""
    created = {}
    ids = sorted(set(pageids))
    for number, pageid in enumerate(ids, start=1):
        body = client.get_json(api_url, creation_params(pageid), label=f"created-{pageid}")
        created[pageid] = parse_creation(body)
        if number % 100 == 0:
            logger.info("Creation times: %d of %d pages", number, len(ids))
    return created


def snapshot_of(revision: Revision, created_at: datetime | None) -> PageSnapshot | None:
    """The features' view of a revision; None if its text is hidden (unknown, not empty)."""
    if revision.hidden:
        return None
    return PageSnapshot(
        revision_id=revision.revid,
        revision_time=revision.timestamp,
        categories=revision.categories,
        templates=revision.templates,
        refs=revision.refs,
        created_at=created_at,
    )


class WikiPages:
    """Pages at their cutoff revision from the MediaWiki API, through the caches.

    The one way to get page snapshots, for the dataset (`dataset.fetch_snapshots`) and
    for the live scorer (`serve.score_spikes`) alike (spec 11a). `transport` replaces
    the network in tests (an `httpx.MockTransport`); `stats` counts the requests of the
    last `snapshots` call.
    """

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.cache = settings.data_dir / "cache" / "ml"
        self.transport = transport
        self.stats: dict[str, int] = {}

    def _client(self) -> WikiClient:
        s = self.settings
        return WikiClient(
            s.user_agent, self.cache, s.wiki_api_min_interval_s, transport=self.transport
        )

    def snapshots(self, revids: set[int]) -> dict[int, PageSnapshot | None]:
        """Each revision's snapshot (None: its text is hidden); deleted ones are absent."""
        with self._client() as client:
            url = self.settings.wiki_api_url
            revisions = fetch_revisions(
                client, url, revids, RevisionCache(self.cache / "revisions")
            )
            pageids = {r.pageid for r in revisions.values() if r.pageid is not None}
            created = fetch_creation_times(client, url, pageids)
            self.stats = {
                "requests": client.requests,
                "cache_hits": client.cache_hits,
                "revisions_wanted": len(revids),
                "revisions_found": len(revisions),
            }
        return {
            revid: snapshot_of(rev, created.get(rev.pageid) if rev.pageid else None)
            for revid, rev in revisions.items()
        }

    def leads(self, revids: set[int]) -> "dict[int, Lead]":
        """Each revision's lead section (for the models that read text)."""
        from wikipulse.ml.summaries import LeadCache, fetch_leads

        with self._client() as client:
            cache = LeadCache(self.cache / "leads")
            return fetch_leads(client, self.settings.wiki_api_url, revids, cache)
