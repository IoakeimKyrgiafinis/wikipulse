"""scripts/cluster.py (specs 08a and 08b), without a cluster: no command really runs.

The chart it deploys is tested in tests/test_chart.py.
"""

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from wikipulse.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cluster.py"
spec = importlib.util.spec_from_file_location("cluster", SCRIPT)
assert spec is not None and spec.loader is not None
cluster = importlib.util.module_from_spec(spec)
sys.modules["cluster"] = cluster
spec.loader.exec_module(cluster)


# --- scripts/cluster.py ---


def test_image_tags() -> None:
    now = datetime(2026, 11, 10, 14, 3, 5, tzinfo=UTC)
    assert cluster.image_tag("3f2a1b9", False, now) == "3f2a1b9"
    assert cluster.image_tag("3f2a1b9", True, now) == "3f2a1b9-dirty-20261110140305"


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "postgresql://wikipulse:pw@localhost:5432/wikipulse",
            "postgresql://wikipulse:pw@postgres:5432/wikipulse",
        ),
        ("postgresql://u:p@127.0.0.1/db", "postgresql://u:p@postgres/db"),
        (
            "postgresql://localhost:5433/db?sslmode=disable",
            "postgresql://postgres:5433/db?sslmode=disable",
        ),
    ],
)
def test_cluster_dsn_points_at_the_service(dsn: str, expected: str) -> None:
    assert cluster.cluster_dsn(dsn) == expected


def test_secret_manifest_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        postgres_dsn="postgresql://a:b@localhost:5432/c",
        neo4j_password=SecretStr("graph-pw"),
        user_agent="WikiPulse/0.1 (test)",
    )
    secret = cluster.secret_manifest(settings)
    assert secret["metadata"] == {
        "name": "wikipulse-secrets",
        "namespace": "wikipulse",
        "labels": {"app.kubernetes.io/part-of": "wikipulse"},
    }
    assert secret["stringData"]["POSTGRES_DSN"] == "postgresql://a:b@postgres:5432/c"
    assert secret["stringData"]["NEO4J_PASSWORD"] == "graph-pw"
    assert secret["stringData"]["USER_AGENT"] == "WikiPulse/0.1 (test)"


# A User-Agent with a contact on a reserved, never-real domain (RFC 2606).
REAL_LOOKING_UA = "WikiPulse/0.1 (https://github.com/someone/wikipulse; contact@wikipulse.invalid)"


