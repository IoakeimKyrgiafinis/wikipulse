"""The `graph_extractions` table (migration 0005) and the events the graph runner reads.

An event needs extraction when it has no row yet, when it changed materially since its
last extraction (its `fingerprint`, see `event_fingerprint()`), or when its last attempt
failed and the retry delay has passed. The delay doubles with every failure in a row
(10 min, 20 min, ... up to 5 h 20 min with the default), so a broken event doesn't cost
an LLM call on every poll.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from wikipulse.graph.inputs import EventInput, pages_from_rows

# Marks the fingerprint of an event that is still active (see `event_fingerprint()`).
ACTIVE_MARK = "|active"

# The run's events that are not merged away, with their spike ids in a fixed order.
CURRENT = """
SELECT ev.run_id, ev.number, ev.status, ev.title, ev.started_at, ev.ended_at,
       string_agg(ep.spike_id::text, ',' ORDER BY ep.spike_id) AS spike_ids
FROM events ev
JOIN event_pages ep ON ep.run_id = ev.run_id AND ep.event_number = ev.number
WHERE ev.run_id = %(run_id)s AND ev.status <> 'merged'
  AND (%(since)s::timestamptz IS NULL OR ev.started_at >= %(since)s::timestamptz)
  AND (%(until)s::timestamptz IS NULL OR ev.started_at < %(until)s::timestamptz)
GROUP BY ev.run_id, ev.number, ev.status, ev.title, ev.started_at, ev.ended_at
"""

# Every current event with what its last extraction stored; Python decides what is due.
CANDIDATES = f"""
WITH current AS ({CURRENT})
SELECT c.number, c.status, c.spike_ids, g.fingerprint AS stored,
       g.status = 'error'
       AND g.attempted_at < now() - %(retry)s * power(2, least(g.failures - 1, 5))
       AS retry_due
FROM current c
LEFT JOIN graph_extractions g ON g.run_id = c.run_id AND g.event_number = c.number
ORDER BY c.started_at, c.number
"""

EVENT_SPIKES = """
SELECT s.title, s.first_edit_at, s.last_edit_at, s.score,
       p.status AS page_status, p.canonical_title, p.wikidata_id, p.description,
       p.summary, p.links
FROM event_pages ep
JOIN spikes s ON s.id = ep.spike_id
LEFT JOIN pages p ON p.title = s.title
WHERE ep.run_id = %s AND ep.event_number = %s
ORDER BY s.score DESC, s.started_at, s.title
"""

RECORD = """
INSERT INTO graph_extractions (run_id, event_number, status, fingerprint, failures, error,
                               model, prompt_version, pages, entities, linked, relations,
                               seconds, llm_seconds, input_tokens, output_tokens,
                               attempted_at)
VALUES (%(run_id)s, %(event_number)s, %(status)s, %(fingerprint)s, %(failures)s, %(error)s,
        %(model)s, %(prompt_version)s, %(pages)s, %(entities)s, %(linked)s, %(relations)s,
        %(seconds)s, %(llm_seconds)s, %(input_tokens)s, %(output_tokens)s, now())
ON CONFLICT (run_id, event_number) DO UPDATE SET
    status = EXCLUDED.status, fingerprint = EXCLUDED.fingerprint,
    failures = 0, error = NULL, model = EXCLUDED.model,
    prompt_version = EXCLUDED.prompt_version, pages = EXCLUDED.pages,
    entities = EXCLUDED.entities, linked = EXCLUDED.linked, relations = EXCLUDED.relations,
    seconds = EXCLUDED.seconds, llm_seconds = EXCLUDED.llm_seconds,
    input_tokens = EXCLUDED.input_tokens, output_tokens = EXCLUDED.output_tokens,
    attempted_at = now()
"""

# A failed attempt keeps the numbers of the last good extraction (the graph still has it).
RECORD_FAILURE = """
INSERT INTO graph_extractions (run_id, event_number, status, fingerprint, failures, error,
                               seconds, attempted_at)
VALUES (%(run_id)s, %(event_number)s, 'error', %(fingerprint)s, 1, %(error)s, %(seconds)s,
        now())
ON CONFLICT (run_id, event_number) DO UPDATE SET
    status = 'error', fingerprint = EXCLUDED.fingerprint,
    failures = graph_extractions.failures + 1, error = EXCLUDED.error,
    seconds = EXCLUDED.seconds, attempted_at = now()
