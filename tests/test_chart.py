"""The Helm chart charts/wikipulse (spec 08b): lint and template tests, without a cluster.

`helm template` renders the chart locally, exactly as `helm install` would send it to
the cluster; the tests check the rendered objects for the rules every WikiPulse pod
must follow (probes, requests and limits, non-root, read-only, singleton workers,
network policies, no secrets), for each environment's values. `helm lint --strict`
checks the chart's structure and the values against values.schema.json.

Needs the `helm` binary (no network, no cluster). Skipped when helm isn't installed,
except in CI, where a missing helm is an error (CI installs it).
"""

import os
import shutil
import subprocess
from functools import cache
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "wikipulse"
DEPLOYMENTS = ("consumer", "detector", "archiver", "enricher", "grouper", "graph", "api")
WORKERS = tuple(name for name in DEPLOYMENTS if name != "api")
CALLS_WIKIMEDIA = ("consumer", "enricher", "graph")
ENVIRONMENTS = {
    "default": (),
    "dev": ("values-dev.yaml",),
    "prod": ("values-prod.yaml",),
    "dev-offline": ("values-dev.yaml", "values-offline.yaml"),
    "prod-offline": ("values-prod.yaml", "values-offline.yaml"),
}

if shutil.which("helm") is None and not os.environ.get("CI"):
    pytest.skip("helm is not installed (CI installs it)", allow_module_level=True)


def helm(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", *args], capture_output=True, text=True, encoding="utf-8", check=check
    )


def values_args(files: tuple[str, ...], sets: tuple[str, ...] = ()) -> list[str]:
    args = [arg for name in files for arg in ("--values", str(CHART / name))]
    return args + [arg for item in sets for arg in ("--set", item)]


@cache
def render(files: tuple[str, ...] = (), sets: tuple[str, ...] = ()) -> tuple[dict, ...]:
    """The chart's objects, as `helm install` would create them (hooks included)."""
    result = helm(
        "template", "wikipulse", str(CHART), "--namespace", "wikipulse",
        *values_args(files, sets),
    )  # fmt: skip
    return tuple(doc for doc in yaml.safe_load_all(result.stdout) if doc)


