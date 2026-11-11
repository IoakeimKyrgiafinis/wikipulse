"""Smoke test (spec 09c): a throwaway kind cluster, the chart installed, every pod ready.

    uv run python scripts/smoke_test.py --image wikipulse:ci --host-gateway

1. create the kind cluster `wikipulse-smoke` (k8s/kind-config.yaml), separate from the
   day-to-day cluster `wikipulse`;
2. load the image into its node (`kind load docker-image`; build it first);
3. with `--host-gateway`: make the name `host.docker.internal` resolve inside the
   cluster to the Docker network's gateway, i.e. the machine Docker runs on. Docker
   Desktop provides that name by itself; Linux (CI) does not. The chart reaches Redis,
   Postgres, Neo4j and the S3 store through that name, wherever they run: Docker Compose
   on the laptop, the job's service containers in CI;
4. the namespace and the Secret (from your settings, as `scripts/cluster.py secret`);
5. `helm upgrade --install` with values-dev.yaml and values-offline.yaml: the consumer,
   enricher and graph runner (the services that contact Wikimedia) get 0 replicas, so
   nothing in the cluster talks to Wikimedia. `--wait` returns when the pods are ready;
6. checks: every Deployment has all its replicas ready, every pod is Ready (the
   migration Job's pod: Succeeded), and the API's /health reaches Postgres and Neo4j;
7. deletes the cluster again, unless `--keep`.

On failure it prints the pods, their events and their last log lines, then exits 1.
Needs Docker, kind, kubectl and helm.
"""

import argparse
import json
import logging
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so `wikipulse` imports when run as a script
sys.path.insert(0, str(ROOT / "scripts"))

import cluster  # noqa: E402

from wikipulse.config import get_settings  # noqa: E402
from wikipulse.logging_setup import setup_logging  # noqa: E402

logger = logging.getLogger("smoke_test")

CLUSTER = "wikipulse-smoke"
CONTEXT = f"kind-{CLUSTER}"
NAMESPACE = cluster.NAMESPACE
RELEASE = cluster.RELEASE
VALUES = ("values-dev.yaml", "values-offline.yaml")
HOST_NAME = "host.docker.internal"
TIMEOUT = "15m"  # the first grouper start downloads the embedding model
HEALTH_CHECK = (
    "import json, urllib.request; "
    "req = urllib.request.Request('http://127.0.0.1:8000/health', "
    "headers={'Host': 'localhost'}); "
    "print(urllib.request.urlopen(req, timeout=10).read().decode())"
)


def run(command: list[str], stdin: str | None = None, capture: bool = False) -> str:
    return cluster.run(command, stdin=stdin, capture=capture)


def kubectl(*args: str) -> list[str]:
    return ["kubectl", "--context", CONTEXT, *args]


def helm(*args: str) -> list[str]:
    return ["helm", "--kube-context", CONTEXT, "--namespace", NAMESPACE, *args]


# --- host.docker.internal on Linux ---


def gateway_ip(network: dict[str, Any]) -> str:
    """The IPv4 gateway of a Docker network (`docker network inspect` output, one item)."""
    for entry in network.get("IPAM", {}).get("Config") or []:
        gateway = entry.get("Gateway", "")
        if gateway and ":" not in gateway:
            return gateway
    raise SystemExit(f"Docker network {network.get('Name')!r} has no IPv4 gateway")


def corefile_with_host(corefile: str, name: str, ip: str) -> str:
    """CoreDNS's Corefile with a `hosts` block answering `name` with `ip`.

    `fallthrough` passes every other name on to the next plugins, as before. A block
    added earlier is replaced, so running this twice changes nothing.
    """
    block = f"    hosts {{\n        {ip} {name}\n        fallthrough\n    }}\n"
    lines = corefile.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for line in lines:
        if skipping:
            skipping = line.strip() != "}"
            continue
        if line.strip() == "hosts {":
            skipping = True
            continue
        out.append(line)
        if line.rstrip().endswith("{") and line.startswith(".:53"):
            out.append(block)
    if block not in out:
        raise SystemExit("CoreDNS's Corefile has no `.:53 {` server block")
    return "".join(out)


def point_host_name_at_gateway() -> None:
    network = json.loads(run(["docker", "network", "inspect", "kind"], capture=True))[0]
    ip = gateway_ip(network)
    configmap = json.loads(
        run(kubectl("-n", "kube-system", "get", "configmap", "coredns", "-o", "json"), capture=True)
    )
    configmap["data"]["Corefile"] = corefile_with_host(configmap["data"]["Corefile"], HOST_NAME, ip)
    run(kubectl("replace", "-f", "-"), stdin=json.dumps(configmap))
    run(kubectl("-n", "kube-system", "rollout", "restart", "deployment", "coredns"))
    run(kubectl("-n", "kube-system", "rollout", "status", "deployment", "coredns", "--timeout=2m"))
    logger.info("Inside the cluster, %s now resolves to %s", HOST_NAME, ip)


# --- install ---


def split_image(image: str) -> tuple[str, str]:
    """`ghcr.io/x/wikipulse:1.2.0` -> ("ghcr.io/x/wikipulse", "1.2.0")."""
    repository, sep, tag = image.rpartition(":")
    if not sep or "/" in tag:
        raise SystemExit(f"--image needs a tag, e.g. wikipulse:ci (got {image!r})")
    return repository, tag