@pytest.fixture
def kubectl_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the commands `cluster.run` would run, instead of running them."""
    calls: list[list[str]] = []
    monkeypatch.setattr(cluster, "run", lambda command, **_: calls.append(command) or "")
    return calls


@pytest.mark.parametrize(
    "user_agent",
    [Settings.model_fields["user_agent"].default, "WikiPulse/0.1 (x; you@example.com)"],
)
def test_secret_refuses_a_placeholder_contact(kubectl_calls, user_agent: str) -> None:
    settings = Settings(_env_file=None, user_agent=user_agent)
    with pytest.raises(SystemExit) as refused:
        cluster.secret(settings)
    assert ".env" in str(refused.value) and "USER_AGENT" in str(refused.value)
    assert kubectl_calls == []  # nothing was created or applied


def test_secret_with_a_real_contact_is_applied(kubectl_calls) -> None:
    cluster.secret(Settings(_env_file=None, user_agent=REAL_LOOKING_UA))
    assert kubectl_calls[-1][-3:] == ["apply", "-f", "-"]


def test_the_placeholder_contact_needs_the_explicit_opt_out(kubectl_calls) -> None:
    settings = Settings(_env_file=None, user_agent="WikiPulse/0.1 (x; you@example.com)")
    cluster.secret(settings, allow_placeholder=True)
    assert kubectl_calls[-1][-3:] == ["apply", "-f", "-"]


def test_up_refuses_a_placeholder_contact_before_doing_anything(
    kubectl_calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    placeholder = Settings(_env_file=None, user_agent="WikiPulse/0.1 (x; you@example.com)")
    monkeypatch.setattr(cluster, "get_settings", lambda: placeholder)
    with pytest.raises(SystemExit, match="placeholder"):
        cluster.main(["up"])
    assert kubectl_calls == []  # no cluster created, no image built


def test_deploy_refuses_a_placeholder_contact(kubectl_calls, monkeypatch) -> None:
    placeholder = Settings(_env_file=None, user_agent="WikiPulse/0.1 (x; you@example.com)")
    monkeypatch.setattr(cluster, "get_settings", lambda: placeholder)
    with pytest.raises(SystemExit, match="placeholder"):
        cluster.main(["deploy", "--tag", "t1"])
    assert kubectl_calls == []


def helm_upgrade(calls: list[list[str]]) -> list[str]:
    (command,) = [c for c in calls if c[0] == "helm" and "upgrade" in c]
    return command


def test_the_placeholder_opt_out_deploys_offline(kubectl_calls, monkeypatch) -> None:
    """With the placeholder contact, the services that call Wikimedia must not run."""
    placeholder = Settings(_env_file=None, user_agent="WikiPulse/0.1 (x; you@example.com)")
    monkeypatch.setattr(cluster, "get_settings", lambda: placeholder)
    cluster.main(["deploy", "--tag", "t1", "--allow-placeholder-contact"])
    command = helm_upgrade(kubectl_calls)
    assert str(cluster.CHART / "values-offline.yaml") in command


def test_deploy_with_a_real_contact_is_online_by_default(kubectl_calls, monkeypatch) -> None:
    real = Settings(_env_file=None, user_agent=REAL_LOOKING_UA)
    monkeypatch.setattr(cluster, "get_settings", lambda: real)
    cluster.main(["deploy", "--tag", "t1"])
    command = helm_upgrade(kubectl_calls)
    assert not any("values-offline" in arg for arg in command)
    assert str(cluster.CHART / "values-dev.yaml") in command
    # The namespace (with its Pod Security label) exists before Helm installs into it.
    assert kubectl_calls.index(command) > 0


def test_deploy_command() -> None:
    command = cluster.deploy_command(
        "abc123", ["values-prod.yaml"], ["api.replicas=3"], offline=True
    )
    assert command[:5] == ["helm", "--kube-context", "kind-wikipulse", "--namespace", "wikipulse"]
    assert command[5:9] == ["upgrade", "--install", "wikipulse", str(cluster.CHART)]
    files = [command[i + 1] for i, arg in enumerate(command) if arg == "--values"]
    # Order matters: later files win. The environment first, then the offline overlay.
    assert files == [
        str(cluster.CHART / "values-prod.yaml"),
        str(cluster.CHART / "values-offline.yaml"),
    ]
    sets = [command[i + 1] for i, arg in enumerate(command) if arg == "--set"]
    assert sets == ["image.tag=abc123", "api.replicas=3"]
    assert "--wait" in command


def test_values_files_outside_the_chart_are_used_as_given(tmp_path: Path) -> None:
    own = tmp_path / "my-values.yaml"
    own.write_text("api: {replicas: 2}\n", encoding="utf-8")
    assert cluster.values_file(str(own)) == own
    assert cluster.values_file("values-dev.yaml") == cluster.CHART / "values-dev.yaml"


def test_rollback_and_history_name_the_release(kubectl_calls) -> None:
    cluster.rollback(2)
    cluster.rollback()
    assert kubectl_calls[0][5:8] == ["rollback", "wikipulse", "2"]
    assert kubectl_calls[2][5:7] == ["rollback", "wikipulse"]
    assert kubectl_calls[2][7] == "--wait"
    assert kubectl_calls[1][5:] == ["history", "wikipulse"]


def test_the_namespace_enforces_the_restricted_pod_security_standard() -> None:
    labels = cluster.namespace_manifest()["metadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"


def test_the_secret_template_holds_only_placeholders() -> None:
    template = cluster.CHART / "secret.example.yaml"
    secret = yaml.safe_load(template.read_text(encoding="utf-8"))
    settings = Settings(_env_file=None)  # not the developer's .env
    assert set(secret["stringData"]) == set(cluster.secret_manifest(settings)["stringData"])
    assert all("<" in value for value in secret["stringData"].values())
    # Not packaged with the chart.
    assert "secret.example.yaml" in (cluster.CHART / ".helmignore").read_text(encoding="utf-8")


def test_a_session_pod_looks_like_the_charts_pods() -> None:
    pod = cluster.session_pod(
        "replay", "wikipulse:abc", ["python", "-m", "x"], {"A": "1"}, model_cache=True
    )
    labels = pod["metadata"]["labels"]
    assert labels["wikipulse.io/session"] == "true" and labels["app.kubernetes.io/name"] == "replay"
    spec = pod["spec"]
    assert spec["restartPolicy"] == "Never" and spec["securityContext"]["runAsNonRoot"]
    (container,) = spec["containers"]
    assert container["name"] == "replay" and container["stdin"] and container["stdinOnce"]
    assert {"name": "LOG_SERVICE", "value": "replay"} in container["env"]
    assert {"name": "A", "value": "1"} in container["env"]
    assert container["securityContext"]["readOnlyRootFilesystem"]
    assert {v["name"] for v in spec["volumes"]} == {"tmp", "model-cache"}


@pytest.mark.parametrize("name", sorted(cluster.CHART_POD_NAMES))
def test_a_session_pod_may_not_take_a_chart_pods_name(name: str) -> None:
    # Policies add up: a session pod labeled like `graph` would get graph's 443.
    with pytest.raises(SystemExit, match="chart service"):
        cluster.session_pod(name, "wikipulse:abc", ["python"], {})
    args = cluster.parse_args(["run", "--name", name, "--", "python", "-V"])
    with pytest.raises(SystemExit, match="chart service"):
        cluster.run_pod(args)  # refused before any kubectl call
    cluster.session_pod(f"{name}-session", "wikipulse:abc", ["python"], {})  # fine


def test_session_pod_env_must_be_key_value() -> None:
    assert cluster.parse_env(["A=1", "B=x=y"]) == {"A": "1", "B": "x=y"}
    with pytest.raises(SystemExit):
        cluster.parse_env(["nope"])
