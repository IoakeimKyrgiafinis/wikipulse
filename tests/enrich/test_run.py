"""The enrichment runner without a database: failures are recorded, never raised."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from tests.enrich.helpers import API_URL, REST_URL, FixtureTransport, make_client
from wikipulse.enrich import run as enrich_run
from wikipulse.enrich.client import ClientStats
from wikipulse.enrich.pages import Enricher, PageInfo, PageStatus


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Replace the database writes with lists."""
    calls: dict[str, list] = {"pages": [], "errors": []}
    monkeypatch.setattr(enrich_run, "upsert_page", lambda conn, info: calls["pages"].append(info))
    monkeypatch.setattr(
        enrich_run, "record_error", lambda conn, title, error: calls["errors"].append(title)
    )
    return calls


async def test_enrich_titles_stores_every_outcome(stored) -> None:
    transport = FixtureTransport()
    titles = ["Syed Soharwardy", "Susan Wiles", "Shipley", "Invalid [title]", "Broken"]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("titles") == "Broken":
            return httpx.Response(500)  # keeps failing: the client gives up
        return transport.answer(request)

    async with make_client(httpx.MockTransport(answer), concurrency=2) as client:
        statuses = await enrich_run.enrich_titles(
            Enricher(client, API_URL, REST_URL), conn=None, titles=titles
        )
    assert statuses == {"ok": 2, "disambiguation": 1, "invalid": 1, "error": 1}
    assert {p.requested_title for p in stored["pages"]} == set(titles) - {"Broken"}
    assert stored["errors"] == ["Broken"]


def test_report_summary() -> None:
    stats = ClientStats(requests=4, cache_hits=2, cache_misses=4, by_status={200: 3, 429: 1})
    report = enrich_run.RunReport(pages=3, client=stats)
    report.statuses.update({"ok": 2, "missing": 1})
    text = report.summary()
    assert "Enriched 3 pages" in text and "API calls: 4" in text and "hit rate 33.3%" in text
    assert "HTTP statuses: 200 3, 429 1." in text


def test_parse_args_days_are_utc_midnight() -> None:
    args = enrich_run.parse_args(["--run-id", "x", "--since", "2026-11-06", "--once"])
    assert args.since == datetime(2026, 11, 6, tzinfo=UTC)
    assert args.once and not args.force and args.until is None


def test_parse_args_rejects_naive_timestamps() -> None:
    with pytest.raises(SystemExit):
        enrich_run.parse_args(["--since", "2026-11-06T10:00:00"])


class SlowEnricher:
    """A fake `Enricher` that counts the pages in progress and can stop the run."""

    def __init__(self, stop: asyncio.Event | None = None, stop_after: int = 0) -> None:
        self.in_progress = 0
        self.most = 0
        self.started: list[str] = []
        self.stop = stop
        self.stop_after = stop_after

    async def enrich(self, title: str) -> PageInfo:
        self.started.append(title)
        if self.stop is not None and len(self.started) == self.stop_after:
            self.stop.set()
        self.in_progress += 1
        self.most = max(self.most, self.in_progress)
        await asyncio.sleep(0.01)
        self.in_progress -= 1
        return PageInfo(requested_title=title, title=title, status=PageStatus.OK)


async def test_enrich_titles_keeps_a_bounded_number_of_pages_in_progress(stored) -> None:
    enricher = SlowEnricher()
    titles = [f"T{i}" for i in range(10)]
    statuses = await enrich_run.enrich_titles(enricher, conn=None, titles=titles, workers=3)
    assert statuses == {"ok": 10}
    assert enricher.most == 3
    assert enricher.started == titles  # oldest spike first
    assert [p.requested_title for p in stored["pages"]][:3] == titles[:3]


async def test_enrich_titles_stops_after_the_pages_in_progress(stored) -> None:
    stop = asyncio.Event()
    enricher = SlowEnricher(stop, stop_after=2)
    titles = [f"T{i}" for i in range(10)]
    statuses = await enrich_run.enrich_titles(enricher, None, titles, workers=2, stop=stop)
    assert enricher.started == ["T0", "T1"]
    assert statuses == {"ok": 2}


async def test_unexpected_answers_are_recorded_as_errors(stored) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": {"pages": ["not a page object"]}})

    async with make_client(httpx.MockTransport(answer)) as client:
        statuses = await enrich_run.enrich_titles(
            Enricher(client, API_URL, REST_URL), conn=None, titles=["X"]
        )
    assert statuses == {"error": 1}
    assert stored["errors"] == ["X"]
