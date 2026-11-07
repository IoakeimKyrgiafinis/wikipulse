"""Hand-made spikes and portal days for evaluation tests."""

from datetime import UTC, date, datetime, timedelta

from wikipulse.detect.detector import SpikeRecord
from wikipulse.eval.ground_truth import PortalDay


def at(day: int, hour: float = 0) -> datetime:
    """A moment in November 2026 (UTC)."""
    return datetime(2026, 11, day, tzinfo=UTC) + timedelta(hours=hour)


def spike(
    title: str,
    start: datetime,
    delay_min: float = 5,
    duration_h: float = 1,
    status: str = "ended",
) -> SpikeRecord:
    """A spike whose burst began `delay_min` minutes before it was detected at `start`."""
    first = start - timedelta(minutes=delay_min)
    end = start + timedelta(hours=duration_h)
    return SpikeRecord(
        title=title,
        status=status,
        started_at=start,
        first_edit_at=first,
        last_edit_at=end,
        ended_at=end if status == "ended" else None,
        total_edits=8,
        distinct_editors=4,
        revert_edits=0,
        peak_edits_10m=5,
        score=6.0,
    )


def portal_day(
    day: int, links: list[str], leads: list[str], resolve: dict[str, str] | None = None
) -> PortalDay:
    return PortalDay(date(2026, 11, day), links, leads, resolve or {})
