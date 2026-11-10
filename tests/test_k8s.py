"""The Kubernetes manifests of k8s/ and scripts/cluster.py (spec 08a), without a cluster.

The manifests are checked for the rules every WikiPulse pod must follow: probes,
resource requests and limits, non-root and read-only containers, one replica (Recreate)
for the workers, a network policy for each pod, and no secret values.
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
K8S = ROOT / "k8s"
SCRIPT = ROOT / "scripts" / "cluster.py"
spec = importlib.util.spec_from_file_location("cluster", SCRIPT)
assert spec is not None and spec.loader is not None
cluster = importlib.util.module_from_spec(spec)
sys.modules["cluster"] = cluster
spec.loader.exec_module(cluster)


def documents(name: str) -> list[dict]:
    text = (K8S / name).read_text(encoding="utf-8")
    return [doc for doc in yaml.safe_load_all(text) if doc]


def kustomization() -> dict:
    return documents("kustomization.yaml")[0]


def all_resources() -> list[dict]:
    return [doc for name in kustomization()["resources"] for doc in documents(name)]


def workloads() -> list[dict]:
    """Every Deployment, plus the migration Job."""
    deployments = [d for d in all_resources() if d["kind"] == "Deployment"]
    return deployments + documents("migrate-job.yaml")


def pod_spec(workload: dict) -> dict:
    return workload["spec"]["template"]["spec"]


def test_every_listed_file_exists_and_every_manifest_is_listed() -> None:
    listed = set(kustomization()["resources"])
    assert all((K8S / name).exists() for name in listed)
    not_applied = {"kustomization.yaml", "kind-config.yaml", "secret.example.yaml"}
    assert {p.name for p in K8S.glob("*.yaml")} - not_applied - listed == {"migrate-job.yaml"}


def test_the_deployments_are_the_pipeline() -> None:
    names = {d["metadata"]["name"] for d in all_resources() if d["kind"] == "Deployment"}
    assert names == set(cluster.DEPLOYMENTS)


def test_everything_is_in_the_namespace() -> None:
    for doc in all_resources() + documents("migrate-job.yaml"):
        if doc["kind"] != "Namespace":
            assert doc["metadata"]["namespace"] == "wikipulse", doc["metadata"]["name"]


@pytest.mark.parametrize("workload", workloads(), ids=lambda w: w["metadata"]["name"])
def test_pods_run_as_non_root_with_a_read_only_filesystem(workload: dict) -> None:
    spec = pod_spec(workload)
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["runAsUser"] != 0
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert spec["automountServiceAccountToken"] is False
    # Service links would set API_PORT=tcp://... and break the settings.
    assert spec["enableServiceLinks"] is False
    for container in spec["containers"]:
        context = container["securityContext"]
        assert context["allowPrivilegeEscalation"] is False
        assert context["readOnlyRootFilesystem"] is True
        assert context["capabilities"]["drop"] == ["ALL"]


@pytest.mark.parametrize("workload", workloads(), ids=lambda w: w["metadata"]["name"])
def test_containers_have_requests_and_limits(workload: dict) -> None:
    for container in pod_spec(workload)["containers"]:
        resources = container["resources"]
        assert {"cpu", "memory"} <= set(resources["requests"])
        assert {"cpu", "memory"} <= set(resources["limits"])
        assert container["image"].startswith("wikipulse:")
        assert container["imagePullPolicy"] == "IfNotPresent"


@pytest.mark.parametrize(
    "deployment",
    [d for d in all_resources() if d["kind"] == "Deployment"],
    ids=lambda d: d["metadata"]["name"],
)
def test_deployments_have_all_three_probes(deployment: dict) -> None:
    for container in pod_spec(deployment)["containers"]:
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            assert probe in container, f"{deployment['metadata']['name']}: no {probe}"
        if deployment["metadata"]["name"] != "api":
            command = container["livenessProbe"]["exec"]["command"]
            assert command[:3] == ["python", "-m", "wikipulse.health"]


@pytest.mark.parametrize(
    "deployment",
    [d for d in all_resources() if d["kind"] == "Deployment"],
    ids=lambda d: d["metadata"]["name"],
)
def test_workers_run_once_and_are_replaced_with_recreate(deployment: dict) -> None:
    if deployment["metadata"]["name"] == "api":
        return  # stateless: a rolling update is fine
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"]["type"] == "Recreate"


def test_the_consumer_keeps_its_own_checkpoint_namespace() -> None:
    (consumer,) = documents("consumer.yaml")
    command = pod_spec(consumer)["containers"][0]["command"]
    assert command[:3] == ["python", "-m", "wikipulse.stream.consume"]
    assert command[command.index("--namespace") + 1] == "wiki:k8s:consumer"


def test_every_pod_has_an_egress_policy_and_everything_else_is_denied() -> None:
    policies = [d for d in all_resources() if d["kind"] == "NetworkPolicy"]
    deny = next(p for p in policies if p["metadata"]["name"] == "default-deny-all")
    assert deny["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}
    selected = {
        p["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
        for p in policies
        if "Egress" in p["spec"]["policyTypes"] and p["spec"]["podSelector"]
    }
    assert selected == set(cluster.DEPLOYMENTS) | {"migrate"}
    ingress = [p for p in policies if p["spec"].get("ingress")]
    assert [p["metadata"]["name"] for p in ingress] == ["api-ingress"]


def test_the_config_map_holds_no_secrets() -> None:
    (config,) = documents("configmap.yaml")
    for key in config["data"]:
        assert not any(word in key for word in ("PASSWORD", "SECRET", "DSN", "KEY", "AGENT"))


def test_the_secret_template_holds_only_placeholders() -> None:
    (secret,) = documents("secret.example.yaml")
    assert set(secret["stringData"]) == set(cluster.secret_manifest(Settings())["stringData"])
    for value in secret["stringData"].values():
        assert "<" in value


# --- scripts/cluster.py ---


def test_image_tags() -> None:
    now = datetime(2026, 11, 10, 14, 3, 5, tzinfo=UTC)
    assert cluster.image_tag("3f2a1b9", False, now) == "3f2a1b9"
    assert cluster.image_tag("3f2a1b9", True, now) == "3f2a1b9-dirty-20261110140305"


def test_with_image_tag_replaces_only_wikipulse_images() -> None:
    rendered = (
        "containers:\n"
        "  - image: wikipulse:dev\n"
        "    name: a\n"
        "  - name: b\n"
        "    image: wikipulse:old\n"
        "  - image: redis:7-alpine\n"
        "env:\n"
        "  - value: 'image: wikipulse:dev'\n"
    )
    result = cluster.with_image_tag(rendered, "abc123")
    assert "  - image: wikipulse:abc123\n" in result
    assert "    image: wikipulse:abc123\n" in result
    assert "image: redis:7-alpine" in result
    assert "value: 'image: wikipulse:dev'" in result  # not an image field


def test_with_image_tag_covers_every_rendered_container() -> None:
    text = "\n---\n".join(
        (K8S / n).read_text(encoding="utf-8") for n in kustomization()["resources"]
    )
    result = cluster.with_image_tag(text, "t1")
    assert result.count("image: wikipulse:t1") == len(cluster.DEPLOYMENTS)
    assert "wikipulse:dev" not in result


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
