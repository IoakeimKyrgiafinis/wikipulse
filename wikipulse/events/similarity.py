"""How likely two spiking pages belong to the same story: one score in [0, 1].

Three signals, each in [0, 1], combined as a weighted average (weights in
`EventsConfig`, set with `EVENTS_*_WEIGHT`):

- **link**: one page links to the other. A link from a page with more than
  `link_hub_size` links counts `link_hub_size / links` instead of 1. And a link counts
  only if the two summaries are at least somewhat alike (cosine >= `link_min_cosine`):
  "Deaths in 2026" links to every recent death, and a conductor's and a politician's
  deaths are different stories, so link plus timing alone must not join (and chain)
  them.
- **time**: the spikes started close together. 1 for the same moment, falling linearly
  to 0 at `time_window_h` hours apart.
- **embed**: the page summaries mean similar things: the cosine similarity of their
  embeddings, rescaled so that `embed_floor` (unrelated summaries) gives 0 and 1 gives 1.

Two spikes of the same page (same canonical title) always score 1.
"""

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from wikipulse.config import EventsConfig

# Cosine similarities are rounded to this many decimals, so tiny floating-point
# differences (e.g. from batching the embedding model) can't flip a decision.
COSINE_DECIMALS = 4


@dataclass(eq=False)
class SpikePage:
    """One spike plus what grouping needs to know about its page."""

    spike_id: int
    title: str  # the spike's title
    canonical_title: str  # after redirects (the spike's title if not enriched)
    started_at: datetime
    ended_at: datetime | None  # None while the spike is active
    score: float  # the spike's detector score
    links: frozenset[str] = frozenset()
    embedding: np.ndarray | None = field(default=None, repr=False)  # unit length
    correlation_id: str | None = None  # the edit that started the spike (spec 09b)

    @property
    def names(self) -> frozenset[str]:
        """The titles other pages may use to link to this one."""
        return frozenset({self.title, self.canonical_title})


@dataclass(frozen=True)
class PairScore:
    """The three signals and their weighted average (`total`)."""

    link: float
    time: float
    embed: float
    total: float


def link_strength(source: SpikePage, target: SpikePage, hub_size: int) -> float:
    """How much `source` linking to `target` counts: 0, 1, or less for a hub page."""
    if source.links.isdisjoint(target.names):
        return 0.0
    if hub_size <= 0 or len(source.links) <= hub_size:
        return 1.0
    return hub_size / len(source.links)


def link_signal(a: SpikePage, b: SpikePage, config: EventsConfig) -> float:
    """The stronger link direction; 0 if the summaries look unrelated (see module doc)."""
    if not content_allows_link(a, b, config):
        return 0.0
    return max(link_strength(a, b, config.link_hub_size), link_strength(b, a, config.link_hub_size))


def content_allows_link(a: SpikePage, b: SpikePage, config: EventsConfig) -> bool:
    """False when both summaries are known and their cosine is below `link_min_cosine`.

    Without an embedding the content can't be judged, so the link counts as before.
    """
    if a.embedding is None or b.embedding is None:
        return True
    return cosine(a.embedding, b.embedding) >= config.link_min_cosine


def time_signal(a: SpikePage, b: SpikePage, config: EventsConfig) -> float:
    hours = abs((a.started_at - b.started_at).total_seconds()) / 3600
    return max(0.0, 1.0 - hours / config.time_window_h)


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Cosine similarity of two unit vectors (0 if either is missing)."""
    if a is None or b is None:
        return 0.0
    return round(float(np.dot(a, b)), COSINE_DECIMALS)


def embed_signal(a: SpikePage, b: SpikePage, config: EventsConfig) -> float:
    similarity = cosine(a.embedding, b.embedding)
    return min(1.0, max(0.0, (similarity - config.embed_floor) / (1.0 - config.embed_floor)))


def pair_score(a: SpikePage, b: SpikePage, config: EventsConfig) -> PairScore:
    """The combined score of two spikes; see the module docstring."""
    if a.canonical_title == b.canonical_title:
        return PairScore(1.0, 1.0, 1.0, 1.0)
    link = link_signal(a, b, config)
    time = time_signal(a, b, config)
    embed = embed_signal(a, b, config)
    weights = config.link_weight + config.time_weight + config.embed_weight
    if weights == 0:
        return PairScore(link, time, embed, 0.0)
    total = (
        config.link_weight * link + config.time_weight * time + config.embed_weight * embed
    ) / weights
    return PairScore(link, time, embed, round(total, 6))
