"""Scores and drift reports in the real Postgres (spec 07c). `uv run pytest -m integration`.

Every test uses its own random run id and deletes it afterwards (scores go with their
spikes: ON DELETE CASCADE)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from wikipulse.config import get_settings
from wikipulse.db.events import recent_events
from wikipulse.db.migrate import migrate
from wikipulse.db.scores import (
    SpikeScore,
    drift_reports,
    save_drift_report,
    scored_spikes,
    spikes_to_score,
    upsert_scores,
)
from wikipulse.db.spikes import delete_run, upsert_spikes
from wikipulse.detect.detector import SpikeRecord
from wikipulse.ml.features import FEATURE_NAMES

pytestmark = pytest.mark.integration

T = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as connection:
        migrate(connection)
        yield connection


@pytest.fixture
def run_id(conn) -> Iterator[str]:
    rid = f"test-{uuid.uuid4().hex}"
    yield rid
    conn.execute("DELETE FROM drift_reports WHERE run_id = %s", (rid,))
    conn.execute("DELETE FROM event_pages WHERE run_id = %s", (rid,))
    conn.execute("DELETE FROM events WHERE run_id = %s", (rid,))
    delete_run(conn, rid)


def spike(title: str, minute: int) -> SpikeRecord:
    start = T + timedelta(minutes=minute)
    return SpikeRecord(
        title=title,
        status="ended",
        started_at=start,
        first_edit_at=start - timedelta(minutes=5),
        last_edit_at=start + timedelta(minutes=20),
        ended_at=start + timedelta(minutes=50),
        total_edits=12,
        distinct_editors=5,
        revert_edits=0,
        peak_edits_10m=6,
        score=8.0,
    )


def score_of(item, run_id: str, value: float) -> SpikeScore:
    features = {name: 1.0 for name in FEATURE_NAMES} | {"page_age_days": float("nan")}
    return SpikeScore(
        spike_id=item.id,
        run_id=run_id,
        model_name="news-classifier",
        model_version="3",
        score=value,
        is_news=value >= 0.55,
        threshold=0.55,
        cutoff=item.started_at + timedelta(minutes=10),
        features=features,
        page_revision_id=123,
        history_complete=True,
    )


def test_spikes_are_scored_once_after_their_cutoff(conn, run_id):
    upsert_spikes(conn, run_id, [spike("A", 0), spike("B", 30)])
    due = spikes_to_score(conn, run_id, 10, T + timedelta(minutes=20))
    assert [s.title for s in due] == ["A"]  # B's cutoff (T+40) hasn't come yet
    assert due[0].first_edit_at == T - timedelta(minutes=5)
    upsert_scores(conn, [score_of(due[0], run_id, 0.8)])
    assert spikes_to_score(conn, run_id, 10, T + timedelta(minutes=20)) == []
    later = spikes_to_score(conn, run_id, 10, T + timedelta(hours=2))
    assert [s.title for s in later] == ["B"]
    again = spikes_to_score(conn, run_id, 10, T + timedelta(hours=2), rescore=True)
    assert [s.title for s in again] == ["A", "B"]
    skipped = spikes_to_score(conn, run_id, 10, T + timedelta(hours=2), first_edit_from=T)
    assert [s.title for s in skipped] == ["B"]  # A's burst began before the buffer did


def test_a_rescore_replaces_the_row_and_unknown_features_are_null(conn, run_id):
    upsert_spikes(conn, run_id, [spike("A", 0)])
    [item] = spikes_to_score(conn, run_id, 10, T + timedelta(hours=1))
    upsert_scores(conn, [score_of(item, run_id, 0.2)])
    upsert_scores(conn, [score_of(item, run_id, 0.9)])
    [stored] = scored_spikes(conn, run_id, "news-classifier", "3", T, T + timedelta(days=1))
    assert stored.score == 0.9 and stored.is_news
    assert stored.features["page_age_days"] is None and stored.features["edits"] == 1.0
    assert scored_spikes(conn, run_id, "news-classifier", "4", T, T + timedelta(days=1)) == []


def test_the_event_feed_shows_the_news_score(conn, run_id):
    upsert_spikes(conn, run_id, [spike("A", 0), spike("B", 5)])
    items = spikes_to_score(conn, run_id, 10, T + timedelta(hours=1))
    upsert_scores(conn, [score_of(items[0], run_id, 0.91)])  # B not scored yet
    # An event with both spikes, written directly (the grouping itself isn't tested here).
    conn.execute(
        "INSERT INTO events (run_id, spike_run_id, number, status, title, started_at, "
        "last_spike_at, page_count, score) VALUES (%s, %s, 1, 'ended', 'A', %s, %s, 2, 8)",
        (run_id, run_id, T, T + timedelta(minutes=5)),
    )
    for item in items:
        conn.execute(
            "INSERT INTO event_pages (run_id, spike_id, event_number, title, joined_score) "
            "VALUES (%s, %s, 1, %s, 1.0)",
            (run_id, item.id, item.title),
        )
    [event] = recent_events(conn, run_id)
    scores = {page.title: (page.news_score, page.is_news) for page in event.pages}
    assert scores == {"A": (0.91, True), "B": (None, None)}


def test_a_drift_report_is_stored_once_per_period(conn, run_id):
    period = (T, T + timedelta(days=7))
    for status in ("ok", "drift"):
        save_drift_report(
            conn, "news-classifier", "3", run_id, period, "v2 train", 40, status, {"x": 1}
        )
    [report] = drift_reports(conn, "news-classifier", run_id)
    assert report["status"] == "drift" and report["rows"] == 40 and report["report"] == {"x": 1}
