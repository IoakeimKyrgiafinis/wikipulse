"""Serving: loading the champion, scoring spikes at their cutoff, the live edit buffer.

A local registry (SQLite) holds a logistic regression fitted on the synthetic data;
edits and pages are synthetic; Postgres is replaced by small fakes. No network.
"""

import json
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
from fakeredis import FakeRedis
from mlflow import MlflowClient

import wikipulse.ml.serve as serve
from tests.ml.registry_helpers import fitted_logreg, local_tracking, register
from tests.ml.test_features import START, synthetic_spike
from wikipulse.db.scores import SpikeToScore
from wikipulse.ml.features import (
    FEATURE_NAMES,
    PageSnapshot,
    SpikeWindow,
    compute_features,
)
from wikipulse.ml.models import INPUT_COLUMNS
from wikipulse.ml.registry import promote
from wikipulse.ml.serve import (
    EditBuffer,
    LiveScorer,
    LoadedModel,
    ModelHolder,
    load_alias,
    page_edit,
    score_spikes,
    stream_start_id,
)
from wikipulse.stream.models import EditEvent

NAME = "serve-model"
TITLE = "2026 Example election"
FIRST = START - timedelta(minutes=8)
CUTOFF = START + timedelta(minutes=10)
SPIKE = SpikeToScore(1, "live", TITLE, FIRST, START)


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    """Version 1: the rule; version 2: logistic regression (threshold 0.4), champion."""
    with local_tracking(tmp_path_factory.mktemp("serve")):
        register(NAME)
        version = register(NAME, fitted_logreg(), threshold=0.4, tags={"model_type": "logreg"})
        promote(MlflowClient(), NAME, version, "test")
        yield version


class FakePages:
    """Page revisions without the MediaWiki API: one snapshot per revision asked for."""

    def __init__(self) -> None:
        self.asked: list[set[int]] = []

    def snapshots(self, revids):
        self.asked.append(set(revids))
        return {
            r: PageSnapshot(r, START, ("2026 elections", "Living people"), ("current",), 12)
            for r in revids
        }

    def leads(self, revids):
        raise AssertionError("a model without text must not ask for leads")


# --- loading ----------------------------------------------------------------------------


def test_the_champion_loads_by_alias(registry):
    model = load_alias(NAME)
    assert model.version == registry
    assert model.threshold == pytest.approx(0.4)
    assert model.needs_text is False and model.model_type == "logreg"
    frame = pd.DataFrame([{c: 0.0 for c in FEATURE_NAMES}]).assign(
        default_rule_fired=True, summary_text="x"
    )
    out = model.predict(frame[INPUT_COLUMNS])
    assert list(out.columns) == ["score", "is_news"] and 0 <= out["score"][0] <= 1


def test_a_missing_alias_says_how_to_set_one(registry):
    with pytest.raises(LookupError, match="registry promote"):
        load_alias(NAME, "no-such-alias")


def test_the_holder_reloads_when_the_alias_moves():
    versions = iter(["3", "3", "4"])
    now = [0.0]
    loaded = []

    def load(name, version):
        loaded.append(version)
        return LoadedModel(name, version, None, 0.5, False)

    holder = ModelHolder(
        "m", reload_s=60, resolve=lambda n, a: next(versions), load=load, clock=lambda: now[0]
    )
    assert holder.current().version == "3"
    now[0] = 30
    assert holder.current().version == "3"  # not checked again yet
    now[0] = 61
    assert holder.current().version == "3"  # checked: unchanged, not reloaded
    now[0] = 122
    assert holder.current().version == "4"  # moved: reloaded
    assert loaded == ["3", "4"]


def test_the_holder_keeps_serving_when_the_registry_is_down():
    calls = {"n": 0}

    def resolve(name, alias):
        calls["n"] += 1
        if calls["n"] > 1:
            raise ConnectionError("registry down")
        return "1"

    holder = ModelHolder(
        "m",
        reload_s=0,
        resolve=resolve,
        load=lambda n, v: LoadedModel(n, v, None, 0.5, False),
    )
    assert holder.current().version == "1"
    assert holder.current().version == "1"


# --- scoring ----------------------------------------------------------------------------


def test_a_spike_is_scored_with_the_features_it_was_trained_on(registry):
    model = load_alias(NAME)
    edits = synthetic_spike()
    pages = FakePages()
    scores = score_spikes(model, [SPIKE], lambda t: edits, pages, 10, START - timedelta(days=2))
    [score] = scores
    revision = max((e for e in edits if e.time <= CUTOFF), key=lambda e: e.time).rev_new
    assert pages.asked == [{revision}]  # the revision current at the cutoff
    window = SpikeWindow(TITLE, FIRST, START, 10)
    expected = compute_features(window, edits, pages.snapshots({revision})[revision])
    assert score.features.keys() == expected.keys()
    assert all(
        (np.isnan(a) and np.isnan(b)) or a == b
        for a, b in zip(score.features.values(), expected.values(), strict=True)
    )
    frame = pd.DataFrame([expected])[FEATURE_NAMES].to_numpy()
    direct = model.model.unwrap_python_model().scorer.estimator.predict_proba(frame)[0, 1]
    assert score.score == pytest.approx(direct)
    assert score.is_news == (score.score >= 0.4)
    assert (score.model_name, score.model_version) == (NAME, registry)
    assert score.cutoff == CUTOFF and score.page_revision_id == revision
    assert score.history_complete is True


def test_edits_after_the_cutoff_change_nothing(registry):
    model = load_alias(NAME)
    edits = synthetic_spike()
    early = [e for e in edits if e.time <= CUTOFF]
    a = score_spikes(model, [SPIKE], lambda t: edits, FakePages(), 10, None)[0]
    b = score_spikes(model, [SPIKE], lambda t: early, FakePages(), 10, None)[0]
    assert a.score == b.score
    assert a.history_complete is False  # no history start given: unknown, not complete


