"""The news classifier's scores and drift reports in Postgres (migrations/0008).

Both writes are upserts: scoring a spike again (after a restart, or `--rescore`)
replaces its row, and a drift report for the same model, run and period replaces the
older one. So every job here can be repeated safely.
"""

import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class SpikeToScore:
    """What scoring needs of a stored spike: who it is and when it began (no totals)."""

    id: int
    run_id: str
    title: str
    first_edit_at: datetime
    started_at: datetime


@dataclass(frozen=True)
class SpikeScore:
    """One row of `spike_scores`."""

    spike_id: int
    run_id: str
    model_name: str
    model_version: str
    score: float
    is_news: bool
    threshold: float
    cutoff: datetime
    features: dict[str, float]
    page_revision_id: int | None
    history_complete: bool


def json_features(features: dict[str, float]) -> dict[str, float | None]:
    """NaN (unknown) is not valid JSON; it is stored as null."""
    return {k: None if v is None or math.isnan(v) else float(v) for k, v in features.items()}


UNSCORED = """
SELECT s.id, s.run_id, s.title, s.first_edit_at, s.started_at
FROM spikes s
LEFT JOIN spike_scores sc ON sc.spike_id = s.id
WHERE s.run_id = %(run_id)s
  AND (%(rescore)s OR sc.spike_id IS NULL)
  AND s.started_at + %(window)s <= %(cutoff_until)s::timestamptz
  AND (%(first_edit_from)s::timestamptz IS NULL OR s.first_edit_at >= %(first_edit_from)s)
  AND (%(since)s::timestamptz IS NULL OR s.started_at >= %(since)s::timestamptz)
  AND (%(until)s::timestamptz IS NULL OR s.started_at < %(until)s::timestamptz)
ORDER BY s.started_at, s.title
LIMIT %(limit)s
"""


def spikes_to_score(
    conn: psycopg.Connection,
    run_id: str,
    window_min: float,
    cutoff_until: datetime,
    first_edit_from: datetime | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    rescore: bool = False,
    limit: int = 500,
) -> list[SpikeToScore]:
    """Spikes of a run whose cutoff (`started_at + window_min`) is at or before
    `cutoff_until`, not scored yet (all of them with `rescore`), oldest first.

    `first_edit_from` leaves out spikes whose burst began before that moment (the live
    scorer hasn't seen their edits); `since`/`until` filter on `started_at`."""
    params = {
        "run_id": run_id,
        "rescore": rescore,
        "window": timedelta(minutes=window_min),
        "cutoff_until": cutoff_until,
        "first_edit_from": first_edit_from,
        "since": since,
        "until": until,
        "limit": limit,
    }
    with conn.cursor(row_factory=dict_row) as cur:
        return [SpikeToScore(**row) for row in cur.execute(UNSCORED, params).fetchall()]


UPSERT = """
INSERT INTO spike_scores (spike_id, run_id, model_name, model_version, score, is_news,
                          threshold, cutoff, features, page_revision_id, history_complete)
VALUES (%(spike_id)s, %(run_id)s, %(model_name)s, %(model_version)s, %(score)s, %(is_news)s,
        %(threshold)s, %(cutoff)s, %(features)s, %(page_revision_id)s, %(history_complete)s)
ON CONFLICT (spike_id) DO UPDATE SET
    run_id = EXCLUDED.run_id, model_name = EXCLUDED.model_name,
    model_version = EXCLUDED.model_version, score = EXCLUDED.score,
    is_news = EXCLUDED.is_news, threshold = EXCLUDED.threshold, cutoff = EXCLUDED.cutoff,
    features = EXCLUDED.features, page_revision_id = EXCLUDED.page_revision_id,
    history_complete = EXCLUDED.history_complete, scored_at = now()
"""


def upsert_scores(conn: psycopg.Connection, scores: Iterable[SpikeScore]) -> int:
    """Store scores (replacing a spike's earlier score), in one transaction."""
    rows = [{**asdict(s), "features": Jsonb(json_features(s.features))} for s in scores]
    if not rows:
        return 0
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
    return len(rows)


@dataclass(frozen=True)
class ScoredSpike:
    """A stored score with its features, for drift monitoring."""

    spike_id: int
    title: str
    cutoff: datetime
    score: float
    is_news: bool
    features: dict[str, float | None]


def scored_spikes(
    conn: psycopg.Connection,
    run_id: str,
    model_name: str,
    model_version: str,
    since: datetime,
    until: datetime,
) -> list[ScoredSpike]:
    """Scores of one model version for a run's spikes with a cutoff in [since, until)."""
    query = """
        SELECT sc.spike_id, s.title, sc.cutoff, sc.score, sc.is_news, sc.features
        FROM spike_scores sc JOIN spikes s ON s.id = sc.spike_id
        WHERE sc.run_id = %s AND sc.model_name = %s AND sc.model_version = %s
          AND sc.cutoff >= %s AND sc.cutoff < %s
        ORDER BY sc.cutoff, s.title
    """
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(query, (run_id, model_name, model_version, since, until)).fetchall()
    return [ScoredSpike(**row) for row in rows]


def score_versions(
    conn: psycopg.Connection, run_id: str, since: datetime, until: datetime
) -> dict[str, int]:
    """How many of a run's spikes in [since, until) each model version scored."""
    rows = conn.execute(
        "SELECT model_name || '/' || model_version, count(*) FROM spike_scores "
        "WHERE run_id = %s AND cutoff >= %s AND cutoff < %s GROUP BY 1 ORDER BY 1",
        (run_id, since, until),
    ).fetchall()
    return {name: count for name, count in rows}


SAVE_REPORT = """
INSERT INTO drift_reports (model_name, model_version, run_id, period_start, period_end,
                           reference, rows, status, report)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (model_name, model_version, run_id, period_start, period_end) DO UPDATE SET
    reference = EXCLUDED.reference, rows = EXCLUDED.rows, status = EXCLUDED.status,
    report = EXCLUDED.report, created_at = now()
"""


def save_drift_report(
    conn: psycopg.Connection,
    model_name: str,
    model_version: str,
    run_id: str,
    period: tuple[datetime, datetime],
    reference: str,
    rows: int,
    status: str,
    report: dict[str, Any],
) -> None:
    """Store (or replace) the drift report of one model version, run and period."""
    text = json.dumps(report, allow_nan=False)  # NaN must have been turned into null
    with conn.transaction():
        conn.execute(
            SAVE_REPORT,
            (
                model_name,
                model_version,
                run_id,
                *period,
                reference,
                rows,
                status,
                Jsonb(json.loads(text)),
            ),
        )


def drift_reports(
    conn: psycopg.Connection, model_name: str, run_id: str | None = None
) -> list[dict[str, Any]]:
    """The stored drift reports of a model, newest period first."""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT * FROM drift_reports WHERE model_name = %s "
            "AND (%s::text IS NULL OR run_id = %s) ORDER BY period_start DESC, model_version",
            (model_name, run_id, run_id),
        ).fetchall()
