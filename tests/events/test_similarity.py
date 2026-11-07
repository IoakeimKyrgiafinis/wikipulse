"""The pairwise score: links, time, embeddings and their weighted average."""

import pytest

from tests.events.helpers import ELECTION, ELECTION_ISH, FOOTBALL, page, unit
from wikipulse.config import EventsConfig
from wikipulse.events.similarity import cosine, pair_score

CONFIG = EventsConfig()  # weights 0.4 link, 0.2 time, 0.4 embed; floor 0.5; window 6 h


def test_a_link_and_the_same_start_without_similar_summaries() -> None:
    a = page(1, "2026 Ohio gubernatorial election", links=("Vivek Ramaswamy",), embedding=ELECTION)
    b = page(2, "Vivek Ramaswamy", embedding=FOOTBALL)
    score = pair_score(a, b, CONFIG)
    assert (score.link, score.time, score.embed) == (1.0, 1.0, 0.0)
    assert score.total == pytest.approx(0.6)


def test_links_count_in_either_direction_and_through_redirect_names() -> None:
    a = page(1, "Susan Wiles", canonical="Susie Wiles")
    b = page(2, "White House Chief of Staff", links=("Susie Wiles",))
    assert pair_score(a, b, CONFIG).link == 1.0
    assert pair_score(b, a, CONFIG).link == 1.0


def test_a_link_from_a_hub_page_counts_less() -> None:
    hub_links = tuple(f"Person {i}" for i in range(2000)) + ("Herbert Blomstedt",)
    hub = page(1, "Deaths in 2026", links=hub_links)
    person = page(2, "Herbert Blomstedt")
    assert pair_score(hub, person, CONFIG).link == pytest.approx(1000 / 2001)
    no_guard = EventsConfig(link_hub_size=0)
    assert pair_score(hub, person, no_guard).link == 1.0


def test_time_signal_falls_linearly_to_zero_at_the_window() -> None:
    a = page(1, "A")
    assert pair_score(a, page(2, "B", minute=180), CONFIG).time == pytest.approx(0.5)
    assert pair_score(a, page(2, "B", minute=-180), CONFIG).time == pytest.approx(0.5)
    assert pair_score(a, page(2, "B", minute=360), CONFIG).time == 0.0
    assert pair_score(a, page(2, "B", minute=2000), CONFIG).time == 0.0


def test_embedding_signal_is_rescaled_above_the_floor() -> None:
    a = page(1, "A", embedding=unit(1, 0))
    b = page(2, "B", embedding=unit(0.75, (1 - 0.75**2) ** 0.5))  # cosine 0.75
    assert cosine(a.embedding, b.embedding) == pytest.approx(0.75)
    assert pair_score(a, b, CONFIG).embed == pytest.approx(0.5, abs=1e-3)
    below = page(3, "C", embedding=unit(0.3, (1 - 0.09) ** 0.5))  # cosine 0.3 < floor
    assert pair_score(a, below, CONFIG).embed == 0.0
    assert pair_score(a, page(4, "D", embedding=unit(1, 0)), CONFIG).embed == 1.0


def test_a_missing_embedding_gives_zero() -> None:
    assert pair_score(page(1, "A"), page(2, "B", embedding=ELECTION), CONFIG).embed == 0.0


def test_same_page_scores_one() -> None:
    a = page(1, "Ueno bank", canonical="Ueno Bank", minute=0)
    b = page(2, "Ueno Bank", minute=600)  # far apart, no links, no embeddings
    assert pair_score(a, b, CONFIG).total == 1.0


def test_score_is_symmetric_and_weights_are_normalized() -> None:
    a = page(1, "A", minute=0, links=("B",), embedding=ELECTION)
    b = page(2, "B", minute=90, embedding=ELECTION_ISH)
    assert pair_score(a, b, CONFIG) == pair_score(b, a, CONFIG)
    doubled = EventsConfig(link_weight=0.8, time_weight=0.4, embed_weight=0.8)
    assert pair_score(a, b, doubled).total == pytest.approx(pair_score(a, b, CONFIG).total)


def test_weights_choose_what_matters() -> None:
    a = page(1, "A", links=("B",), embedding=ELECTION)
    b = page(2, "B", embedding=FOOTBALL)
    only_embed = EventsConfig(link_weight=0, time_weight=0, embed_weight=1)
    assert pair_score(a, b, only_embed).total == 0.0
    only_links = EventsConfig(link_weight=1, time_weight=0, embed_weight=0)
    assert pair_score(a, b, only_links).total == 1.0
    nothing = EventsConfig(link_weight=0, time_weight=0, embed_weight=0)
    assert pair_score(a, b, nothing).total == 0.0
