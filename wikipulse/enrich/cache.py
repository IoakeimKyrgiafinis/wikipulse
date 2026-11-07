"""Response caches for the enrichment client: in Postgres (table `api_cache`) or in memory.

A cache stores one entry per request key (see `client.request_key`): the HTTP status,
the JSON body and when it was fetched. `get(key, max_age)` returns an entry only while
it is younger than `max_age`; older entries count as missing and get overwritten.

`fetched_at` is wall-clock time on purpose: it measures how old our *copy* of a web
page is, which has nothing to do with the event time of edits.
"""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class CachedResponse:
    status: int
    body: Any
    fetched_at: datetime


class ResponseCache(Protocol):
    """What the client needs from a cache."""

    async def get(self, key: str, max_age: timedelta) -> CachedResponse | None: ...

    async def put(
        self, key: str, url: str, params: dict[str, Any], status: int, body: Any
    ) -> None: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


class MemoryCache:
    """A cache in a dict, for tests and one-off scripts. `clock` can be replaced in tests."""

    def __init__(self, clock: Callable[[], datetime] = utc_now) -> None:
        self.clock = clock
        self.entries: dict[str, CachedResponse] = {}

    async def get(self, key: str, max_age: timedelta) -> CachedResponse | None:
        entry = self.entries.get(key)
        if entry is None or self.clock() - entry.fetched_at > max_age:
            return None
        return entry

    async def put(self, key: str, url: str, params: dict[str, Any], status: int, body: Any) -> None:
        # Round-trip through JSON so the memory cache returns what Postgres would.
        stored = json.loads(json.dumps(body))
        self.entries[key] = CachedResponse(status, stored, self.clock())


SELECT_FRESH = """
SELECT status, body, fetched_at FROM api_cache
WHERE request_key = %s AND fetched_at > now() - %s
"""

UPSERT = """
INSERT INTO api_cache (request_key, url, params, status, body, fetched_at)
VALUES (%s, %s, %s, %s, %s, now())
ON CONFLICT (request_key) DO UPDATE SET
    url = EXCLUDED.url, params = EXCLUDED.params, status = EXCLUDED.status,
    body = EXCLUDED.body, fetched_at = EXCLUDED.fetched_at
"""


class PostgresCache:
    """The `api_cache` table (migration 0002). Freshness is decided by Postgres' clock.

    psycopg's synchronous connection is thread-safe, so each query runs in a worker
    thread (`asyncio.to_thread`) and the event loop keeps serving other requests. This
    also avoids psycopg's async mode, which doesn't run on Windows' default event loop.
    `conn` must be in autocommit mode.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    async def get(self, key: str, max_age: timedelta) -> CachedResponse | None:
        return await asyncio.to_thread(self._get, key, max_age)

    async def put(self, key: str, url: str, params: dict[str, Any], status: int, body: Any) -> None:
        await asyncio.to_thread(self._put, key, url, params, status, body)

    def _get(self, key: str, max_age: timedelta) -> CachedResponse | None:
        row = self.conn.execute(SELECT_FRESH, (key, max_age)).fetchone()
        return CachedResponse(*row) if row else None

    def _put(self, key: str, url: str, params: dict[str, Any], status: int, body: Any) -> None:
        self.conn.execute(UPSERT, (key, url, Jsonb(params), status, Jsonb(body)))

    def purge_older_than(self, max_age: timedelta) -> int:
        """Delete entries older than `max_age` (they would never be used). Returns the count."""
        return self.conn.execute(
            "DELETE FROM api_cache WHERE fetched_at <= now() - %s", (max_age,)
        ).rowcount
