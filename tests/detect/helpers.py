"""Shared helpers for detection tests: hand-made edits and the recorded fixture."""

from datetime import UTC, datetime, timedelta
from typing import Any

from tests.stream.helpers import fixture_raws
from wikipulse.config import DetectorConfig
from wikipulse.stream.filters import is_wanted, normalize
from wikipulse.stream.models import EditEvent

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
T0_S = T0.timestamp()
RECEIVED = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)  # fixed, so events are reproducible

_counter = 0


def make_edit(
    title: str,
    minute: float,
    user: str = "Alice",
    is_bot: bool = False,
    comment: str = "copyedit",
    start: datetime = T0,
) -> EditEvent:
    """An edit of `title` by `user`, `minute` minutes after `start`."""
    global _counter
    _counter += 1
    return EditEvent(
        event_id=f"test-{_counter}",
        event_time=start + timedelta(minutes=minute),
        received_at=RECEIVED,
        title=title,
        type="edit",
        user=user,
        is_bot=is_bot,
        is_minor=False,
        rev_old=1,
        rev_new=2,
        len_old=100,
        len_new=110,
        comment=comment,
    )


def burst(
    title: str, start_minute: float, edits: int, editors: int, every_min: float = 1.0
) -> list[EditEvent]:
    """`edits` edits, one every `every_min` minutes, by `editors` users in turn."""
    return [
        make_edit(title, start_minute + i * every_min, user=f"User{i % editors}")
        for i in range(edits)
    ]


def config(**overrides: Any) -> DetectorConfig:
    """The default detector config with some values changed."""
    return DetectorConfig(**overrides)


def fixture_events() -> list[EditEvent]:
    """The kept edits of the recorded fixture, normalized as stage 1 does."""
    return [normalize(raw, received_at=RECEIVED) for raw in fixture_raws() if is_wanted(raw)]