"""


@dataclass(frozen=True)
class ExtractionResult:
    """What one successful extraction produced (a row of `graph_extractions`)."""

    model: str
    prompt_version: str
    pages: int
    entities: int
    linked: int
    relations: int
    seconds: float
    input_tokens: int
    output_tokens: int
    llm_seconds: float


def event_fingerprint(spike_ids: str, status: str) -> str:
    """What an event's extraction depends on; when it changes, the event is extracted again.

    The rule is cheap on purpose: the sorted spike ids, plus a mark while the event is
    still active. So an event is extracted again when a spike joins it (or it absorbs
    another event), and once more when it ends, by which time its spikes have finished
    growing and the diff covers the whole burst. Growth of a spike while the event is
    active does not trigger a new LLM call; the extraction at the end catches it up.
    Ended events keep the plain id list, which is the format of the first version.
    """
    return spike_ids + ACTIVE_MARK if status == "active" else spike_ids


def needs_extraction(
    spike_ids: str, status: str, stored: str | None, retry_due: bool | None, force: bool
) -> bool:
    """Whether an event is due: forced, never tried, changed, or a failure ready to retry."""
    if force or stored is None:
        return True
    return stored != event_fingerprint(spike_ids, status) or bool(retry_due)


def pending_events(
    conn: psycopg.Connection,
    run_id: str,
    retry: timedelta = timedelta(minutes=10),
    since: datetime | None = None,
    until: datetime | None = None,
    force: bool = False,
    limit: int | None = None,
) -> list[int]:
    """Numbers of the run's events that need extraction, oldest first."""
    params = {"run_id": run_id, "since": since, "until": until, "retry": retry}
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(CANDIDATES, params).fetchall()
    numbers = [
        row["number"]
        for row in rows
        if needs_extraction(row["spike_ids"], row["status"], row["stored"], row["retry_due"], force)
    ]
    return numbers if limit is None else numbers[:limit]


def load_event(conn: psycopg.Connection, run_id: str, number: int) -> EventInput | None:
    """An event with its pages (None if it is gone or was merged into another)."""
    query = f"{CURRENT} HAVING ev.number = %(number)s"
    params = {"run_id": run_id, "since": None, "until": None, "number": number}
    with conn.cursor(row_factory=dict_row) as cur:
        event = cur.execute(query, params).fetchone()
        if event is None:
            return None
        spikes = cur.execute(EVENT_SPIKES, (run_id, number)).fetchall()
    fingerprint = event_fingerprint(event.pop("spike_ids"), event["status"])
    return EventInput(**event, fingerprint=fingerprint, pages=tuple(pages_from_rows(spikes)))


def record_success(conn: psycopg.Connection, event: EventInput, result: ExtractionResult) -> None:
    conn.execute(
        RECORD,
        {
            "run_id": event.run_id,
            "event_number": event.number,
            "status": "ok",
            "fingerprint": event.fingerprint,
            "failures": 0,
            "error": None,
            **result.__dict__,
        },
    )


def record_failure(conn: psycopg.Connection, event: EventInput, error: str, seconds: float) -> None:
    """Remember a failed attempt; the event is tried again after the retry delay."""
    conn.execute(
        RECORD_FAILURE,
        {
            "run_id": event.run_id,
            "event_number": event.number,
            "fingerprint": event.fingerprint,
            "error": error[:1000],
            "seconds": seconds,
        },
    )


def current_events(conn: psycopg.Connection, run_id: str) -> list[int]:
    """Numbers of the run's events that exist in Postgres and are not merged away."""
    rows = conn.execute(
        "SELECT number FROM events WHERE run_id = %s AND status <> 'merged'", (run_id,)
    ).fetchall()
    return [row[0] for row in rows]


def forget_extraction(conn: psycopg.Connection, run_id: str, number: int) -> None:
    conn.execute(
        "DELETE FROM graph_extractions WHERE run_id = %s AND event_number = %s", (run_id, number)
    )


def extracted_events(conn: psycopg.Connection, run_id: str) -> list[int]:
    """Numbers of every event of the run with an extraction row (ok or not)."""
    rows = conn.execute(
        "SELECT event_number FROM graph_extractions WHERE run_id = %s ORDER BY event_number",
        (run_id,),
    ).fetchall()
    return [row[0] for row in rows]
