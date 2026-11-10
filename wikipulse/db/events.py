"""The `events` and `event_pages` tables (migration 0004), and the spikes to group.

`save_events()` writes the events the grouper changed, in one transaction: first the
event rows (upsert on (run_id, number)), then one `event_pages` row per member spike
(upsert on (run_id, spike_id), so a spike whose event was merged moves to the survivor).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from wikipulse.events.grouper import Event

UPSERT_EVENT = """
INSERT INTO events (run_id, spike_run_id, number, status, title, started_at, last_spike_at,
                    ended_at, page_count, score, merged_into, correlation_id)
VALUES (%(run_id)s, %(spike_run_id)s, %(number)s, %(status)s, %(title)s, %(started_at)s,
        %(last_spike_at)s, %(ended_at)s, %(page_count)s, %(score)s, %(merged_into)s,
        %(correlation_id)s)
ON CONFLICT (run_id, number) DO UPDATE SET
    status = EXCLUDED.status, title = EXCLUDED.title, started_at = EXCLUDED.started_at,
    last_spike_at = EXCLUDED.last_spike_at, ended_at = EXCLUDED.ended_at,
    page_count = EXCLUDED.page_count, score = EXCLUDED.score,
    merged_into = EXCLUDED.merged_into, correlation_id = EXCLUDED.correlation_id,
    updated_at = now()
"""

UPSERT_MEMBER = """
INSERT INTO event_pages (run_id, spike_id, event_number, title, joined_score)
VALUES (%(run_id)s, %(spike_id)s, %(event_number)s, %(title)s, %(joined_score)s)
ON CONFLICT (run_id, spike_id) DO UPDATE SET
    event_number = EXCLUDED.event_number, joined_score = EXCLUDED.joined_score
"""

# A spike with what grouping needs from its page (NULLs if not enriched yet).
SPIKE_ROWS = """
SELECT s.id AS spike_id, s.title, s.started_at, s.ended_at, s.score, s.total_edits,
       p.title IS NOT NULL AS enriched, p.status AS page_status, p.canonical_title,
       p.description, p.summary, p.links, s.correlation_id
FROM spikes s LEFT JOIN pages p ON p.title = s.title
"""

UNASSIGNED = f"""
{SPIKE_ROWS}
WHERE s.run_id = %(spike_run_id)s
  AND (%(since)s::timestamptz IS NULL OR s.started_at >= %(since)s::timestamptz)
  AND (%(until)s::timestamptz IS NULL OR s.started_at < %(until)s::timestamptz)
  AND NOT EXISTS (
    SELECT 1 FROM event_pages e WHERE e.run_id = %(run_id)s AND e.spike_id = s.id
  )
ORDER BY s.started_at, s.title, s.id
"""

# The same columns plus the event and joined score, for the run's active events.
ACTIVE_MEMBERS = f"""
SELECT ep.event_number, ep.joined_score, {SPIKE_ROWS.strip().removeprefix("SELECT ")}
JOIN event_pages ep ON ep.spike_id = s.id
JOIN events ev ON ev.run_id = ep.run_id AND ev.number = ep.event_number
WHERE ep.run_id = %s AND ev.status = 'active'
ORDER BY ep.event_number, s.started_at, s.title, s.id
"""


@dataclass(frozen=True)
class SpikeRow:
    """A spike and its enriched page, as read for grouping."""

    spike_id: int
    title: str
    started_at: datetime
    ended_at: datetime | None
    score: float
    total_edits: int
    enriched: bool
    page_status: str | None
    canonical_title: str | None
    description: str | None
    summary: str | None
    links: list[str] | None
    correlation_id: str | None = None  # spec 09b: the edit that started the spike


def _spike_rows(conn: psycopg.Connection, query: str, params) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


def unassigned_spikes(
    conn: psycopg.Connection,
    run_id: str,
    spike_run_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[SpikeRow]:
    """Spikes of the detector run not yet in an event of this grouping run, in order."""
    params = {"run_id": run_id, "spike_run_id": spike_run_id, "since": since, "until": until}
    return [SpikeRow(**row) for row in _spike_rows(conn, UNASSIGNED, params)]


def spikes_with_pages(
    conn: psycopg.Connection,
    spike_run_id: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[SpikeRow]:
    """Every spike of a detector run (started in [since, until)), in grouping order."""
    query = f"""
    {SPIKE_ROWS}
    WHERE s.run_id = %(run)s
      AND (%(since)s::timestamptz IS NULL OR s.started_at >= %(since)s::timestamptz)
      AND (%(until)s::timestamptz IS NULL OR s.started_at < %(until)s::timestamptz)
    ORDER BY s.started_at, s.title, s.id
    """
    params = {"run": spike_run_id, "since": since, "until": until}
    return [SpikeRow(**row) for row in _spike_rows(conn, query, params)]


def event_membership(conn: psycopg.Connection, run_id: str) -> dict[int, int]:
    """spike id -> event number, for every spike grouped by the run."""
    rows = conn.execute(
        "SELECT spike_id, event_number FROM event_pages WHERE run_id = %s", (run_id,)
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def active_members(conn: psycopg.Connection, run_id: str) -> list[tuple[int, float, SpikeRow]]:
    """(event number, joined score, spike) for every spike of the run's active events."""
    members = []
    for row in _spike_rows(conn, ACTIVE_MEMBERS, (run_id,)):
        number, joined = row.pop("event_number"), row.pop("joined_score")
        members.append((number, joined, SpikeRow(**row)))
    return members


