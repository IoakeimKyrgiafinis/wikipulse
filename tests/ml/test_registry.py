"""Aliases, promotion and rollback in a local registry (no server)."""

import mlflow
import pytest
from mlflow import MlflowClient

from tests.ml.registry_helpers import local_tracking, register
from wikipulse.ml.registry import (
    CHALLENGER,
    CHAMPION,
    RegistryError,
    alias_version,
    describe,
    promote,
    rollback,
    rollback_target,
)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    with local_tracking(tmp_path_factory.mktemp("registry")):
        yield MlflowClient()


def test_promotion_moves_the_champion_and_remembers_the_previous_one(client):
    name = "reg-promote"
    v1, v2 = register(name), register(name)
    first = promote(client, name, v1, "first choice")
    assert (first.before, first.after) == (None, v1)
    assert alias_version(client, name, CHAMPION) == v1

    client.set_registered_model_alias(name, CHALLENGER, v2)
    second = promote(client, name, v2, "better on the test set")
    assert (second.before, second.after) == (v1, v2)
    assert alias_version(client, name, CHAMPION) == v2
    assert alias_version(client, name, CHALLENGER) is None  # it is the champion now
    new, old = client.get_model_version(name, v2).tags, client.get_model_version(name, v1).tags
    assert new["status"] == "champion" and new["previous_champion"] == v1
    assert new["promotion_reason"] == "better on the test set"
    assert old["status"] == "retired" and old["retired_by"] == v2


def test_promoting_the_champion_again_changes_nothing(client):
    name = "reg-again"
    v1 = register(name)
    promote(client, name, v1, "x")
    change = promote(client, name, v1, "y")
    assert change.before == change.after == v1
    assert "previous_champion" not in client.get_model_version(name, v1).tags


def test_rollback_returns_to_the_previous_champion_and_forward_again(client):
    name = "reg-rollback"
    v1, v2 = register(name), register(name)
    promote(client, name, v1, "first")
    promote(client, name, v2, "second")
    back = rollback(client, name)
    assert (back.before, back.after) == (v2, v1)
    assert alias_version(client, name, CHAMPION) == v1
    assert client.get_model_version(name, v2).tags["status"] == "rolled_back"
    assert client.get_model_version(name, v1).tags["status"] == "champion"
    forward = promote(client, name, v2, "the problem is fixed")  # roll forward by hand
    assert (forward.before, forward.after) == (v1, v2)
    tags = client.get_model_version(name, v2).tags
    assert tags["status"] == "champion" and "rolled_back_to" not in tags  # no stale tag
    assert client.get_model_version(name, v1).tags["retired_by"] == v2


def test_rollback_without_a_previous_champion_takes_the_newest_usable_older_version(client):
    name = "reg-fallback"
    v1 = register(name)
    v2 = register(name, tags={"status": "rejected"})
    v3 = register(name)
    promote(client, name, v3, "set by hand")  # no previous champion recorded
    assert rollback_target(client, name, v3) == v1  # v2 lost a comparison: skipped
    assert rollback(client, name).after == v1
    assert alias_version(client, name, CHAMPION) == v1
    assert client.get_model_version(name, v2).tags["status"] == "rejected"


def test_rollback_needs_a_champion_and_an_older_version(client):
    name = "reg-nothing"
    v1 = register(name)
    with pytest.raises(RegistryError, match="no champion"):
        rollback(client, name)
    promote(client, name, v1, "only one")
    with pytest.raises(RegistryError, match="no earlier version"):
        rollback(client, name)


def test_describe_lists_versions_with_aliases(client):
    name = "reg-describe"
    v1, v2 = register(name), register(name)
    promote(client, name, v1, "x")
    client.set_registered_model_alias(name, CHALLENGER, v2)
    text = describe(client, name)
    assert f"v{v2}" in text and "@challenger" in text and "@champion" in text
    assert mlflow.get_tracking_uri().startswith("sqlite:")


def test_an_unreachable_registry_is_an_error_not_a_missing_champion():
    from types import SimpleNamespace

    from mlflow.exceptions import MlflowException

    def refuse(name, alias):
        raise MlflowException("Invalid Host header", error_code="BAD_REQUEST")

    with pytest.raises(MlflowException):
        alias_version(SimpleNamespace(get_model_version_by_alias=refuse), "m", CHAMPION)