def test_history_is_incomplete_when_the_source_starts_inside_the_24_hours(registry):
    model = load_alias(NAME)
    edits = synthetic_spike()
    late_start = FIRST - timedelta(hours=3)
    [score] = score_spikes(model, [SPIKE], lambda t: edits, FakePages(), 10, late_start)
    assert score.history_complete is False


def test_a_page_without_edits_or_revision_still_gets_a_score(registry):
    model = load_alias(NAME)
    pages = FakePages()
    [score] = score_spikes(model, [SPIKE], lambda t: [], pages, 10, None)
    assert pages.asked == []  # nothing to look up
    assert score.page_revision_id is None and 0 <= score.score <= 1
    assert np.isnan(score.features["page_age_days"])


# --- the live edit buffer -----------------------------------------------------------------


def event(minutes: float, title: str = TITLE, user: str = "Alice", rev: int = 1) -> EditEvent:
    return EditEvent(
        event_id=f"{title}-{minutes}-{user}",
        event_time=START + timedelta(minutes=minutes),
        received_at=START + timedelta(minutes=minutes, seconds=1),
        title=title,
        type="edit",
        user=user,
        is_bot=False,
        is_minor=False,
        rev_old=rev - 1,
        rev_new=rev,
        len_old=100,
        len_new=120,
        comment="update",
    )


def test_the_buffer_keeps_hours_of_edits_and_knows_where_it_starts():
    buffer = EditBuffer(keep=timedelta(hours=2))
    for minutes in (-300, -100, -10, 0, 5):
        buffer.add(event(minutes))
    buffer.add(event(-5, title="Other page"))
    assert buffer.start == START - timedelta(minutes=300)
    assert buffer.watermark == START + timedelta(minutes=5)
    assert [e.time for e in buffer.edits(TITLE)][-1] == START + timedelta(minutes=5)
    assert buffer.prune() == 1  # the edit 300 minutes back is older than 2 hours
    assert buffer.start == START + timedelta(minutes=5) - timedelta(hours=2)
    assert buffer.size() == 5 and buffer.edits("missing") == []


def test_page_edit_keeps_what_the_features_need():
    e = event(1, rev=77)
    pe = page_edit(e)
    assert (pe.time, pe.user, pe.rev_new, pe.len_new, pe.is_new) == (
        e.event_time,
        "Alice",
        77,
        120,
        False,
    )


def test_reading_starts_the_buffer_hours_before_the_newest_entry():
    client = FakeRedis(decode_responses=True)
    assert stream_start_id(client, "s", timedelta(hours=1)) == "0-0"
    client.xadd("s", {"data": "{}"}, id="7200000-0")
    assert stream_start_id(client, "s", timedelta(hours=1)) == "3600000-0"


def write_stream(client, events) -> None:
    for i, e in enumerate(sorted(events, key=lambda e: e.event_time)):
        millis = int(e.event_time.timestamp() * 1000)
        client.xadd("edits", {"data": e.model_dump_json()}, id=f"{millis}-{i}")


def test_the_live_scorer_scores_a_spike_once_the_stream_passes_its_cutoff(monkeypatch):
    client = FakeRedis(decode_responses=True)
    burst = [event(m, user=f"U{m}", rev=100 + i) for i, m in enumerate((-8, -6, -3, 0, 2, 4))]
    write_stream(client, burst)
    asked, stored = [], []

    def fake_spikes(conn, run_id, window_min, cutoff_until, first_edit_from=None, **kw):
        asked.append((run_id, cutoff_until, first_edit_from))
        return [SPIKE] if START + timedelta(minutes=window_min) <= cutoff_until else []

    monkeypatch.setattr(serve, "spikes_to_score", fake_spikes)
    monkeypatch.setattr(serve, "upsert_scores", lambda conn, scores: stored.extend(scores))
    model = LoadedModel(NAME, "7", FixedModel(0.9), 0.5, False)
    scorer = LiveScorer(
        client,
        None,
        "edits",
        "live",
        ModelHolder(NAME, resolve=lambda n, a: "7", load=lambda n, v: model),
        FakePages(),
        window_min=10,
        lateness=timedelta(minutes=2),
        keep=timedelta(hours=26),
    )
    scorer.catch_up()
    assert scorer.score_due() == 0  # the stream is at +4 min, the cutoff at +10
    write_stream(client, [event(13, user="Late", rev=200)])  # +13: past cutoff + lateness
    scorer.read()
    assert scorer.score_due() == 1
    assert asked[-1] == ("live", START + timedelta(minutes=11), START - timedelta(minutes=8))
    [score] = stored
    assert score.score == 0.9 and score.is_news and score.model_version == "7"
    assert score.features["edits"] == 6  # the burst up to the cutoff, not the edit at +13
    assert score.history_complete is False  # the stream began with the burst


class FixedModel:
    """A stand-in pyfunc model: the same score for every row."""

    def __init__(self, score: float) -> None:
        self.score = score

    def predict(self, frame):
        assert list(frame.columns) == INPUT_COLUMNS
        assert frame[FEATURE_NAMES].dtypes.eq("float64").all()
        return pd.DataFrame(
            {"score": [self.score] * len(frame), "is_news": [self.score >= 0.5] * len(frame)}
        )


def test_json_features_store_unknown_as_null():
    from wikipulse.db.scores import json_features

    assert json.dumps(json_features({"a": 1.0, "b": float("nan")})) == '{"a": 1.0, "b": null}'
