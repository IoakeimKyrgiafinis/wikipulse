"""Deploy a released version of WikiPulse to the local kind cluster (spec 09c).

    uv run python scripts/deploy.py 0.2.0                  # offline (the default)
    uv run python scripts/deploy.py 0.2.0 --values values-prod.yaml
    uv run python scripts/deploy.py 0.2.0 --online         # with the live services

What a release (.github/workflows/release.yml) published, put on the laptop's cluster:

1. pulls the release's image `<registry>/wikipulse:<version>` with your Docker login
   (`docker login ghcr.io`, needed while the package is private) and loads it into the
   kind node, as `scripts/cluster.py load` does with a local build;
2. pulls the release's chart `oci://<registry>/charts/wikipulse --version <version>`;
3. creates the namespace and the Secret from your settings, as `scripts/cluster.py secret`;
4. `helm upgrade --install` with that chart, its values-dev.yaml (or `--values`) and the
   release's image: a new revision of the release `wikipulse`, so
   `scripts/cluster.py rollback` goes back to what ran before.

The registry is `--registry`, else the WIKIPULSE_REGISTRY setting, else
`ghcr.io/<owner>` from the git remote `origin`. The cluster must exist
(`scripts/cluster.py create`). A cluster made by Terraform (infra/cluster) owns its
release: deploy there by setting the image tag in Terraform instead, or Terraform's next
apply undoes this.

Why the laptop pulls instead of GitHub pushing: see docs/learning/09c-cicd-pipeline.md
(continuous deployment to a laptop needs a self-hosted runner, a security decision).

Offline by default: values-offline.yaml gives the services that contact Wikimedia
(consumer, enricher, graph runner) 0 replicas, whatever USER_AGENT holds. Only
`--online` starts them, and only with a real contact in USER_AGENT: with the
placeholder it refuses before doing anything.
"""

import argparse
import logging
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so `wikipulse` imports when run as a script
sys.path.insert(0, str(ROOT / "scripts"))

import cluster  # noqa: E402

from wikipulse.config import Settings, get_settings  # noqa: E402
from wikipulse.logging_setup import setup_logging  # noqa: E402

logger = logging.getLogger("deploy")

VERSION = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
REMOTE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/")
ONLINE_NEEDS_CONTACT = (
    "--online starts the services that contact Wikimedia, whose policy requires a real "
    "way to reach you, but USER_AGENT still contains the placeholder contact. Set "
    "USER_AGENT in .env to your own contact address, or deploy offline (no --online)."
)


def registry_from_remote(url: str) -> str:
    """`https://github.com/Owner/wikipulse.git` -> `ghcr.io/owner` (GHCR wants lowercase)."""
    match = REMOTE.search(url)
    if match is None:
        raise SystemExit(f"Can't tell the GitHub owner from {url!r}: pass --registry")
    return f"ghcr.io/{match['owner'].lower()}"


def default_registry(settings: Settings) -> str:
    if settings.wikipulse_registry:
        return settings.wikipulse_registry.rstrip("/")
    url = cluster.run(["git", "remote", "get-url", "origin"], capture=True).strip()
    return registry_from_remote(url)


def image_ref(registry: str, version: str) -> str:
    return f"{registry}/{cluster.IMAGE}:{version}"


def chart_ref(registry: str) -> str:
    return f"oci://{registry}/charts/{cluster.RELEASE}"


def pull_chart_command(registry: str, version: str, into: Path, plain_http: bool) -> list[str]:
    command = ["helm", "pull", chart_ref(registry), "--version", version, "--untar"]
    command += ["--destination", str(into)]
    return command + (["--plain-http"] if plain_http else [])


def install_command(
    chart: Path, registry: str, version: str, values: list[str], offline: bool
) -> list[str]:
    """`helm upgrade --install` of the pulled chart, with its values files and the image."""
    command = cluster.helm("upgrade", "--install", cluster.RELEASE, str(chart))
    files = [chart / name for name in values]
    if offline:
        files.append(chart / cluster.OFFLINE_VALUES)
    for path in files:
        command += ["--values", str(path)]
    command += ["--set", f"image.repository={registry}/{cluster.IMAGE}"]
    command += ["--set", f"image.tag={version}"]
    return command + ["--wait", "--timeout", cluster.DEPLOY_TIMEOUT]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the release version, e.g. 0.2.0 (tag v0.2.0)")
    parser.add_argument("--registry", help="e.g. ghcr.io/<owner> (default: from git remote)")
    parser.add_argument(
        "--values",
        action="append",
        help="a values file of the chart, repeatable (default: values-dev.yaml)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline",
        dest="online",
        action="store_false",
        help="the default: add values-offline.yaml (nothing contacts Wikimedia)",
    )
    mode.add_argument(
        "--online",
        dest="online",
        action="store_true",
        help="also start the services that contact Wikimedia (needs a real USER_AGENT contact)",
    )
    parser.add_argument(
        "--plain-http", action="store_true", help="the registry speaks HTTP (a local test one)"
    )
    parser.set_defaults(online=False)  # offline unless --online says otherwise
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    version = args.version.removeprefix("v")
    if not VERSION.match(version):
        raise SystemExit(f"Not a release version: {args.version!r} (e.g. 0.2.0)")
    settings = get_settings()
    if args.online and settings.user_agent_is_placeholder():
        raise SystemExit(ONLINE_NEEDS_CONTACT)
    offline = not args.online
    logger.info("Deploying %s", "ONLINE (contacts Wikimedia)" if args.online else "offline")
    if not cluster.cluster_exists():
        raise SystemExit("No kind cluster `wikipulse`: run `scripts/cluster.py create` first")
    registry = (args.registry or default_registry(settings)).rstrip("/")
    image = image_ref(registry, version)

    cluster.run(["docker", "pull", image])
    cluster.run(["kind", "load", "docker-image", image, "--name", cluster.CLUSTER])
    with tempfile.TemporaryDirectory() as temp:
        cluster.run(pull_chart_command(registry, version, Path(temp), args.plain_http))
        chart = Path(temp) / cluster.RELEASE
        # Online, the contact was checked above; offline, a placeholder is harmless.
        cluster.secret(settings, allow_placeholder=offline)
        values = args.values or list(cluster.DEFAULT_VALUES)
        cluster.run(install_command(chart, registry, version, values, offline))
    logger.info("Deployed %s (chart %s %s)", image, chart_ref(registry), version)
    cluster.history()
    cluster.status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
