"""Integration tests: the `api_cache` and `pages` tables in the real Postgres.

Run with `uv run pytest -m integration`. Every test uses its own keys, titles and run id
and removes them afterwards.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.enrich.helpers import API_URL, REST_URL, FixtureTransport, make_client
from wikipulse.config import get_settings
from wikipulse.db.migrate import migrate
from wikipulse.db.pages import get_page, pending_titles, record_error, upsert_page
from wikipulse.db.spikes import delete_run, upsert_spikes
from wikipulse.detect.detector import SpikeRecord
from wikipulse.enrich.cache import PostgresCache
from wikipulse.enrich.pages import PageInfo, PageStatus, enrich

pytestmark = pytest.mark.integration

TTL = timedelta(hours=24)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(get_settings().postgres_dsn, autocommit=True) as connection:
        migrate(connection)
        yield connection


@pytest.fixture
def tag(conn: psycopg.Connection) -> Iterator[str]:
    """A unique marker for this test's cache URLs, page titles and run id."""
    marker = f"test-{uuid.uuid4().hex}"
    yield marker
    conn.execute("DELETE FROM api_cache WHERE url LIKE %s", (f"%{marker}%",))
    conn.execute("DELETE FROM pages WHERE title LIKE %s", (f"{marker}%",))
    delete_run(conn, marker)


async def test_cache_roundtrip_and_ttl(conn, tag) -> None:
    cache = PostgresCache(conn)
    url = f"https://example.org/{tag}"
    assert await cache.get(tag, TTL) is None
    await cache.put(tag, url, {"a": 1}, 200, {"extract": "Ünïcode"})
    entry = await cache.get(tag, TTL)
    assert entry is not None and entry.status == 200 and entry.body == {"extract": "Ünïcode"}
    await cache.put(tag, url, {"a": 1}, 404, None)  # overwrite
    assert (await cache.get(tag, TTL)).status == 404
    conn.execute(
        "UPDATE api_cache SET fetched_at = now() - interval '25 hours' WHERE request_key = %s",
        (tag,),
    )
    assert await cache.get(tag, TTL) is None  # expired
    assert cache.purge_older_than(TTL) >= 1
    assert conn.execute("SELECT 1 FROM api_cache WHERE request_key = %s", (tag,)).fetchone() is None


async def test_client_reuses_the_postgres_cache(conn, tag) -> None:
    """A second client with the same cache table enriches without any request."""
    first = FixtureTransport()
    async with make_client(first, PostgresCache(conn)) as client:
        info = await enrich("Syed Soharwardy", client, API_URL, REST_URL)
    second = FixtureTransport()
    async with make_client(second, PostgresCache(conn)) as client:
        again = await enrich("Syed Soharwardy", client, API_URL, REST_URL)
        assert client.stats.cache_hits == 2 and client.stats.requests == 0
    assert again == info
    assert second.requests == []
    # These rows hold real fixture URLs, not the tag; remove them explicitly.
    conn.execute(
        "DELETE FROM api_cache WHERE params->>'titles' = %s OR url LIKE %s",
        ("Syed Soharwardy", "%/page/summary/Syed_Soharwardy"),
    )


def spike(title: str, started_at: datetime) -> SpikeRecord:
    return SpikeRecord(
        title=title,
        status="active",
        started_at=started_at,
        first_edit_at=started_at,
        last_edit_at=started_at,
        ended_at=None,
        total_edits=5,
        distinct_editors=3,
        revert_edits=0,
        peak_edits_10m=5,
        score=6.0,
    )


def test_pages_roundtrip_and_errors(conn, tag) -> None:
    info = PageInfo(
        requested_title=f"{tag} A",
        title=f"{tag} B",
        status=PageStatus.OK,
        is_redirect=True,
        page_id=42,
        wikidata_id="Q42",
        description="d",
        summary="s",
        categories=("C1",),
        links=("L1", "L2"),
    )
    upsert_page(conn, info)
    assert get_page(conn, info.requested_title) == info
    record_error(conn, info.requested_title, "boom")  # never overwrites good data
    assert get_page(conn, info.requested_title) == info
    record_error(conn, f"{tag} C", "boom")
    assert get_page(conn, f"{tag} C").status is PageStatus.ERROR
    assert get_page(conn, f"{tag} nothing") is None


def test_pending_titles(conn, tag) -> None:
    now = datetime.now(UTC)
    fresh, stale, new, old_spike, failed = (f"{tag} {n}" for n in "abcde")
    upsert_spikes(
        conn,
        tag,
        [
            spike(fresh, now),
            spike(stale, now),
            spike(new, now - timedelta(hours=1)),
            spike(old_spike, now - timedelta(days=3)),
            spike(failed, now),
        ],
    )
    for title in (fresh, stale, old_spike):
        upsert_page(conn, PageInfo(requested_title=title, title=title, status=PageStatus.OK))
    record_error(conn, failed, "boom")
    # `stale` was enriched two days ago, before its spike started: due again.
    # `old_spike` was enriched two days ago too, but after its spike: nothing new.
    for title in (stale, old_spike):
        conn.execute(
            "UPDATE pages SET enriched_at = now() - interval '2 days',"
            " attempted_at = now() - interval '2 days' WHERE title = %s",
            (title,),
        )
    assert pending_titles(conn, tag, TTL) == [new, stale]
    assert pending_titles(conn, tag, TTL, error_retry=timedelta(0)) == [new, stale, failed]
    assert pending_titles(conn, tag, TTL, since=now - timedelta(minutes=5)) == [stale]
    assert len(pending_titles(conn, tag, TTL, force=True)) == 5


def test_failed_refresh_of_a_good_row_waits_before_retrying(conn, tag) -> None:
    """Review problem 1: a failed re-enrichment must not be retried on every poll."""
    now = datetime.now(UTC)
    title = f"{tag} refresh"
    upsert_spikes(conn, tag, [spike(title, now)])
    good = PageInfo(requested_title=title, title=title, status=PageStatus.OK, wikidata_id="Q1")
    upsert_page(conn, good)
    conn.execute(
        "UPDATE pages SET enriched_at = now() - interval '2 days',"
        " attempted_at = now() - interval '2 days' WHERE title = %s",
        (title,),
    )
    assert pending_titles(conn, tag, TTL) == [title]  # stale, and a new spike: due

    record_error(conn, title, "HTTP 403")
    assert get_page(conn, title) == good  # the old data is kept
    error, old_enough = conn.execute(
        "SELECT error, enriched_at < now() - interval '1 day' FROM pages WHERE title = %s",
        (title,),
    ).fetchone()
    assert error == "HTTP 403" and old_enough  # `enriched_at` is still the data's age
    assert pending_titles(conn, tag, TTL) == []  # waits for ERROR_RETRY
    assert pending_titles(conn, tag, TTL, error_retry=timedelta(0)) == [title]

    upsert_page(conn, good)  # a later success clears the error
    row = conn.execute("SELECT error FROM pages WHERE title = %s", (title,)).fetchone()
    assert row == (None,)
    assert pending_titles(conn, tag, TTL, error_retry=timedelta(0)) == []


def test_section_redirect_roundtrip(conn, tag) -> None:
    info = PageInfo(
        requested_title=f"{tag} Methos",
        title=f"{tag} List",
        status=PageStatus.OK,
        is_redirect=True,
        redirect_fragment="Recurring characters",
    )
    upsert_page(conn, info)
    assert get_page(conn, info.requested_title) == info
