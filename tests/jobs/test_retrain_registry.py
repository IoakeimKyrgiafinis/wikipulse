"""The retraining steps that change the registry (train's reuse, evaluate, promote), in a
throwaway local registry: the stale-champion guard, the reject path, and that a step run
again after a promotion never touches the champion."""

from datetime import date

import pytest
from mlflow import MlflowClient

from tests.ml.registry_helpers import local_tracking, register
from wikipulse.config import Settings
from wikipulse.jobs.retrain import (
    PROMOTE,
    REJECT,
    SKIPPED,
    retrain_key,
    step_evaluate,
    step_promote,
    step_train,
)
from wikipulse.ml.registry import CHALLENGER, CHAMPION, alias_version, past_challenger, promote

WEEK = date(2026, 11, 2)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with local_tracking(tmp_path_factory.mktemp("retrain-registry")):
        yield MlflowClient()


def settings(name: str) -> Settings:
    return Settings(_env_file=None, ml_model_name=name)


def decided(client, name: str, version: str, decision: str, champion: str) -> None:
    """Tag `version` as the evaluate step does."""
    for tag, value in {
        "decision": decision,
        "decision_reason": f"{decision} for the test",
        "champion_at_evaluation": champion,
    }.items():
        client.set_model_version_tag(name, version, tag, value)


def last_line(capsys) -> str:
    return capsys.readouterr().out.strip().splitlines()[-1]


def test_promote_moves_the_champion_when_the_decision_says_so(client, capsys):
    name = "rt-promote"
    v1, v2 = register(name), register(name)
    promote(client, name, v1, "first")
    decided(client, name, v2, PROMOTE, champion=v1)
    assert step_promote(settings(name), v2) == 0
    assert last_line(capsys) == PROMOTE
    assert alias_version(client, name, CHAMPION) == v2
    assert client.get_model_version(name, v1).tags["status"] == "retired"


def test_promote_refuses_when_the_champion_changed_since_the_evaluation(client):
    name = "rt-stale"
    v1, v2, v3 = register(name), register(name), register(name)
    promote(client, name, v1, "first")
    decided(client, name, v3, PROMOTE, champion=v1)
    promote(client, name, v2, "by hand, after the evaluation")
    assert step_promote(settings(name), v3) == 1
    assert alias_version(client, name, CHAMPION) == v2
    assert client.get_model_version(name, v3).tags.get("status") != "champion"


def test_a_rejected_challenger_is_tagged_and_the_champion_stays(client, capsys):
    name = "rt-reject"
    v1, v2 = register(name), register(name)
    promote(client, name, v1, "first")
    decided(client, name, v2, REJECT, champion=v1)
    assert step_promote(settings(name), v2) == 0
    assert last_line(capsys) == REJECT
    tags = client.get_model_version(name, v2).tags
    assert tags["status"] == "rejected" and tags["rejected_reason"] == "reject for the test"
    assert alias_version(client, name, CHAMPION) == v1


def test_promote_needs_a_decision(client):
    name = "rt-undecided"
    v1 = register(name)
    assert step_promote(settings(name), v1) == 1
    assert alias_version(client, name, CHAMPION) is None


def test_promote_run_again_after_the_promotion_leaves_the_champion_alone(client, capsys):
    name = "rt-promote-twice"
    v1, v2 = register(name), register(name)
    promote(client, name, v1, "first")
    decided(client, name, v2, PROMOTE, champion=v1)
    assert step_promote(settings(name), v2) == 0
    before = client.get_model_version(name, v2).tags
    assert step_promote(settings(name), v2) == 0
    assert last_line(capsys) == SKIPPED
    assert client.get_model_version(name, v2).tags == before
    assert alias_version(client, name, CHAMPION) == v2


def test_the_champion_is_never_tagged_rejected(client, capsys):
    name = "rt-never-rejected"
    v1, v2 = register(name), register(name)
    promote(client, name, v1, "first")
    decided(client, name, v2, REJECT, champion=v1)
    promote(client, name, v2, "promoted by hand after all")
    assert step_promote(settings(name), v2) == 0
    assert last_line(capsys) == SKIPPED
    tags = client.get_model_version(name, v2).tags
    assert tags["status"] == "champion" and "rejected_reason" not in tags


def test_training_again_after_the_promotion_never_reuses_the_champion(client, capsys):
    name = "rt-train-again"
    key = retrain_key(WEEK)
    v1 = register(name)
    v2 = register(name, tags={"retrain_key": key, "dataset_version": "v3"})
    promote(client, name, v1, "first")
    promote(client, name, v2, "this week's challenger won")
    assert step_train(settings(name), WEEK, "v3") == 0  # finds v2: no training
    assert last_line(capsys) == v2
    assert alias_version(client, name, CHALLENGER) is None
    assert client.get_model_version(name, v2).tags["status"] == "champion"
    # ... and evaluate and promote, run again with it, change nothing either.
    before = client.get_model_version(name, v2).tags
    assert step_evaluate(settings(name), WEEK, "v3", v2) == 0
    assert last_line(capsys) == SKIPPED
    assert step_promote(settings(name), v2) == 0
    assert client.get_model_version(name, v2).tags == before
    assert alias_version(client, name, CHAMPION) == v2


def test_a_retried_training_makes_its_earlier_version_the_challenger_again(client, capsys):
    name = "rt-train-retry"
    key = retrain_key(WEEK)
    v1 = register(name)
    v2 = register(name, tags={"retrain_key": key, "dataset_version": "v3"})
    promote(client, name, v1, "first")
    assert step_train(settings(name), WEEK, "v3") == 0
    assert last_line(capsys) == v2
    assert alias_version(client, name, CHALLENGER) == v2
    assert client.get_model_version(name, v2).tags["status"] == "challenger"


def test_a_version_that_served_is_past_the_challenger_stage(client):
    name = "rt-past"
    v1, v2, v3 = register(name), register(name), register(name)
    assert past_challenger(client, name, v1) is None
    promote(client, name, v1, "first")
    promote(client, name, v2, "second")
    assert "champion" in past_challenger(client, name, v2)
    assert "retired" in past_challenger(client, name, v1)
    assert past_challenger(client, name, v3) is None
