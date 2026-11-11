"""The CI/CD pipeline (spec 09c): the workflows, Dependabot, and the scripts they run
(scripts/smoke_test.py, scripts/release.py, scripts/deploy.py, scripts/make_gate_fixture.py).
Nothing here runs a cluster, a build or a registry."""

import importlib.util
import re
import shutil
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from tests.eval.helpers import at, spike

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def load_script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


smoke = load_script("smoke_test")
release = load_script("release")
deploy = load_script("deploy")
make_gate = load_script("make_gate_fixture")


def workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text("utf-8"))
    data["on"] = data.pop(True)  # YAML 1.1 reads the key `on` as true
    return data


# --- Workflows ---


def test_ci_runs_every_stage_the_spec_lists() -> None:
    ci = workflow("ci.yml")
    assert set(ci["jobs"]) == {"test", "terraform", "dags", "eval-gate", "smoke"}
    steps = " ".join(str(step) for job in ci["jobs"].values() for step in job["steps"])
    for command in (
        "ruff check", "pytest -m integration", "helm lint", "terraform -chdir",
        "tests/dags", "wikipulse.eval.gate", "scripts/smoke_test.py",
    ):  # fmt: skip
        assert command in steps, command
    assert set(ci["jobs"]["test"]["services"]) == {"redis", "postgres", "neo4j", "rustfs"}


def test_ci_runs_on_pull_requests_branches_and_when_called() -> None:
    on = workflow("ci.yml")["on"]
    assert "pull_request" in on and "workflow_call" in on
    assert on["push"] == {"branches": ["**"]}  # tags run release.yml, which calls CI


def test_the_smoke_test_uses_the_same_service_images_as_the_tests() -> None:
    jobs = workflow("ci.yml")["jobs"]
    images = {name: s["image"] for name, s in jobs["test"]["services"].items()}
    assert {name: s["image"] for name, s in jobs["smoke"]["services"].items()} == images


