"""The `pages` table (migration 0002): enriched pages, one row per spike title.

`enriched_at` is wall-clock time: it says how old our copy of the page is. A page is
enriched again only when a spike started after the copy was made *and* the copy is
older than the time-to-live, so each page is fetched at most once per TTL.
`attempted_at` is the time of the last attempt; a failed attempt (good row or not) is
not repeated before `error_retry` has passed.
"""

from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from wikipulse.enrich.pages import PageInfo, PageStatus

UPSERT = """
INSERT INTO pages (title, canonical_title, status, is_redirect, redirect_fragment, page_id,
                   wikidata_id, description, summary, categories, links, links_truncated,
                   error, enriched_at, attempted_at)
VALUES (%(title)s, %(canonical_title)s, %(status)s, %(is_redirect)s, %(redirect_fragment)s,
        %(page_id)s, %(wikidata_id)s, %(description)s, %(summary)s, %(categories)s,
        %(links)s, %(links_truncated)s, NULL, now(), now())
ON CONFLICT (title) DO UPDATE SET
    canonical_title = EXCLUDED.canonical_title, status = EXCLUDED.status,
    is_redirect = EXCLUDED.is_redirect, redirect_fragment = EXCLUDED.redirect_fragment,
    page_id = EXCLUDED.page_id, wikidata_id = EXCLUDED.wikidata_id,
    description = EXCLUDED.description, summary = EXCLUDED.summary,
    categories = EXCLUDED.categories, links = EXCLUDED.links,
    links_truncated = EXCLUDED.links_truncated, error = NULL, enriched_at = now(),
    attempted_at = now()
"""

# An error never overwrites good (if old) data: on a good row it only notes the error
# and the attempt time (`attempted_at`), so the next try waits for `error_retry`.
# `enriched_at` stays the age of the data; on an error row it is the time of the error.
RECORD_ERROR = """
INSERT INTO pages (title, canonical_title, status, error, enriched_at, attempted_at)
VALUES (%s, %s, 'error', %s, now(), now())
ON CONFLICT (title) DO UPDATE SET
    error = EXCLUDED.error,
    attempted_at = now(),
    enriched_at = CASE WHEN pages.status = 'error' THEN now() ELSE pages.enriched_at END
"""

PENDING = """
SELECT s.title
FROM spikes s LEFT JOIN pages p ON p.title = s.title
WHERE s.run_id = %(run_id)s
  AND (%(since)s::timestamptz IS NULL OR s.started_at >= %(since)s::timestamptz)
  AND (%(until)s::timestamptz IS NULL OR s.started_at < %(until)s::timestamptz)
  AND (
    %(force)s
    OR p.title IS NULL
    OR (p.status = 'error' AND p.attempted_at < now() - %(error_retry)s)
    OR (p.status <> 'error' AND p.enriched_at < s.started_at
        AND p.enriched_at < now() - %(ttl)s
        AND p.attempted_at < now() - %(error_retry)s)
  )
GROUP BY s.title
ORDER BY min(s.started_at), s.title
"""


def upsert_page(conn: psycopg.Connection, info: PageInfo) -> None:
    """Insert or replace the row for `info.requested_title`."""
    conn.execute(
        UPSERT,
        {
            "title": info.requested_title,
            "canonical_title": info.title,
            "status": info.status.value,
            "is_redirect": info.is_redirect,
            "redirect_fragment": info.redirect_fragment,
            "page_id": info.page_id,
            "wikidata_id": info.wikidata_id,
            "description": info.description,
            "summary": info.summary,
            "categories": list(info.categories),
            "links": list(info.links),
            "links_truncated": info.links_truncated,
        },
    )


def record_error(conn: psycopg.Connection, title: str, error: str) -> None:
    """Remember that enriching `title` failed; an older good row keeps its data."""
    conn.execute(RECORD_ERROR, (title, title, error[:1000]))


def get_page(conn: psycopg.Connection, title: str) -> PageInfo | None:
    """The stored page for a spike title, or None."""
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT * FROM pages WHERE title = %s", (title,)).fetchone()
    if row is None:
        return None
    return PageInfo(
        requested_title=row["title"],
        title=row["canonical_title"],
        status=PageStatus(row["status"]),
        is_redirect=row["is_redirect"],
        redirect_fragment=row["redirect_fragment"],
        page_id=row["page_id"],
        wikidata_id=row["wikidata_id"],
        description=row["description"],
        summary=row["summary"],
        categories=tuple(row["categories"]),
        links=tuple(row["links"]),
        links_truncated=row["links_truncated"],
    )


def pending_titles(
    conn: psycopg.Connection,
    run_id: str,
    ttl: timedelta,
    error_retry: timedelta = timedelta(minutes=10),
    since: datetime | None = None,
    until: datetime | None = None,
    force: bool = False,
) -> list[str]:
    """Spike titles of a run that need (re-)enrichment, oldest spike first.

    A title is pending if it has no row; or its row is an error older than
    `error_retry`; or a spike started after the row was written, the row is older than
    `ttl` and its last (failed) refresh attempt is older than `error_retry`.
    `force` returns every title of the run (in the time range).
    """
    params = {
        "run_id": run_id,
        "since": since,
        "until": until,
        "force": force,
        "ttl": ttl,
        "error_retry": error_retry,
    }
    return [row[0] for row in conn.execute(PENDING, params).fetchall()]
