"""A polite async client for Wikimedia's web APIs, with a response cache (stage 3).

The evaluation's `wikipulse.eval.wiki_client.WikiClient` sends one request at a time,
which is fine for a batch job but too slow to enrich pages as spikes start. This client
keeps the same politeness rules and adds limited concurrency:

- every request sends the descriptive User-Agent from config,
- at most `concurrency` requests are in flight (an `asyncio.Semaphore`), and two
  requests never start closer than `min_interval_s` apart,
- Action API requests carry `maxlag=5`, so the API turns us away when its database
  replicas lag (then we wait, as Wikimedia's API etiquette asks),
- failures (network errors, HTTP 429/5xx, `maxlag`) are retried with exponential
  backoff and jitter, honouring `Retry-After`; while one request waits to retry, no
  other request starts either (a throttled client slows down as a whole),
- answers (HTTP 200, and 404 for a page that doesn't exist) are cached with a
  time-to-live, so the same request is sent at most once per `ttl`.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx

from wikipulse.enrich.cache import ResponseCache
from wikipulse.eval.wiki_client import RETRY_STATUSES
from wikipulse.metrics.catalog import WIKI_API_REQUESTS
from wikipulse.stream.resilience import Backoff

logger = logging.getLogger(__name__)

# Answers worth caching: the page, or "this page does not exist".
CACHEABLE_STATUSES = {200, 404}
# Ask the Action API to refuse work while its replicas lag more than 5 s (API etiquette).
MAXLAG_S = 5
# Attempts per request. Backoff waits about 1, 2, 4, 8, 16 s between them, so a request
# rides out about half a minute of throttling before it is given up (and retried later).
MAX_ATTEMPTS = 6


class WikiApiError(Exception):
    """A request failed for good (after retries), or the API answered with an error."""


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: Any  # the parsed JSON body, or None if the body wasn't JSON
    from_cache: bool


@dataclass
class ClientStats:
    """Counters for the run report."""

    requests: int = 0  # HTTP requests sent over the network (retries included)
    cache_hits: int = 0
    cache_misses: int = 0
    retries: int = 0
    max_in_flight: int = 0
    by_status: dict[int, int] = field(default_factory=dict)

    @property
    def hit_rate(self) -> float:
        lookups = self.cache_hits + self.cache_misses
        return self.cache_hits / lookups if lookups else 0.0


@dataclass(frozen=True)
class Retry:
    """An attempt that failed in a way worth retrying."""

    problem: str
    wait_s: float = 0.0  # at least this long (from Retry-After), if the server said so


def request_key(url: str, params: dict[str, Any]) -> str:
    """A stable cache key for `GET url?params` (the order of params doesn't matter)."""
    request = json.dumps({"url": url, "params": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(request.encode("utf-8")).hexdigest()


def is_action_api(url: str) -> bool:
    """True for a MediaWiki Action API URL (`.../api.php`)."""
    return url.endswith("/api.php")


def api_error_code(body: Any) -> str | None:
    """The MediaWiki Action API's error code in a response body, if there is one."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("code", "unknown"))
    return None


def retry_after_s(response: httpx.Response) -> float:
    """The response's Retry-After in seconds (0 if absent or not a number)."""
    value = response.headers.get("Retry-After", "")
    return float(value) if value.isdigit() else 0.0


class WikimediaClient:
    """GET JSON from Wikimedia APIs: User-Agent, concurrency limit, retries, cache."""

    def __init__(
        self,
        user_agent: str,
        cache: ResponseCache,
        concurrency: int = 4,
        ttl: timedelta = timedelta(hours=24),
        min_interval_s: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 60.0,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.cache = cache
        self.ttl = ttl
        self.min_interval_s = min_interval_s
        self.sleep = sleep
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self.max_attempts = max_attempts
        self.http = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=True,
            transport=transport,
        )
        self._slots = asyncio.Semaphore(concurrency)
        self._turn = asyncio.Lock()
        self._next_start = 0.0  # event-loop time before which no request may start
        self._paused_until = 0.0  # after a throttle: no request starts before this
        self._in_flight = 0
        self.stats = ClientStats()

    async def aclose(self) -> None:
        await self.http.aclose()

    async def __aenter__(self) -> "WikimediaClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> ApiResponse:
        """`GET url?params` as JSON, from the cache while fresh. Raises `WikiApiError`.

        Returns statuses 200 and 404; anything else raises after the retries.
        """
        params = dict(params or {})
        if is_action_api(url):
            params.setdefault("maxlag", MAXLAG_S)
        key = request_key(url, params)
        cached = await self.cache.get(key, self.ttl)
        if cached is not None:
            self.stats.cache_hits += 1
            WIKI_API_REQUESTS.labels("cache").inc()
            return ApiResponse(cached.status, cached.body, from_cache=True)
        self.stats.cache_misses += 1
        status, body = await self._fetch(url, params)
        await self.cache.put(key, url, params, status, body)
        return ApiResponse(status, body, from_cache=False)

    async def _fetch(self, url: str, params: dict[str, Any]) -> tuple[int, Any]:
        backoff = Backoff(self.backoff_base_s, self.backoff_max_s)
        for attempt in range(1, self.max_attempts + 1):
            result = await self._try_once(url, params)
            if not isinstance(result, Retry):
                return result
            if attempt == self.max_attempts:
                raise WikiApiError(f"{url} {params}: {result.problem} (gave up after {attempt})")
            delay = max(backoff.next_delay(), result.wait_s)
            logger.warning("%s from %s; retrying in %.1f s", result.problem, url, delay)
            self.stats.retries += 1
            self._pause_everyone(delay)
            await self.sleep(delay)
        raise AssertionError("unreachable")  # the loop always returns or raises

    async def _try_once(self, url: str, params: dict[str, Any]) -> tuple[int, Any] | Retry:
        """One attempt: `(status, body)`, or a `Retry`. Raises on errors not worth retrying."""
        try:
            response = await self._send(url, params)
        except httpx.TransportError as error:
            WIKI_API_REQUESTS.labels("error").inc()
            return Retry(f"{type(error).__name__}: {error}")
        status = response.status_code
        self.stats.by_status[status] = self.stats.by_status.get(status, 0) + 1
        WIKI_API_REQUESTS.labels(str(status)).inc()
        if status in RETRY_STATUSES:
            return Retry(f"HTTP {status}", retry_after_s(response))
        if status not in CACHEABLE_STATUSES:
            raise WikiApiError(f"HTTP {status} from {response.request.url}")
        body = self._json_or_none(response)
        code = api_error_code(body)
        if code == "maxlag":
            return Retry("maxlag (replicas lagging)", max(retry_after_s(response), MAXLAG_S))
        if code is not None:
            raise WikiApiError(f"API error {code!r} from {response.request.url}: {body['error']}")
        if status == 200 and body is None:
            raise WikiApiError(f"Response from {response.request.url} is not JSON")
        return status, body

    async def _send(self, url: str, params: dict[str, Any]) -> httpx.Response:
        async with self._slots:  # at most `concurrency` requests in flight
            await self._wait_for_turn()
            self._in_flight += 1
            self.stats.max_in_flight = max(self.stats.max_in_flight, self._in_flight)
            self.stats.requests += 1
            try:
                return await self.http.get(url, params=params)
            finally:
                self._in_flight -= 1

    def _pause_everyone(self, seconds: float) -> None:
        """Hold back every new request for `seconds` (the server asked us to slow down)."""
        until = asyncio.get_running_loop().time() + seconds
        self._paused_until = max(self._paused_until, until)

    async def _wait_for_turn(self) -> None:
        """Wait out a pause after a failure, and space request starts `min_interval_s` apart."""
        loop = asyncio.get_running_loop()
        async with self._turn:
            paused_until = self._paused_until
            if paused_until > loop.time():
                await self.sleep(paused_until - loop.time())
                if self._paused_until == paused_until:  # not extended meanwhile: it's over
                    self._paused_until = 0.0
            if self.min_interval_s <= 0:
                return
            # A loop, because asyncio may wake a sleeper up to one clock tick early
            # (about 16 ms on Windows).
            while (wait := self._next_start - loop.time()) > 0:
                await asyncio.sleep(wait)
            self._next_start = loop.time() + self.min_interval_s

    @staticmethod
    def _json_or_none(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None
