"""The enrichment runner without a database: failures are recorded, never raised."""

from datetime import UTC, datetime

import httpx
import pytest

from tests.enrich.helpers import API_URL, REST_URL, FixtureTransport, make_client
from wikipulse.enrich import run as enrich_run
from wikipulse.enrich.client import ClientStats
from wikipulse.enrich.pages import Enricher


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
    stats = ClientStats(requests=4, cache_hits=2, cache_misses=4)
    report = enrich_run.RunReport(pages=3, client=stats)
    report.statuses.update({"ok": 2, "missing": 1})
    text = report.summary()
    assert "Enriched 3 pages" in text and "API calls: 4" in text and "hit rate 33.3%" in text


def test_parse_args_days_are_utc_midnight() -> None:
    args = enrich_run.parse_args(["--run-id", "x", "--since", "2026-11-06", "--once"])
    assert args.since == datetime(2026, 11, 6, tzinfo=UTC)
    assert args.once and not args.force and args.until is None


def test_parse_args_rejects_naive_timestamps() -> None:
    with pytest.raises(SystemExit):
        enrich_run.parse_args(["--since", "2026-11-06T10:00:00"])