def install_command(image: str) -> list[str]:
    repository, tag = split_image(image)
    command = helm("upgrade", "--install", RELEASE, str(cluster.CHART))
    for name in VALUES:
        command += ["--values", str(cluster.CHART / name)]
    command += ["--set", f"image.repository={repository}", "--set", f"image.tag={tag}"]
    return command + ["--wait", "--timeout", TIMEOUT]


def install(image: str) -> None:
    settings = get_settings()
    run(kubectl("apply", "-f", "-"), stdin=json.dumps(cluster.namespace_manifest()))
    # Through stdin: the values never touch the disk or show up in a process list.
    run(kubectl("apply", "-f", "-"), stdin=json.dumps(cluster.secret_manifest(settings)))
    run(install_command(image))


# --- checks ---


def owner_kind(item: dict[str, Any]) -> str:
    owners = item["metadata"].get("ownerReferences") or [{}]
    return owners[0].get("kind", "")


def is_ready(pod: dict[str, Any]) -> bool:
    conditions = pod.get("status", {}).get("conditions") or []
    return any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)


def problems(deployments: Sequence[dict], pods: Sequence[dict]) -> list[str]:
    """What isn't healthy, from `kubectl get deployments,pods -o json` items."""
    found = []
    running = [d for d in deployments if d["spec"].get("replicas", 1) > 0]
    if not running:
        found.append("no Deployment has any replicas")
    for deployment in running:
        name = deployment["metadata"]["name"]
        want = deployment["spec"].get("replicas", 1)
        ready = deployment.get("status", {}).get("readyReplicas", 0)
        if ready < want:
            found.append(f"deployment {name}: {ready} of {want} replicas ready")
    for pod in pods:
        name, phase = pod["metadata"]["name"], pod.get("status", {}).get("phase")
        if owner_kind(pod) == "Job":
            if phase != "Succeeded":
                found.append(f"pod {name} (a Job's): {phase}, not Succeeded")
        elif not is_ready(pod):
            found.append(f"pod {name}: not Ready ({phase})")
    return found


def health_problems(health: dict[str, Any]) -> list[str]:
    return [f"the API can't reach {name}" for name in ("postgres", "neo4j") if not health.get(name)]


def get_items(kind: str) -> list[dict]:
    output = run(kubectl("-n", NAMESPACE, "get", kind, "-o", "json"), capture=True)
    return json.loads(output)["items"]


def check() -> list[str]:
    deployments, pods = get_items("deployments"), get_items("pods")
    for deployment in deployments:
        spec, status = deployment["spec"], deployment.get("status", {})
        logger.info("%-9s %d/%d ready", deployment["metadata"]["name"],
                    status.get("readyReplicas", 0), spec.get("replicas", 1))  # fmt: skip
    found = problems(deployments, pods)
    if not found:
        output = run(
            kubectl("-n", NAMESPACE, "exec", "deploy/api", "--", "python", "-c", HEALTH_CHECK),
            capture=True,
        )
        logger.info("API /health: %s", output.strip())
        found += health_problems(json.loads(output))
    return found


def diagnostics() -> None:
    """Everything useful for a failed run in CI, where the cluster is gone afterwards."""
    for command in (
        kubectl("-n", NAMESPACE, "get", "pods,deployments,jobs", "-o", "wide"),
        kubectl("-n", NAMESPACE, "describe", "pods"),
        kubectl("-n", NAMESPACE, "get", "events", "--sort-by=.lastTimestamp"),
        kubectl(
            "-n",
            NAMESPACE,
            "logs",
            "--all-containers",
            "--prefix",
            "--tail=40",
            "-l",
            "app.kubernetes.io/part-of=wikipulse",
        ),  # fmt: skip
    ):
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        print(f"$ {' '.join(command)}\n{result.stdout}{result.stderr}", flush=True)


# --- command line ---


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True, help="the image to test, e.g. wikipulse:ci")
    parser.add_argument(
        "--host-gateway",
        action="store_true",
        help=f"resolve {HOST_NAME} to the Docker network's gateway (Linux, CI)",
    )
    parser.add_argument("--keep", action="store_true", help="leave the cluster running")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    if CLUSTER in run(["kind", "get", "clusters"], capture=True).split():
        raise SystemExit(
            f"Cluster {CLUSTER} exists already: `kind delete cluster --name {CLUSTER}`"
        )
    run(["kind", "create", "cluster", "--name", CLUSTER, "--config", str(cluster.KIND_CONFIG),
         "--wait", "2m"])  # fmt: skip
    failed = True
    try:
        run(["kind", "load", "docker-image", args.image, "--name", CLUSTER])
        if args.host_gateway:
            point_host_name_at_gateway()
        install(args.image)
        found = check()
        for problem in found:
            logger.error("%s", problem)
        failed = bool(found)
    except SystemExit as error:
        logger.error("%s", error)
    finally:
        if failed:
            diagnostics()
        if args.keep:
            logger.info("Keeping the cluster: `kind delete cluster --name %s`", CLUSTER)
        else:
            run(["kind", "delete", "cluster", "--name", CLUSTER])
    print("FAIL" if failed else "PASS: every pod is ready, and the API reaches its databases")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