def spike_ends(conn: psycopg.Connection, spike_ids: Sequence[int]) -> dict[int, datetime | None]:
    """The current `ended_at` of each spike (None while it is active)."""
    if not spike_ids:
        return {}
    rows = conn.execute(
        "SELECT id, ended_at FROM spikes WHERE id = ANY(%s)", (list(spike_ids),)
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def save_events(
    conn: psycopg.Connection, run_id: str, spike_run_id: str, events: Iterable[Event]
) -> int:
    """Insert or update events and their members, in one transaction. Returns the count."""
    events = list(events)
    if not events:
        return 0
    event_rows = [
        {
            "run_id": run_id,
            "spike_run_id": spike_run_id,
            "number": e.number,
            "status": e.status,
            "title": e.title,
            "started_at": e.started_at,
            "last_spike_at": e.last_spike_at,
            "ended_at": e.ended_at,
            "page_count": e.page_count,
            "score": e.score,
            "merged_into": e.merged_into,
            "correlation_id": e.correlation_id,
        }
        for e in events
    ]
    member_rows = [
        {
            "run_id": run_id,
            "spike_id": m.page.spike_id,
            "event_number": e.number,
            "title": m.page.title,
            "joined_score": m.joined_score,
        }
        for e in events
        for m in e.members
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(UPSERT_EVENT, event_rows)
        if member_rows:
            cur.executemany(UPSERT_MEMBER, member_rows)
    return len(events)


def run_info(conn: psycopg.Connection, run_id: str) -> tuple[str | None, int, datetime | None]:
    """(spike run id, next event number, latest spike start) of a grouping run."""
    row = conn.execute(
        "SELECT min(spike_run_id), coalesce(max(number), 0) + 1, max(last_spike_at) "
        "FROM events WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    return row[0], row[1], row[2]


def delete_grouping_run(conn: psycopg.Connection, run_id: str) -> int:
    """Delete every event (and membership) of a grouping run. Returns the events deleted."""
    with conn.transaction():
        conn.execute("DELETE FROM event_pages WHERE run_id = %s", (run_id,))
        return conn.execute("DELETE FROM events WHERE run_id = %s", (run_id,)).rowcount


@dataclass(frozen=True)
class FeedPage:
    title: str
    canonical_title: str
    started_at: datetime
    ended_at: datetime | None
    score: float
    joined_score: float
    news_score: float | None = None  # the news classifier's score (spec 07c), once scored
    is_news: bool | None = None


@dataclass(frozen=True)
class FeedEvent:
    number: int
    status: str
    title: str
    started_at: datetime
    last_spike_at: datetime
    ended_at: datetime | None
    page_count: int
    score: float
    pages: tuple[FeedPage, ...]
    merged_into: int | None = None


def feed_events(
    conn: psycopg.Connection,
    run_id: str,
    include_ended: bool = False,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[FeedEvent]:
    """Events of a grouping run with their pages (merged events left out).

    Ordered by size (pages), then score, then number. `since`/`until` filter on the
    event's start.
    """
    statuses = ["active", "ended"] if include_ended else ["active"]
    with conn.cursor(row_factory=dict_row) as cur:
        events = cur.execute(
            """
            SELECT number, status, title, started_at, last_spike_at, ended_at, page_count, score
            FROM events
            WHERE run_id = %(run_id)s AND status = ANY(%(statuses)s)
              AND (%(since)s::timestamptz IS NULL OR started_at >= %(since)s::timestamptz)
              AND (%(until)s::timestamptz IS NULL OR started_at < %(until)s::timestamptz)
            ORDER BY page_count DESC, score DESC, number
            """,
            {"run_id": run_id, "statuses": statuses, "since": since, "until": until},
        ).fetchall()
        pages = cur.execute(
            """
            SELECT ep.event_number, s.title,
                   coalesce(p.canonical_title, s.title) AS canonical_title,
                   s.started_at, s.ended_at, s.score, ep.joined_score,
                   sc.score AS news_score, sc.is_news
            FROM event_pages ep
            JOIN spikes s ON s.id = ep.spike_id
            LEFT JOIN pages p ON p.title = s.title
            LEFT JOIN spike_scores sc ON sc.spike_id = s.id
            WHERE ep.run_id = %s
            ORDER BY s.score DESC, s.started_at, s.title
            """,
            (run_id,),
        ).fetchall()
    by_event: dict[int, list[FeedPage]] = {}
    for row in pages:
        by_event.setdefault(row.pop("event_number"), []).append(FeedPage(**row))
    return [FeedEvent(**e, pages=tuple(by_event.get(e["number"], []))) for e in events]


# Most recent activity first: active events, then by their latest spike start.
RECENT_EVENTS = """
SELECT number, status, title, started_at, last_spike_at, ended_at, page_count, score,
       merged_into
FROM events
WHERE run_id = %(run_id)s AND status = ANY(%(statuses)s)
  AND (%(number)s::integer IS NULL OR number = %(number)s::integer)
  AND (SELECT count(DISTINCT coalesce(p.canonical_title, s.title))
       FROM event_pages ep JOIN spikes s ON s.id = ep.spike_id
       LEFT JOIN pages p ON p.title = s.title
       WHERE ep.run_id = events.run_id AND ep.event_number = events.number) >= %(min_pages)s
ORDER BY (status = 'active') DESC, last_spike_at DESC, number DESC
LIMIT %(limit)s
"""
EVENT_PAGES = """
SELECT ep.event_number, s.title, coalesce(p.canonical_title, s.title) AS canonical_title,
       s.started_at, s.ended_at, s.score, ep.joined_score,
       sc.score AS news_score, sc.is_news
FROM event_pages ep
JOIN spikes s ON s.id = ep.spike_id
LEFT JOIN pages p ON p.title = s.title
LEFT JOIN spike_scores sc ON sc.spike_id = s.id
WHERE ep.run_id = %s AND ep.event_number = ANY(%s)
ORDER BY s.score DESC, s.started_at, s.title
"""


def recent_events(
    conn: psycopg.Connection,
    run_id: str,
    limit: int = 50,
    statuses: Sequence[str] = ("active", "ended"),
    min_pages: int = 1,
    number: int | None = None,
) -> list[FeedEvent]:
    """The run's events with their pages, most recent activity first (spec 05e).

    Active events come first, then the others by their latest spike start. `min_pages`
    counts distinct pages (canonical titles), not spikes. `number` picks one event.
    """
    params = {
        "run_id": run_id,
        "statuses": list(statuses),
        "number": number,
        "min_pages": min_pages,
        "limit": limit,
    }
    with conn.cursor(row_factory=dict_row) as cur:
        events = cur.execute(RECENT_EVENTS, params).fetchall()
        numbers = [e["number"] for e in events]
        pages = cur.execute(EVENT_PAGES, (run_id, numbers)).fetchall() if numbers else []
    by_event: dict[int, list[FeedPage]] = {}
    for row in pages:
        by_event.setdefault(row.pop("event_number"), []).append(FeedPage(**row))
    return [FeedEvent(**e, pages=tuple(by_event.get(e["number"], []))) for e in events]


def latest_event_time(conn: psycopg.Connection, run_id: str) -> datetime | None:
    """The run's "now" in event time: its latest spike start or event end."""
    row = conn.execute(
        "SELECT max(greatest(last_spike_at, coalesce(ended_at, last_spike_at))) "
        "FROM events WHERE run_id = %s",
        (run_id,),
    ).fetchone()
    return row[0] if row else None
