"""The async Wikimedia client: User-Agent, cache and TTL, retries, concurrency limit."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.enrich.helpers import API_URL, REST_URL, USER_AGENT, make_client
from wikipulse.enrich.cache import MemoryCache
from wikipulse.enrich.client import MAXLAG_S, WikiApiError, request_key
from wikipulse.eval.wiki_client import MAX_ATTEMPTS

SUMMARY = f"{REST_URL}/page/summary/X"


def answering(*responses: httpx.Response):
    """A transport that gives the responses in order (the last one again and again)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses[min(len(seen), len(responses)) - 1]

    return httpx.MockTransport(handler), seen


async def test_sends_user_agent_and_maxlag_on_the_action_api_only() -> None:
    transport, seen = answering(httpx.Response(200, json={"ok": 1}))
    async with make_client(transport) as client:
        await client.get_json(API_URL, {"action": "query"})
        await client.get_json(SUMMARY)
    assert all(r.headers["User-Agent"] == USER_AGENT for r in seen)
    assert seen[0].url.params["maxlag"] == str(MAXLAG_S)
    assert "maxlag" not in seen[1].url.params


async def test_cache_hit_makes_no_request() -> None:
    transport, seen = answering(httpx.Response(200, json={"extract": "x"}))
    async with make_client(transport) as client:
        first = await client.get_json(SUMMARY)
        second = await client.get_json(SUMMARY)
    assert len(seen) == 1
    assert not first.from_cache and second.from_cache
    assert second.body == {"extract": "x"}
    assert (client.stats.cache_hits, client.stats.cache_misses) == (1, 1)
    assert client.stats.hit_rate == 0.5


async def test_cache_entries_expire_after_the_ttl() -> None:
    now = [datetime(2026, 11, 7, tzinfo=UTC)]
    cache = MemoryCache(clock=lambda: now[0])
    transport, seen = answering(httpx.Response(200, json={}))
    async with make_client(transport, cache, ttl=timedelta(hours=24)) as client:
        await client.get_json(SUMMARY)
        now[0] += timedelta(hours=23)
        await client.get_json(SUMMARY)
        assert len(seen) == 1
        now[0] += timedelta(hours=2)
        await client.get_json(SUMMARY)
    assert len(seen) == 2


async def test_not_found_is_cached_too() -> None:
    transport, seen = answering(httpx.Response(404, json={"type": "not_found"}))
    async with make_client(transport) as client:
        assert (await client.get_json(SUMMARY)).status == 404
        assert (await client.get_json(SUMMARY)).status == 404
    assert len(seen) == 1


def test_request_key_ignores_parameter_order() -> None:
    assert request_key(API_URL, {"a": 1, "b": 2}) == request_key(API_URL, {"b": 2, "a": 1})
    assert request_key(API_URL, {"a": 1}) != request_key(API_URL, {"a": 2})


async def test_retries_server_errors_honouring_retry_after() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    transport, seen = answering(
        httpx.Response(503, headers={"Retry-After": "7"}), httpx.Response(200, json={"a": 1})
    )
    async with make_client(transport, sleep=sleep) as client:
        response = await client.get_json(SUMMARY)
    assert response.body == {"a": 1}
    assert len(seen) == 2 and sleeps[0] >= 7
    assert client.stats.retries == 1


async def test_retries_maxlag_errors() -> None:
    maxlag = httpx.Response(200, json={"error": {"code": "maxlag", "info": "lagging"}})
    transport, seen = answering(maxlag, httpx.Response(200, json={"query": {}}))
    async with make_client(transport) as client:
        assert (await client.get_json(API_URL, {"action": "query"})).body == {"query": {}}
    assert len(seen) == 2


async def test_retries_network_errors() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("no route", request=request)
        return httpx.Response(200, json={})

    async with make_client(httpx.MockTransport(handler)) as client:
        await client.get_json(SUMMARY)
    assert len(calls) == 2


async def test_gives_up_after_max_attempts() -> None:
    transport, seen = answering(httpx.Response(502))
    async with make_client(transport) as client:
        with pytest.raises(WikiApiError):
            await client.get_json(SUMMARY)
    assert len(seen) == MAX_ATTEMPTS


async def test_api_errors_raise_and_are_not_cached() -> None:
    error = httpx.Response(200, json={"error": {"code": "badvalue", "info": "nope"}})
    transport, seen = answering(error)
    cache = MemoryCache()
    async with make_client(transport, cache) as client:
        with pytest.raises(WikiApiError, match="badvalue"):
            await client.get_json(API_URL, {"action": "query"})
    assert len(seen) == 1 and cache.entries == {}


async def test_client_errors_are_not_retried() -> None:
    transport, seen = answering(httpx.Response(403))
    async with make_client(transport) as client:
        with pytest.raises(WikiApiError, match="403"):
            await client.get_json(SUMMARY)
    assert len(seen) == 1


async def test_never_more_than_concurrency_requests_in_flight() -> None:
    in_flight = 0
    most = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, most
        in_flight += 1
        most = max(most, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json={})

    async with make_client(httpx.MockTransport(handler), concurrency=3) as client:
        await asyncio.gather(*(client.get_json(f"{REST_URL}/page/summary/P{i}") for i in range(20)))
    assert most == 3  # the limit was reached, never exceeded
    assert client.stats.max_in_flight == 3
    assert client.stats.requests == 20


async def test_request_starts_are_spaced() -> None:
    starts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(asyncio.get_running_loop().time())
        return httpx.Response(200, json={})

    async with make_client(
        httpx.MockTransport(handler), concurrency=4, min_interval_s=0.05
    ) as client:
        await asyncio.gather(*(client.get_json(f"{REST_URL}/page/summary/P{i}") for i in range(4)))
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert all(gap >= 0.04 for gap in gaps)  # a little slack for the timer resolution