def of_kind(docs: tuple[dict, ...], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def workloads(docs: tuple[dict, ...]) -> list[dict]:
    """Every Deployment, plus the migration Job."""
    return list(of_kind(docs, "Deployment").values()) + list(of_kind(docs, "Job").values())


def pod_spec(workload: dict) -> dict:
    return workload["spec"]["template"]["spec"]


def memory_mib(quantity: str) -> float:
    units = {"Mi": 1, "Gi": 1024}
    return float(quantity[:-2]) * units[quantity[-2:]]


def cpu_millis(quantity: str | int) -> float:
    text = str(quantity)
    return float(text[:-1]) if text.endswith("m") else float(text) * 1000


# --- lint ---


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_helm_lint_strict(env: str) -> None:
    result = helm("lint", str(CHART), "--strict", *values_args(ENVIRONMENTS[env]), check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("assignment", "message"),
    [
        ("consumer.replicas=2", "maximum"),  # a second Wikimedia connection
        ("detector.replicas=3", "maximum"),  # per-page state can't be shared
        ("config.NEO4J_PASSWORD=x", "NEO4J_PASSWORD"),  # secrets belong in the Secret
        ("config.USER_AGENT=x", "USER_AGENT"),
        ("api.replica=2", "replica"),  # a typo, caught instead of ignored
        ("image.pullPolicy=Sometimes", "pullPolicy"),
    ],
)
def test_the_schema_refuses_dangerous_values(assignment: str, message: str) -> None:
    result = helm("template", "wikipulse", str(CHART), "--set", assignment, check=False)
    assert result.returncode != 0
    assert "schema" in result.stderr and message in result.stderr


# --- what every environment renders ---


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_the_deployments_are_the_pipeline(env: str) -> None:
    assert set(of_kind(render(ENVIRONMENTS[env]), "Deployment")) == set(DEPLOYMENTS)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_pods_run_as_non_root_with_a_read_only_filesystem(env: str) -> None:
    for workload in workloads(render(ENVIRONMENTS[env])):
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


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_containers_have_requests_within_limits(env: str) -> None:
    for workload in workloads(render(ENVIRONMENTS[env])):
        for container in pod_spec(workload)["containers"]:
            requests, limits = container["resources"]["requests"], container["resources"]["limits"]
            assert memory_mib(requests["memory"]) <= memory_mib(limits["memory"])
            assert cpu_millis(requests["cpu"]) <= cpu_millis(limits["cpu"])
            assert container["imagePullPolicy"] == "IfNotPresent"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_deployments_have_all_three_probes(env: str) -> None:
    for name, deployment in of_kind(render(ENVIRONMENTS[env]), "Deployment").items():
        (container,) = pod_spec(deployment)["containers"]
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            assert probe in container, f"{name}: no {probe}"
        if name == "api":
            assert container["livenessProbe"]["httpGet"]["path"] == "/health"
        else:
            command = container["livenessProbe"]["exec"]["command"]
            assert command[:3] == ["python", "-m", "wikipulse.health"]
            assert all(isinstance(arg, str) for arg in command)


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_workers_are_singletons_replaced_with_recreate(env: str) -> None:
    deployments = of_kind(render(ENVIRONMENTS[env]), "Deployment")
    for name in WORKERS:
        assert deployments[name]["spec"]["replicas"] in (0, 1)
        assert deployments[name]["spec"]["strategy"]["type"] == "Recreate"
    assert deployments["api"]["spec"]["strategy"]["type"] == "RollingUpdate"


@pytest.mark.parametrize("env", ENVIRONMENTS)
def test_every_pod_has_an_egress_policy_and_everything_else_is_denied(env: str) -> None:
    policies = of_kind(render(ENVIRONMENTS[env]), "NetworkPolicy")
    assert policies["default-deny-all"]["spec"] == {
        "podSelector": {},
        "policyTypes": ["Ingress", "Egress"],
    }
    selected = {
        p["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
        for p in policies.values()
        if "Egress" in p["spec"]["policyTypes"] and p["spec"]["podSelector"]
    }
    assert selected == set(DEPLOYMENTS) | {"migrate"}
    assert [name for name, p in policies.items() if p["spec"].get("ingress")] == ["api-ingress"]
    (rule,) = policies["api-ingress"]["spec"]["ingress"]
    assert rule["from"] == [{"podSelector": {}}]  # no namespaceSelector, no ipBlock
    assert rule["ports"] == [{"protocol": "TCP", "port": 8000}]


def test_egress_ports_are_the_ones_each_service_needs() -> None:
    policies = of_kind(render(), "NetworkPolicy")

    def ports(name: str) -> set[int]:
        (rule,) = policies[f"{name}-egress"]["spec"]["egress"]
        return {port["port"] for port in rule["ports"]}

    assert ports("consumer") == {6379, 443}
    assert ports("detector") == {6379, 5432}
    assert ports("archiver") == {6379, 9000}
    assert ports("migrate") == {5432}
    # Only the services that need HTTPS (Wikimedia, Hugging Face) may open it.
    https = {name for name in DEPLOYMENTS if 443 in ports(name)}
    assert https == {"consumer", "enricher", "grouper", "graph", "api"}


# --- objects and settings ---


def test_the_consumer_keeps_its_own_checkpoint_namespace() -> None:
    consumer = of_kind(render(("values-dev.yaml",)), "Deployment")["consumer"]
    command = pod_spec(consumer)["containers"][0]["command"]
    assert command[:3] == ["python", "-m", "wikipulse.stream.consume"]
    assert command[command.index("--namespace") + 1] == "wiki:k8s:consumer"


def test_the_image_tag_reaches_every_container() -> None:
    docs = render(("values-dev.yaml",), ("image.tag=abc123",))
    images = {c["image"] for w in workloads(docs) for c in pod_spec(w)["containers"]}
    assert images == {"wikipulse:abc123"}


def test_external_services_are_aliases_of_the_host() -> None:
    services = of_kind(render(), "Service")
    for name in ("redis", "postgres", "neo4j", "s3"):
        assert services[name]["spec"] == {
            "type": "ExternalName",
            "externalName": "host.docker.internal",
        }
    assert services["api"]["spec"]["type"] == "ClusterIP"


def test_everything_lands_in_the_release_namespace() -> None:
    # No object names a namespace itself: `helm install --namespace` decides.
    assert all("namespace" not in doc["metadata"] for doc in render())


def test_the_config_map_holds_no_secrets() -> None:
    (config,) = of_kind(render(), "ConfigMap").values()
    assert config["metadata"]["name"] == "wikipulse-config"
    for key, value in config["data"].items():
        assert not any(word in key for word in ("PASSWORD", "SECRET", "DSN", "KEY", "AGENT"))
        assert isinstance(value, str)
    assert config["data"]["REDIS_URL"] == "redis://redis:6379/0"


def test_the_chart_creates_no_secret_and_the_pods_read_the_existing_one() -> None:
    docs = render()
    assert of_kind(docs, "Secret") == {}
    for workload in workloads(docs):
        refs = [
            e.get("secretRef", {}).get("name")
            for e in pod_spec(workload)["containers"][0]["envFrom"]
        ]
        assert "wikipulse-secrets" in refs


def test_migrations_run_as_a_hook_before_install_and_upgrade() -> None:
    (job,) = of_kind(render(), "Job").values()
    annotations = job["metadata"]["annotations"]
    assert annotations["helm.sh/hook"] == "pre-install,pre-upgrade"
    assert annotations["helm.sh/hook-delete-policy"] == "before-hook-creation"
    # The ConfigMap doesn't exist yet on the first install: the settings are inlined.
    (config,) = of_kind(render(), "ConfigMap").values()
    env = {e["name"]: e["value"] for e in pod_spec(job)["containers"][0]["env"]}
    assert env == config["data"]
    # ... and the Service `postgres` doesn't exist yet either: it connects to the host.
    command = pod_spec(job)["containers"][0]["command"]
    assert command == ["python", "-m", "wikipulse.db.migrate", "--host", "host.docker.internal"]
    assert of_kind(render(sets=("migrations.enabled=false",)), "Job") == {}


def test_a_config_change_rolls_the_pods() -> None:
    """The checksum annotation changes with the ConfigMap, so Deployments get new pods."""

    def checksums(sets: tuple[str, ...]) -> set[str]:
        deployments = of_kind(render(sets=sets), "Deployment").values()
        return {
            d["spec"]["template"]["metadata"]["annotations"]["checksum/config"] for d in deployments
        }

    before, after = checksums(()), checksums(("config.LOG_LEVEL=DEBUG",))
    assert len(before) == len(after) == 1
    assert before != after


def test_a_secret_checksum_rolls_the_pods_and_the_chart_checksum_does_not() -> None:
    """Terraform passes both (spec 08c): the Secret's on every pod, the chart's on none."""

    def annotations(sets: tuple[str, ...]) -> list[dict]:
        deployments = of_kind(render(sets=sets), "Deployment").values()
        return [d["spec"]["template"]["metadata"]["annotations"] for d in deployments]

    assert all("checksum/secret" not in a for a in annotations(()))
    rolled = annotations(("checksums.secret=abc123",))
    assert all(a["checksum/secret"] == "abc123" for a in rolled)
    assert annotations(("checksums.chart=def456",)) == annotations(())


def test_the_model_cache_survives_uninstall_and_is_mounted_where_needed() -> None:
    docs = render()
    (claim,) = of_kind(docs, "PersistentVolumeClaim").values()
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    mounted = {
        name
        for name, d in of_kind(docs, "Deployment").items()
        if any(v["name"] == "model-cache" for v in pod_spec(d)["volumes"])
    }
    assert mounted == {"grouper", "api"}


def test_selectors_match_their_pods() -> None:
    docs = render(("values-prod.yaml",))
    for deployment in of_kind(docs, "Deployment").values():
        selector = deployment["spec"]["selector"]["matchLabels"]
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        assert selector.items() <= labels.items()
    api_pods = of_kind(docs, "Deployment")["api"]["spec"]["template"]["metadata"]["labels"]
    assert of_kind(docs, "Service")["api"]["spec"]["selector"].items() <= api_pods.items()
    budget = of_kind(docs, "PodDisruptionBudget")["api"]["spec"]["selector"]["matchLabels"]
    assert budget.items() <= api_pods.items()


# --- environments ---


def replicas(docs: tuple[dict, ...]) -> dict[str, int]:
    return {name: d["spec"]["replicas"] for name, d in of_kind(docs, "Deployment").items()}


def test_dev_runs_everything_once() -> None:
    assert replicas(render(("values-dev.yaml",))) == dict.fromkeys(DEPLOYMENTS, 1)
    assert of_kind(render(("values-dev.yaml",)), "PodDisruptionBudget") == {}


def test_prod_has_more_api_replicas_and_a_disruption_budget() -> None:
    docs = render(("values-prod.yaml",))
    assert replicas(docs) == {**dict.fromkeys(WORKERS, 1), "api": 2}
    assert of_kind(docs, "PodDisruptionBudget")["api"]["spec"]["minAvailable"] == 1


def test_prod_limits_are_stricter_than_dev() -> None:
    def limits(files: tuple[str, ...]) -> dict[str, dict]:
        return {
            name: pod_spec(d)["containers"][0]["resources"]["limits"]
            for name, d in of_kind(render(files), "Deployment").items()
        }

    dev, prod = limits(("values-dev.yaml",)), limits(("values-prod.yaml",))
    for name in DEPLOYMENTS:
        assert memory_mib(prod[name]["memory"]) < memory_mib(dev[name]["memory"]), name
        assert cpu_millis(prod[name]["cpu"]) < cpu_millis(dev[name]["cpu"]), name


@pytest.mark.parametrize("env", ["dev-offline", "prod-offline"])
def test_offline_runs_nothing_that_calls_wikimedia(env: str) -> None:
    counts = replicas(render(ENVIRONMENTS[env]))
    assert {name: counts[name] for name in CALLS_WIKIMEDIA} == dict.fromkeys(CALLS_WIKIMEDIA, 0)
    assert all(counts[name] >= 1 for name in DEPLOYMENTS if name not in CALLS_WIKIMEDIA)
