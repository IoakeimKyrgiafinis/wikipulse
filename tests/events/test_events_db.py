"""Integration tests: grouping spikes stored in the real Postgres.

Run with `uv run pytest -m integration`. Every test uses its own run id and page titles
(a unique tag) and removes only those rows afterwards.
"""

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import psycopg
import pytest

from wikipulse.config import EventsConfig, get_settings
from wikipulse.db.events import delete_grouping_run, feed_events, unassigned_spikes
from wikipulse.db.graph import load_event
from wikipulse.db.migrate import migrate
from wikipulse.db.pages import upsert_page
from wikipulse.db.spikes import delete_run, spikes_for_run, upsert_spikes
from wikipulse.detect.detector import SpikeRecord
from wikipulse.enrich.pages import PageInfo, PageStatus
from wikipulse.events.embed import Embedder
from wikipulse.events.run import GroupingRunner

pytestmark = pytest.mark.integration

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
KEYWORDS = ("election", "football")


class KeywordEncoder:
    """A fake embedding model: one direction per keyword found in the text."""

    def encode(self, sentences: list[str], **kwargs) -> np.ndarray:
        vectors = np.zeros((len(sentences), len(KEYWORDS) + 1), dtype=np.float32)
        for i, text in enumerate(sentences):
            hits = [k for k, word in enumerate(KEYWORDS) if word in text.lower()]
            vectors[i, hits[0] if hits else len(KEYWORDS)] = 1.0
        return vectors


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as connection:
        migrate(connection)
        yield connection


@pytest.fixture
def tag(conn: psycopg.Connection) -> Iterator[str]:
    marker = f"test-{uuid.uuid4().hex}"
    yield marker
    delete_grouping_run(conn, marker)
    conn.execute("DELETE FROM pages WHERE title LIKE %s", (f"{marker}%",))
    delete_run(conn, marker)


def spike(title: str, start_min: float, end_min: float | None, score: float = 5.0) -> SpikeRecord:
    started = T0 + timedelta(minutes=start_min)
    ended = None if end_min is None else T0 + timedelta(minutes=end_min)
    return SpikeRecord(
        title=title,
        status="active" if ended is None else "ended",
        started_at=started,
        first_edit_at=started,
        last_edit_at=ended or started + timedelta(minutes=5),
        ended_at=ended,
        total_edits=5,
        distinct_editors=3,
        revert_edits=0,
        peak_edits_10m=5,
        score=score,
    )


def store_page(conn, title: str, summary: str, links: tuple[str, ...] = ()) -> None:
    upsert_page(
        conn,
        PageInfo(
            requested_title=title, title=title, status=PageStatus.OK, summary=summary, links=links
        ),
    )


def runner(conn, tag: str) -> GroupingRunner:
    embedder = Embedder("fake", KeywordEncoder())
    return GroupingRunner(conn, embedder, EventsConfig(), tag, tag)


def test_group_restart_and_end(conn, tag) -> None:
    election, candidate = f"{tag} Election", f"{tag} Candidate"
    store_page(conn, election, "An election was held.", links=(candidate,))
    store_page(conn, candidate, "A candidate in the election.")
    store_page(conn, f"{tag} Club", "A football club.")
    spikes = [
        spike(election, 0, 40, score=8.0),
        spike(candidate, 10, None, score=6.0),
        spike(f"{tag} Club", 15, 20),
        spike(f"{tag} Unenriched", 20, 25),
    ]
    upsert_spikes(conn, tag, spikes)

    first = runner(conn, tag)
    assert first.step() == 4
    assert unassigned_spikes(conn, tag, tag) == []
    events = {e.number: e for e in feed_events(conn, tag, include_ended=True)}
    assert [p.title for p in events[1].pages] == [election, candidate]  # by score
    assert events[1].title == election and events[1].status == "active"
    assert [p.title for p in events[2].pages] == [f"{tag} Club"]
    assert first.stats.not_enriched == 1 and first.stats.created == 3

    # The candidate's spike ends, a much later spike arrives; a new runner (a restart)
    # rebuilds the active events from Postgres and continues.
    upsert_spikes(
        conn, tag, [replace(spikes[1], status="ended", ended_at=T0 + timedelta(minutes=60))]
    )
    upsert_spikes(conn, tag, [spike(f"{tag} Later election", 300, None)])
    second = runner(conn, tag)
    assert set(second.grouper.active) == {1, 2, 3}  # nothing had ended at minute 40
    assert second.step() == 1
    events = {e.number: e for e in feed_events(conn, tag, include_ended=True)}
    assert events[1].status == "ended" and events[1].ended_at == T0 + timedelta(minutes=60)
    assert events[4].pages[0].title == f"{tag} Later election"  # numbering continued
    assert [e.number for e in feed_events(conn, tag)] == [4]  # only the active one


def test_correlation_ids_go_from_spikes_to_events_to_the_graph_runner(conn, tag) -> None:
    election, candidate = f"{tag} Election", f"{tag} Candidate"
    store_page(conn, election, "An election was held.", links=(candidate,))
    store_page(conn, candidate, "A candidate in the election.")
    upsert_spikes(
        conn,
        tag,
        [
            replace(spike(election, 0, None), correlation_id="edit-1"),
            replace(spike(candidate, 10, None), correlation_id="edit-2"),
        ],
    )
    assert [s.correlation_id for s in spikes_for_run(conn, tag)] == ["edit-1", "edit-2"]
    runner(conn, tag).step()
    stored = conn.execute(
        "SELECT number, correlation_id FROM events WHERE run_id = %s", (tag,)
    ).fetchall()
    assert stored == [(1, "edit-1")]  # the candidate joined the election's event
    assert load_event(conn, tag, 1).correlation_id == "edit-1"
