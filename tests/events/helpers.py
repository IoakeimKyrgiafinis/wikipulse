"""Hand-made spikes for the grouping tests."""

from datetime import UTC, datetime, timedelta

import numpy as np

from wikipulse.events.similarity import SpikePage

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


# Three directions: two stories and something unrelated.
ELECTION = unit(1, 0, 0)
FOOTBALL = unit(0, 1, 0)
ELECTION_ISH = unit(0.9, 0.1, 0)  # cosine 0.99 with ELECTION


def page(
    spike_id: int,
    title: str,
    minute: float = 0,
    *,
    links: tuple[str, ...] = (),
    embedding: np.ndarray | None = None,
    ended_minute: float | None = None,
    score: float = 5.0,
    canonical: str | None = None,
) -> SpikePage:
    """A spike starting `minute` minutes after T0 (ended at `ended_minute`, if given)."""
    return SpikePage(
        spike_id=spike_id,
        title=title,
        canonical_title=canonical or title,
        started_at=T0 + timedelta(minutes=minute),
        ended_at=None if ended_minute is None else T0 + timedelta(minutes=ended_minute),
        score=score,
        links=frozenset(links),
        embedding=embedding,
    )


def at(minute: float) -> datetime:
    return T0 + timedelta(minutes=minute)
