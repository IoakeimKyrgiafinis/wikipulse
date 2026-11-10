"""observability/: the dashboards, the alert rules and the monitoring values (spec 09a).

Checked without a cluster: the dashboards are what scripts/dashboards.py generates, every
metric a dashboard or a rule uses really exists, and the rules have what an alert needs.
`promtool test rules` runs the rules' unit tests when promtool is available (on the PATH,
or the Prometheus image already pulled into Docker; nothing is downloaded). In CI a missing
promtool is an error, not a skip: CI pulls the image, and these tests are the only check
of the rules' logic.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from wikipulse.metrics import catalog
from wikipulse.metrics.models import ModelHealth, drift_metrics, score_metrics

ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY = ROOT / "observability"
DASHBOARDS = OBSERVABILITY / "dashboards"
ALERTS = OBSERVABILITY / "alerts"
PROMETHEUS_IMAGE = "quay.io/prometheus/prometheus:v3.12.0"

sys.path.insert(0, str(ROOT / "scripts"))
import dashboards  # noqa: E402

METRIC = re.compile(r"\bwikipulse_[a-z_]+")
SPEC_ALERTS = {  # the spec's four alerts, by name
    "WikiPulseConsumerLagHigh",  # consumer lag above 5 minutes
    "WikiPulseNoEvents",  # no events for 2 minutes
    "WikiPulseExtractionFailuresHigh",  # extraction failure rate high
    "WikiPulseDriftDetected",  # drift detected
}


def exported_names() -> set[str]:
    """Every series name WikiPulse exports: the catalog's and the model collector's."""
    names = set()
    for value in vars(catalog).values():
        if hasattr(value, "_name") and hasattr(value, "_type"):
            base = value._name
            names |= {
                "counter": {f"{base}_total"},
                "gauge": {base},
                "histogram": {f"{base}_bucket", f"{base}_count", f"{base}_sum"},
            }[value._type]
    row = {"model_name": "m", "model_version": "1", "run_id": "r", "status": "ok", "rows": 1}
    row |= {"period_end": datetime.now(UTC), "report": {}}
    scores = {"model_name": "m", "model_version": "1", "run_id": "r"}
    scores |= {"scores": 1, "mean_score": 0.5, "news_share": 0.5}
    health = ModelHealth([row], [scores])
    families = [*drift_metrics(health.drift_reports), *score_metrics(health.scores)]
    return names | {family.name for family in families} | {"wikipulse_model_health_up"}


def load_rules() -> list[dict]:
    groups = yaml.safe_load((ALERTS / "wikipulse.rules.yaml").read_text(encoding="utf-8"))
    return [rule for group in groups["groups"] for rule in group["rules"]]


def dashboard_files() -> dict[str, dict]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(DASHBOARDS.glob("*.json"))
    }


# --- dashboards ---


def test_the_dashboards_are_generated_from_scripts_dashboards_py() -> None:
    assert set(dashboard_files()) == set(dashboards.DASHBOARDS)
    for name, builder in dashboards.DASHBOARDS.items():
        text = (DASHBOARDS / name).read_text(encoding="utf-8")
        assert text == dashboards.render(builder), f"{name}: run scripts/dashboards.py"


def test_the_spec_dashboards_exist() -> None:
    titles = {d["title"] for d in dashboard_files().values()}
    for topic in ("Pipeline health", "Detection", "LLM usage", "Model health"):
        assert f"WikiPulse: {topic}" in titles


def test_dashboards_use_the_provisioned_data_source_and_fit_the_grid() -> None:
    values = yaml.safe_load((OBSERVABILITY / "kube-prometheus-stack.yaml").read_text())
    uid = values["grafana"]["sidecar"]["datasources"]["uid"]
    uids = set()
    for name, board in dashboard_files().items():
        uids.add(board["uid"])
        ids = [panel["id"] for panel in board["panels"]]
        assert len(ids) == len(set(ids)), name
        for panel in board["panels"]:
            assert panel["datasource"]["uid"] == uid, (name, panel["title"])
            grid = panel["gridPos"]
            assert grid["x"] + grid["w"] <= 24, (name, panel["title"])
            assert panel["targets"] and panel["description"], (name, panel["title"])
    assert len(uids) == len(dashboard_files())


def test_every_metric_a_dashboard_or_rule_uses_exists() -> None:
    exported = exported_names()
    used = {
        metric
        for board in dashboard_files().values()
        for panel in board["panels"]
        for target in panel["targets"]
        for metric in METRIC.findall(target["expr"])
    }
    used |= {metric for rule in load_rules() for metric in METRIC.findall(rule["expr"])}
    assert used - exported == set()


def test_the_spec_metrics_are_exported() -> None:
    exported = exported_names()
    for metric in (
        "wikipulse_consumer_messages_total",  # events per second
        "wikipulse_consumer_lag_seconds",  # consumer lag
        "wikipulse_spikes_started_total",  # spikes started
        "wikipulse_llm_request_duration_seconds_bucket",  # LLM latency
        "wikipulse_llm_tokens_total",  # LLM tokens
        "wikipulse_extractions_total",  # extraction failures
        "wikipulse_model_score_bucket",  # model scores
        "wikipulse_drift_score_psi",  # drift values
    ):
        assert metric in exported, metric


# --- alert rules ---


def test_the_rules_have_what_an_alert_needs() -> None:
    rules = load_rules()
    names = [rule["alert"] for rule in rules]
    assert len(names) == len(set(names))
    assert SPEC_ALERTS <= set(names)
    for rule in rules:
        assert rule["labels"]["severity"] in ("critical", "warning", "info"), rule["alert"]
        assert rule["annotations"]["summary"] and rule["annotations"]["description"]


def test_the_alert_thresholds_are_the_specs() -> None:
    rules = {rule["alert"]: rule for rule in load_rules()}
    assert "> 300" in rules["WikiPulseConsumerLagHigh"]["expr"]  # 5 minutes
    assert rules["WikiPulseNoEvents"]["expr"].count("[2m]") == 2  # 2 minutes, both cases


def promtool() -> list[str] | None:
    """A promtool command, without downloading anything; None if there is none."""
    if shutil.which("promtool"):
        return ["promtool"]
    if shutil.which("docker") is None:
        return None
    image = subprocess.run(
        ["docker", "image", "inspect", PROMETHEUS_IMAGE], capture_output=True, check=False
    )
    if image.returncode != 0:
        return None
    mount = f"{ALERTS.as_posix()}:/rules:ro"
    return [
        "docker", "run", "--rm", "--network", "none", "-v", mount, "-w", "/rules",
        "--entrypoint", "promtool", PROMETHEUS_IMAGE,
    ]  # fmt: skip


def test_every_rule_has_a_promtool_test() -> None:
    tests = yaml.safe_load((ALERTS / "wikipulse.rules.test.yaml").read_text())
    tested = {check["alertname"] for test in tests["tests"] for check in test["alert_rule_test"]}
    assert {rule["alert"] for rule in load_rules()} - tested == set()


def test_service_down_does_not_depend_on_the_namespace() -> None:
    # The namespace is a Terraform variable; every WikiPulse target has system="wikipulse".
    rules = {rule["alert"]: rule for rule in load_rules()}
    assert rules["WikiPulseServiceDown"]["expr"].strip() == 'up{system="wikipulse"} == 0'


def test_ci_pulls_the_same_prometheus_image() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert f"docker pull {PROMETHEUS_IMAGE}" in workflow


def test_promtool_checks_and_unit_tests_pass() -> None:
    command = promtool()
    if command is None and os.environ.get("CI"):
        pytest.fail(f"no promtool in CI (the workflow pulls {PROMETHEUS_IMAGE})")
    if command is None:
        pytest.skip(f"no promtool (install it, or `docker pull {PROMETHEUS_IMAGE}`)")
    cwd = ALERTS if command == ["promtool"] else None
    for args in (
        ["check", "rules", "wikipulse.rules.yaml"],
        ["test", "rules", "wikipulse.rules.test.yaml"],
    ):
        result = subprocess.run(
            [*command, *args], capture_output=True, text=True, cwd=cwd, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr


# --- the monitoring stack ---


def test_the_monitoring_stack_is_lean() -> None:
    values = yaml.safe_load((OBSERVABILITY / "kube-prometheus-stack.yaml").read_text())
    for component in ("alertmanager", "nodeExporter", "kubeStateMetrics", "kubelet"):
        assert values[component]["enabled"] is False, component
    assert values["defaultRules"]["create"] is False
    assert values["grafana"]["defaultDashboardsEnabled"] is False
    assert values["grafana"]["sidecar"]["dashboards"]["label"] == "grafana_dashboard"
    assert values["prometheus"]["prometheusSpec"]["resources"]["limits"]["memory"]