def test_release_runs_on_version_tags_after_the_whole_ci() -> None:
    flow = workflow("release.yml")
    assert flow["on"] == {"push": {"tags": ["v*"]}}
    jobs = flow["jobs"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert "ci" in jobs["images"]["needs"]
    assert jobs["release"]["needs"] == ["version", "images", "chart"]
    names = {item["name"] for item in jobs["images"]["strategy"]["matrix"]["include"]}
    assert names == {"wikipulse", "wikipulse-airflow", "wikipulse-mlflow"}


def test_images_are_scanned_before_they_are_pushed() -> None:
    steps = workflow("release.yml")["jobs"]["images"]["steps"]
    names = [step["name"] for step in steps]
    assert names.index("Scan with Trivy") < names.index("Push")
    scan = next(step for step in steps if step["name"] == "Scan with Trivy")["run"]
    assert "--exit-code 1" in scan and "CRITICAL" in scan
    build = next(step for step in steps if step["name"] == "Build")["with"]
    assert build["load"] is True and "push" not in build


def test_workflow_tokens_have_the_least_permissions() -> None:
    ci = workflow("ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in ci["jobs"].values())
    flow = workflow("release.yml")
    assert flow["permissions"] == {}
    granted = {name: job["permissions"] for name, job in flow["jobs"].items()}
    assert granted == {
        "version": {"contents": "read"},
        "ci": {"contents": "read"},
        "images": {"contents": "read", "packages": "write"},
        "chart": {"contents": "read", "packages": "write"},
        "release": {"contents": "write"},
    }


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflows_use_no_stored_secrets_and_no_personal_data(name: str) -> None:
    text = (WORKFLOWS / name).read_text("utf-8")
    assert set(re.findall(r"secrets\.(\w+)", text)) <= {"GITHUB_TOKEN"}
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "an e-mail address"


def test_dependabot_watches_every_ecosystem() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text("utf-8"))
    assert config["version"] == 2
    ecosystems = {u["package-ecosystem"]: u for u in config["updates"]}
    assert set(ecosystems) == {"uv", "github-actions", "docker", "terraform"}
    assert set(ecosystems["docker"]["directories"]) == {"/", "/docker/airflow", "/docker/mlflow"}
    for update in config["updates"]:
        for directory in update.get("directories", [update.get("directory")]):
            assert (ROOT / directory.lstrip("/")).is_dir(), directory


# --- scripts/smoke_test.py ---

KIND_COREFILE = """.:53 {
    errors
    health {
       lameduck 5s
    }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
       pods insecure
       fallthrough in-addr.arpa ip6.arpa
       ttl 30
    }
    prometheus :9153
    forward . /etc/resolv.conf {
       max_concurrent 1000
    }
    cache 30
    loop
    reload
    loadbalance
}
"""


def test_the_corefile_gets_a_hosts_block_once() -> None:
    once = smoke.corefile_with_host(KIND_COREFILE, "host.docker.internal", "172.18.0.1")
    assert "    hosts {\n        172.18.0.1 host.docker.internal\n        fallthrough\n" in once
    assert once.index("hosts {") < once.index("kubernetes cluster.local")
    assert smoke.corefile_with_host(once, "host.docker.internal", "172.18.0.1") == once
    moved = smoke.corefile_with_host(once, "host.docker.internal", "172.19.0.1")
    assert "172.18.0.1" not in moved and moved.count("hosts {") == 1


def test_the_gateway_is_the_networks_ipv4_one() -> None:
    network = {
        "Name": "kind",
        "IPAM": {"Config": [{"Subnet": "fc00:f853::/64", "Gateway": "fc00:f853::1"},
                            {"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}]},
    }  # fmt: skip
    assert smoke.gateway_ip(network) == "172.18.0.1"
    with pytest.raises(SystemExit):
        smoke.gateway_ip({"Name": "x", "IPAM": {"Config": [{"Gateway": "fc00::1"}]}})


def deployment(name: str, replicas: int, ready: int | None) -> dict:
    status = {} if ready is None else {"readyReplicas": ready}
    return {"metadata": {"name": name}, "spec": {"replicas": replicas}, "status": status}


def pod(name: str, ready: bool, owner: str = "ReplicaSet", phase: str = "Running") -> dict:
    return {
        "metadata": {"name": name, "ownerReferences": [{"kind": owner}]},
        "status": {"phase": phase, "conditions": [{"type": "Ready", "status": str(ready)}]},
    }


def test_a_healthy_offline_release_has_no_problems() -> None:
    deployments = [
        deployment("api", 1, 1), deployment("detector", 1, 1),
        deployment("consumer", 0, None), deployment("graph", 0, None),
    ]  # fmt: skip
    pods = [
        pod("api-1", True), pod("detector-1", True),
        pod("migrate-1", False, owner="Job", phase="Succeeded"),
    ]  # fmt: skip
    assert smoke.problems(deployments, pods) == []


def test_unready_pods_and_replicas_are_problems() -> None:
    found = smoke.problems(
        [deployment("api", 1, 0)],
        [pod("api-1", False), pod("migrate-1", False, owner="Job", phase="Failed")],
    )
    assert found == [
        "deployment api: 0 of 1 replicas ready",
        "pod api-1: not Ready (Running)",
        "pod migrate-1 (a Job's): Failed, not Succeeded",
    ]
    assert smoke.problems([deployment("consumer", 0, None)], []) == [
        "no Deployment has any replicas"
    ]


def test_the_api_must_reach_its_databases() -> None:
    assert smoke.health_problems({"postgres": True, "neo4j": True}) == []
    assert smoke.health_problems({"postgres": True, "neo4j": False}) == [
        "the API can't reach neo4j"
    ]


def test_the_smoke_install_is_offline_on_its_own_cluster() -> None:
    command = smoke.install_command("ghcr.io/x/wikipulse:1.2.0")
    assert command[:3] == ["helm", "--kube-context", "kind-wikipulse-smoke"]
    values = [command[i + 1] for i, arg in enumerate(command) if arg == "--values"]
    assert [Path(v).name for v in values] == ["values-dev.yaml", "values-offline.yaml"]
    assert "image.repository=ghcr.io/x/wikipulse" in command
    assert "image.tag=1.2.0" in command and "--wait" in command
    assert smoke.CLUSTER != load_script("cluster").CLUSTER  # never the day-to-day cluster
    with pytest.raises(SystemExit):
        smoke.split_image("wikipulse")
    with pytest.raises(SystemExit):
        smoke.split_image("localhost:5001/wikipulse")


# --- scripts/release.py ---


def test_a_release_tag_must_match_the_project_version() -> None:
    assert release.version_from_tag("v0.2.0", "0.2.0") == "0.2.0"
    for tag in ("0.2.0", "v0.2", "vfoo", "v0.2.0+x", "v0.2.0-"):
        with pytest.raises(SystemExit):
            release.version_from_tag(tag, "0.2.0")
    with pytest.raises(SystemExit, match="pyproject"):
        release.version_from_tag("v0.3.0", "0.2.0")
    assert release.project_version()  # reads pyproject.toml


def test_a_pre_release_tag_of_the_project_version_is_accepted() -> None:
    # The release part must match pyproject; the full version names everything.
    assert release.version_from_tag("v0.1.0-rc.1", "0.1.0") == "0.1.0-rc.1"
    assert release.version_from_tag("v1.0.0-beta.2", "1.0.0") == "1.0.0-beta.2"
    with pytest.raises(SystemExit, match="pyproject"):
        release.version_from_tag("v0.2.0-rc.1", "0.1.0")
    assert release.is_prerelease("0.1.0-rc.1")
    assert not release.is_prerelease("0.1.0")


def test_the_version_command_prints_the_version_and_the_prerelease_flag(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release, "project_version", lambda: "0.1.0")
    release.main(["version", "--tag", "v0.1.0-rc.1"])
    release.main(["version", "--tag", "v0.1.0-rc.1", "--prerelease"])
    release.main(["version", "--tag", "v0.1.0", "--prerelease"])
    assert capsys.readouterr().out.split() == ["0.1.0-rc.1", "true", "false"]


def test_a_pre_release_gets_no_floating_tag_and_is_marked() -> None:
    jobs = workflow("release.yml")["jobs"]
    assert jobs["version"]["outputs"]["prerelease"] == "${{ steps.version.outputs.prerelease }}"
    meta = next(s for s in jobs["images"]["steps"] if s["name"] == "Image tags and labels")
    tags = meta["with"]["tags"].splitlines()
    floating = next(t for t in tags if "{{major}}.{{minor}}" in t)
    assert "enable=${{ needs.version.outputs.prerelease == 'false' }}" in floating
    assert "type=semver,pattern={{version}}" in tags  # 0.1.0-rc.1 itself
    create = next(s for s in jobs["release"]["steps"] if s["name"] == "Create the GitHub Release")
    assert create["env"]["PRERELEASE"] == "${{ needs.version.outputs.prerelease }}"
    assert "--prerelease" in create["run"]


def test_the_packaged_chart_carries_the_release_version_and_image() -> None:
    chart = (ROOT / "charts" / "wikipulse" / "Chart.yaml").read_text("utf-8")
    values = (ROOT / "charts" / "wikipulse" / "values.yaml").read_text("utf-8")
    new_chart = yaml.safe_load(release.set_chart_versions(chart, "0.2.0"))
    assert (new_chart["version"], new_chart["appVersion"]) == ("0.2.0", "0.2.0")
    new_values = release.set_default_image(values, "ghcr.io/owner/wikipulse", "0.2.0")
    image = yaml.safe_load(new_values)["image"]
    assert image == {
        "repository": "ghcr.io/owner/wikipulse", "tag": "0.2.0", "pullPolicy": "IfNotPresent",
    }  # fmt: skip
    # Only the image block changed; everything else (comments included) is kept.
    changed = [a for a, b in zip(values.splitlines(), new_values.splitlines(), strict=True)
               if a != b]  # fmt: skip
    assert changed == ["  repository: wikipulse", "  tag: dev"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")
@pytest.mark.parametrize("version", ["0.2.0", "0.1.0-rc.1"])
def test_package_chart_writes_a_lintable_versioned_chart(tmp_path: Path, version: str) -> None:
    package = release.package_chart(version, "ghcr.io/owner/wikipulse", tmp_path)
    assert package.name == f"wikipulse-{version}.tgz"
    with tarfile.open(package) as archive:
        names = archive.getnames()
        values = yaml.safe_load(archive.extractfile("wikipulse/values.yaml").read())
        chart = yaml.safe_load(archive.extractfile("wikipulse/Chart.yaml").read())
    assert (chart["version"], chart["appVersion"]) == (version, version)
    assert "wikipulse/values-offline.yaml" in names  # deploy.py needs the values files
    assert not any(name.endswith("secret.example.yaml") for name in names)
    assert values["image"]["repository"] == "ghcr.io/owner/wikipulse"
    assert values["image"]["tag"] == version
    # The chart folder in the repository is untouched.
    assert "tag: dev" in (ROOT / "charts" / "wikipulse" / "values.yaml").read_text("utf-8")


def test_deploy_accepts_a_pre_release_version() -> None:
    assert deploy.VERSION.match("0.1.0-rc.1")
    assert deploy.image_ref("ghcr.io/owner", "0.1.0-rc.1") == "ghcr.io/owner/wikipulse:0.1.0-rc.1"


# --- scripts/deploy.py ---


def test_the_registry_comes_from_the_git_remote_in_lowercase() -> None:
    assert deploy.registry_from_remote("https://github.com/Some-Owner/wikipulse.git") == (
        "ghcr.io/some-owner"
    )
    assert deploy.registry_from_remote("git@github.com:Owner/wikipulse.git") == "ghcr.io/owner"
    with pytest.raises(SystemExit):
        deploy.registry_from_remote("https://gitlab.com/owner/wikipulse.git")


def test_deploy_installs_the_released_chart_and_image(tmp_path: Path) -> None:
    chart = tmp_path / "wikipulse"
    command = deploy.install_command(chart, "ghcr.io/owner", "0.2.0", ["values-dev.yaml"], True)
    assert command[:3] == ["helm", "--kube-context", "kind-wikipulse"]
    assert str(chart) in command
    values = [command[i + 1] for i, arg in enumerate(command) if arg == "--values"]
    assert values == [str(chart / "values-dev.yaml"), str(chart / "values-offline.yaml")]
    assert "image.repository=ghcr.io/owner/wikipulse" in command
    assert "image.tag=0.2.0" in command
    pull = deploy.pull_chart_command("ghcr.io/owner", "0.2.0", tmp_path, plain_http=False)
    assert pull[:3] == ["helm", "pull", "oci://ghcr.io/owner/charts/wikipulse"]
    assert "--plain-http" not in pull
    assert deploy.image_ref("ghcr.io/owner", "0.2.0") == "ghcr.io/owner/wikipulse:0.2.0"


def test_deploy_refuses_a_placeholder_contact_before_doing_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(deploy.cluster, "run", lambda *a, **k: calls.append(a) or "")
    monkeypatch.setenv("USER_AGENT", "WikiPulse/0.1 (you@example.com)")
    deploy.get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="placeholder"):
            deploy.main(["0.2.0"])
    finally:
        deploy.get_settings.cache_clear()
    assert calls == []


# --- scripts/make_gate_fixture.py ---


def test_overlapping_spikes_of_one_article_are_one_burst() -> None:
    spikes = [
        spike("A", at(7, 2), duration_h=1),
        spike("A", at(7, 2.5), duration_h=1),  # another variant, overlapping
        spike("A", at(7, 6)),  # later: a burst of its own
        spike("B", at(7, 2)),
    ]
    bursts = make_gate.bursts_of(spikes)
    assert [(b.title, len(b.spikes)) for b in bursts] == [("A", 2), ("B", 1), ("A", 1)]
    first = bursts[0]
    assert first.end_at == at(7, 3.5)
    assert first.representative().first_edit_at == first.first_edit_at
