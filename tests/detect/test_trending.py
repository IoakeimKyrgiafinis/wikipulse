"""The trending table."""

from datetime import UTC, datetime, timedelta

from wikipulse.detect.detector import SpikeRecord
from wikipulse.detect.trending import render

T = datetime(2026, 11, 7, 13, 0, tzinfo=UTC)


def spike(title: str, status: str = "active") -> SpikeRecord:
    return SpikeRecord(
        title=title,
        status=status,
        started_at=T,
        first_edit_at=T - timedelta(minutes=5),
        last_edit_at=T + timedelta(minutes=20),
        ended_at=T + timedelta(minutes=20) if status == "ended" else None,
        total_edits=12,
        distinct_editors=3,
        revert_edits=1,
        peak_edits_10m=8,
        score=10.6,
    )


def test_render_lists_spikes_with_duration_and_status() -> None:
    text = render(
        [spike("Boban Marjanović"), spike("Old", "ended")], "live", T + timedelta(hours=1)
    )
    lines = text.splitlines()
    assert "detector time 2026-11-07 14:00:00 UTC" in lines[0]
    assert lines[2].endswith("1h05m  Boban Marjanović")
    assert lines[3].endswith("0h25m  Old  (ended)")


def test_render_without_spikes() -> None:
    assert "(no active spikes)" in render([], "live", None)
