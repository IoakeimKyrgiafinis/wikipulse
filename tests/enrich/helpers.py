"""Replay saved Wikimedia responses (tests/fixtures/enrich/) through httpx.MockTransport."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from wikipulse.enrich.cache import MemoryCache
from wikipulse.enrich.client import WikimediaClient

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "enrich"
API_URL = "https://en.wikipedia.org/w/api.php"
REST_URL = "https://en.wikipedia.org/api/rest_v1"
Handler = Callable[[httpx.Request], httpx.Response]
USER_AGENT = "WikiPulse-tests (https://example.org; tests@example.org)"


def request_id(url: str, params: dict[str, Any]) -> tuple[str, frozenset]:
    """Identify a request by URL and parameters, ignoring `maxlag` and parameter order."""
    return url, frozenset((k, str(v)) for k, v in params.items() if k != "maxlag")


def load_responses() -> dict[tuple[str, frozenset], tuple[int, Any]]:
    responses = {}
    for path in sorted(FIXTURES.glob("*.json")):
        for exchange in json.loads(path.read_text(encoding="utf-8")):
            key = request_id(exchange["url"], exchange["params"])
            responses[key] = (exchange["status"], exchange["body"])
    return responses


class FixtureTransport(httpx.MockTransport):
    """Answers known requests from the fixtures and records every request it sees."""

    def __init__(self, overrides: dict[str, Handler] | None = None) -> None:
        self.responses = load_responses()
        self.overrides = overrides or {}
        self.requests: list[httpx.Request] = []
        super().__init__(self.answer)

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url.copy_with(query=None))
        for fragment, handler in self.overrides.items():
            if fragment in url:
                return handler(request)
        key = request_id(url, dict(request.url.params))
        if key not in self.responses:
            raise AssertionError(f"No saved response for {request.url}")
        status, body = self.responses[key]
        return httpx.Response(status, json=body)


async def no_sleep(seconds: float) -> None:
    return None


def make_client(transport: httpx.AsyncBaseTransport, cache=None, **kwargs) -> WikimediaClient:
    """A client for tests: no pauses between requests, no real sleeping on retries."""
    kwargs.setdefault("min_interval_s", 0)
    kwargs.setdefault("sleep", no_sleep)
    return WikimediaClient(USER_AGENT, cache or MemoryCache(), transport=transport, **kwargs)
