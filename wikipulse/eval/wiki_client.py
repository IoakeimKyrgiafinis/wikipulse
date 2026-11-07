"""A small, polite, caching client for Wikimedia's web APIs (used by the evaluation).

Politeness rules from AGENTS.md, all enforced here:
- every request sends the descriptive User-Agent from config,
- one request at a time, with at least `min_interval_s` between two requests,
- retries back off exponentially (and honour `Retry-After` on HTTP 429),
- every successful response is cached as a JSON file, so a repeated evaluation
  costs no requests at all.

Whether a cached response is still good enough is the caller's decision
(`is_fresh`): a portal page of last month never changes, today's page does.
"""

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Bounds on retrying one request: 1 s, 2 s, 4 s, ... at most this many attempts.
MAX_ATTEMPTS = 4
RETRY_STATUSES = {429, 500, 502, 503, 504}


def always_fresh(fetched_at: datetime) -> bool:
    """Freshness rule for responses that never change (e.g. old revisions)."""
    return True


def cache_file_name(label: str, url: str, params: dict[str, Any]) -> str:
    """A readable, unique file name: `<label>-<hash of the request>.json`."""
    request = json.dumps({"url": url, "params": params}, sort_keys=True)
    digest = hashlib.sha256(request.encode("utf-8")).hexdigest()[:12]
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:80]
    return f"{safe_label}-{digest}.json"


class WikiClient:
    """GET JSON from Wikimedia APIs with a User-Agent, a rate limit, retries and a cache."""

    def __init__(
        self,
        user_agent: str,
        cache_dir: Path,
        min_interval_s: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_dir = cache_dir
        self.min_interval_s = min_interval_s
        self.sleep = sleep
        self.http = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=30.0,
            follow_redirects=True,
            transport=transport,
        )
        self._last_request = 0.0  # monotonic time of the last request
        self.requests = 0  # network requests made (for the report)
        self.cache_hits = 0

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "WikiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        label: str = "response",
        is_fresh: Callable[[datetime], bool] = always_fresh,
    ) -> dict[str, Any]:
        """The JSON body of `GET url?params`, from the cache when it is fresh enough."""
        params = params or {}
        path = self.cache_dir / cache_file_name(label, url, params)
        cached = self._read_cache(path)
        if cached is not None and is_fresh(cached["fetched_at"]):
            self.cache_hits += 1
            return cached["body"]
        body = self._fetch(url, params)
        self._write_cache(path, url, params, body)
        return body

    def _read_cache(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            entry["fetched_at"] = datetime.fromisoformat(entry["fetched_at"])
            return entry
        except (ValueError, KeyError):
            logger.warning("Ignoring unreadable cache file %s", path)
            return None

    def _write_cache(self, path: Path, url: str, params: dict[str, Any], body: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "url": url,
            "params": params,
            # Wall clock is right here: it says how old the cached copy is.
            "fetched_at": datetime.now(UTC).isoformat(),
            "body": body,
        }
        path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8", newline="\n")

    def _wait_for_turn(self) -> None:
        wait = self._last_request + self.min_interval_s - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last_request = time.monotonic()

    def _fetch(self, url: str, params: dict[str, Any]) -> Any:
        delay = 1.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_for_turn()
            self.requests += 1
            try:
                response = self.http.get(url, params=params)
            except httpx.TransportError as error:
                if attempt == MAX_ATTEMPTS:
                    raise
                logger.warning("Request to %s failed (%s); retrying in %.0f s", url, error, delay)
            else:
                if response.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                    response.raise_for_status()
                    return response.json()
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                logger.warning(
                    "HTTP %d from %s; retrying in %.0f s", response.status_code, url, delay
                )
            self.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")  # the loop always returns or raises
