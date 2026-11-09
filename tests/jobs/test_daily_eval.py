"""The daily evaluation job without network or database (portal pages from fixtures)."""

from datetime import UTC, date, datetime

import pytest

from tests.eval.helpers import at, spike
from tests.eval.test_ground_truth import load
from wikipulse.config import Settings
from wikipulse.eval import detection
from wikipulse.eval.ground_truth import PortalDay, parse_portal_response
from wikipulse.jobs import daily_eval

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 9, 3, tzinfo=UTC)


def fake_portal(client, api_url: str, day: date) -> PortalDay:
    """The saved 6 November page; the days around it are missing."""
    if day == DAY:
        return parse_portal_response(day, load("portal-2026-11-06.json"))
    return PortalDay(day, [], [], missing=True)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detection, "fetch_portal_day", fake_portal)


def test_portal_range_is_the_day_and_two_days_either_side() -> None:
    assert daily_eval.portal_range(DAY) == (date(2026, 11, 4), date(2026, 11, 8))


@pytest.mark.parametrize("today", [DAY, date(2026, 11, 5)])
def test_a_day_that_is_not_over_cannot_be_evaluated(today: date) -> None:
    with pytest.raises(daily_eval.DayNotOverError):
        daily_eval.check_day_is_over(DAY, today)


def test_fetch_returns_the_portal_days_around_the_day() -> None:
    portal = daily_eval.fetch_ground_truth(DAY, None, "https://example.invalid", NOW.date())
    assert [p.day for p in portal] == [date(2026, 11, n) for n in range(4, 9)]
    assert not portal[2].missing and portal[2].links


def test_fetch_does_not_ask_for_days_that_have_not_come_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[date] = []

    def recording_portal(client, api_url: str, day: date) -> PortalDay:
        asked.append(day)
        return fake_portal(client, api_url, day)

    monkeypatch.setattr(detection, "fetch_portal_day", recording_portal)
    daily_eval.fetch_ground_truth(DAY, None, "https://example.invalid", date(2026, 11, 7))
    # Today's page exists (it is being written); tomorrow's can't yet.
    assert asked == [date(2026, 11, n) for n in (4, 5, 6, 7)]


def test_fetch_fails_when_the_days_own_page_is_missing() -> None:
    with pytest.raises(daily_eval.GroundTruthMissingError):
        daily_eval.fetch_ground_truth(
            date(2026, 11, 5), None, "https://example.invalid", NOW.date()
        )


def test_evaluate_day_scores_only_that_days_spikes() -> None:
    portal = daily_eval.fetch_ground_truth(DAY, None, "https://example.invalid", NOW.date())
    spikes = [
        spike("Thomas Markle", at(6, 18), delay_min=7),  # on the 6 November portal
        spike("Some Film", at(6, 12)),  # not on the portal
        spike("Pedro Alliana", at(5, 23), delay_min=11),  # the day before: recall only
    ]
    config = detection.config_snapshot(Settings(_env_file=None), {})
    git = {"commit": "abc", "dirty": False}
    report = daily_eval.evaluate_day(DAY, "live", spikes, portal, [], config, git, NOW)
    assert report["name"] == "daily-live"
    assert (report["date_from"], report["date_to"]) == ("2026-11-06", "2026-11-06")
    metrics = report["metrics"]
    assert metrics["spikes"] == 2
    assert metrics["spikes_on_portal"] == 1
    assert metrics["portal_precision"] == 0.5
    # Recall may use the spike from the evening before: both portal articles are found.
    assert {"Thomas Markle", "Pedro Alliana"} <= set(report["per_day"][0]["found_links"])
    assert metrics["ttd_median_min"] == 7


def test_evaluating_twice_gives_the_same_report() -> None:
    portal = daily_eval.fetch_ground_truth(DAY, None, "https://example.invalid", NOW.date())
    spikes = [spike("Thomas Markle", at(6, 18)), spike("Some Film", at(6, 12))]
    config = detection.config_snapshot(Settings(_env_file=None), {})
    git = {"commit": "abc", "dirty": False}
    first = daily_eval.evaluate_day(DAY, "live", spikes, portal, [], config, git, NOW)
    again = daily_eval.evaluate_day(DAY, "live", spikes, portal, [], config, git, NOW)
    assert first == again


def test_report_path_is_per_run_and_day(tmp_path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    path = daily_eval.report_path(settings, "live", DAY)
    assert path == tmp_path / "daily_eval" / "live" / "2026-11-06.md"
