"""WikiPulse on a local Kubernetes cluster (kind): `uv run python scripts/cluster.py <command>`.

Commands:
  up        everything below in order: create, build, load, secret, deploy, status
  create    create the kind cluster `wikipulse` (one node) if it doesn't exist
  build     build the image `wikipulse:<tag>` from the Dockerfile
  load      copy the image into the cluster's node (kind has no registry to pull from)
  secret    create or update the Secret `wikipulse-secrets` from your settings
  deploy    install or upgrade the Helm release `wikipulse` (charts/wikipulse) with the
            image tag; Helm runs the database migrations first and waits for the pods
  status    show the pods
  history   show the release's revisions (`helm history`)
  rollback  go back to the previous revision, or to `--revision N`
  down      delete the cluster (the data in Redis, Postgres, Neo4j and S3 stays in Compose)

`deploy` uses charts/wikipulse/values-dev.yaml unless `--values` names other files
(`--values values-prod.yaml`, relative to the chart or not); `--set key=value` overrides
single values (`--set api.replicas=2`). Every deploy is a new revision of the release.

The tag is the git commit (`3f2a1b9`), plus `-dirty-<time>` when there are uncommitted
changes, so every build has a new tag and a rollout always means a new image. `load`
and `deploy` use the newest image built (`--tag` picks another).

Every kubectl and helm call names the context `kind-wikipulse`, so this script never
touches another cluster. Needs Docker Desktop, kind, kubectl and helm.

`up`, `secret` and `deploy` refuse to run while USER_AGENT still holds the placeholder
contact: the cluster's consumer connects to Wikimedia, which requires a real contact
address. Set yours in .env. `--allow-placeholder-contact` skips the check for offline
or CI use: `deploy` then adds values-offline.yaml, so the services that call Wikimedia
(consumer, enricher, graph) run with 0 replicas. `--offline` does that by choice.
"""

import argparse
import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so `wikipulse` imports when run as a script

from wikipulse.config import Settings, get_settings  # noqa: E402
from wikipulse.logging_setup import setup_logging  # noqa: E402

logger = logging.getLogger("cluster")

CLUSTER = "wikipulse"
CONTEXT = f"kind-{CLUSTER}"
NAMESPACE = "wikipulse"
IMAGE = "wikipulse"
KIND_CONFIG = ROOT / "k8s" / "kind-config.yaml"
CHART = ROOT / "charts" / "wikipulse"
RELEASE = "wikipulse"
DEFAULT_VALUES = ("values-dev.yaml",)
OFFLINE_VALUES = "values-offline.yaml"  # consumer, enricher and graph at 0 replicas
SECRET_NAME = "wikipulse-secrets"
DEPLOYMENTS = ("consumer", "detector", "archiver", "enricher", "grouper", "graph", "api")
DEPLOY_TIMEOUT = "15m"  # the first grouper start downloads the embedding model


def run(command: list[str], stdin: str | None = None, capture: bool = False) -> str:
    """Run a command, failing loudly. Returns its output when `capture` is set."""
    logger.info("$ %s", " ".join(command))
    result = subprocess.run(
        command, input=stdin, text=True, capture_output=capture, check=False, encoding="utf-8"
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() if capture else ""
        raise SystemExit(f"Command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result.stdout if capture else ""


def kubectl(*args: str) -> list[str]:
    return ["kubectl", "--context", CONTEXT, *args]


def helm(*args: str) -> list[str]:
    return ["helm", "--kube-context", CONTEXT, "--namespace", NAMESPACE, *args]


# --- image tags ---


def image_tag(commit: str, dirty: bool, now: datetime) -> str:
    """`<commit>`, or `<commit>-dirty-<UTC time>` for a build with uncommitted changes."""
    return f"{commit}-dirty-{now:%Y%m%d%H%M%S}" if dirty else commit


def current_tag() -> str:
    commit = run(["git", "rev-parse", "--short", "HEAD"], capture=True).strip()
    dirty = bool(run(["git", "status", "--porcelain"], capture=True).strip())
    return image_tag(commit, dirty, datetime.now(UTC))


def newest_image_tag() -> str:
    """The tag of the newest `wikipulse` image Docker has (built by `build`)."""
    output = run(
        ["docker", "image", "ls", IMAGE, "--format", "{{.Tag}}\t{{.CreatedAt}}"], capture=True
    )
    rows = [line.split("\t") for line in output.splitlines() if line.strip()]
    rows = [row for row in rows if row[0] not in ("dev", "<none>")]
    if not rows:
        raise SystemExit("No wikipulse image yet: run `scripts/cluster.py build` first.")
    return max(rows, key=lambda row: row[1])[0]


# --- the Secret ---


def cluster_dsn(dsn: str, host: str = "postgres") -> str:
    """The Postgres DSN with its host replaced by the in-cluster Service name."""
    parts = urlsplit(dsn)
    userinfo, _, hostport = parts.netloc.rpartition("@")
    port = hostport.rpartition(":")[2] if ":" in hostport else ""
    netloc = f"{userinfo}@{host}" if userinfo else host
    if port:
        netloc += f":{port}"
    return urlunsplit(parts._replace(netloc=netloc))


def secret_manifest(settings: Settings) -> dict:
    """The Secret with the settings that must not be committed (built in memory only)."""
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": SECRET_NAME,
            "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/part-of": "wikipulse"},
        },
        "type": "Opaque",
        "stringData": {
            "POSTGRES_DSN": cluster_dsn(settings.postgres_dsn),
            "NEO4J_PASSWORD": settings.neo4j_password.get_secret_value(),
            "S3_ACCESS_KEY": settings.s3_access_key.get_secret_value(),
            "S3_SECRET_KEY": settings.s3_secret_key.get_secret_value(),
            "USER_AGENT": settings.user_agent,
        },
    }


# --- commands ---


def cluster_exists() -> bool:
    return CLUSTER in run(["kind", "get", "clusters"], capture=True).split()


def create() -> None:
    if cluster_exists():
        logger.info("Cluster %s exists already", CLUSTER)
        return
    run(["kind", "create", "cluster", "--name", CLUSTER, "--config", str(KIND_CONFIG)])


def build() -> str:
    tag = current_tag()
    run(["docker", "build", "-t", f"{IMAGE}:{tag}", str(ROOT)])
    logger.info("Built %s:%s", IMAGE, tag)
    return tag


def load(tag: str) -> None:
    run(["kind", "load", "docker-image", f"{IMAGE}:{tag}", "--name", CLUSTER])


# --- the namespace ---


def namespace_manifest() -> dict:
    """The namespace, created before the release (Helm keeps its records in it).

    Pod Security Admission: the label makes the API server refuse pods that could run
    as root, add capabilities or escalate privileges.
    """
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": NAMESPACE,
            "labels": {
                "app.kubernetes.io/part-of": "wikipulse",
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": "latest",
            },
        },
    }


def ensure_namespace() -> None:
    run(kubectl("apply", "-f", "-"), stdin=json.dumps(namespace_manifest()))


PLACEHOLDER_CONTACT_ERROR = (
    "USER_AGENT still contains the placeholder contact (e.g. you@example.com). The "
    "cluster's consumer connects to Wikimedia, whose policy requires a real way to reach "
    "you: set USER_AGENT in .env to your own contact address and run this again. "
    "(--allow-placeholder-contact skips this check, for offline or CI use only.)"
)


def check_contact(settings: Settings, allow_placeholder: bool = False) -> bool:
    """Refuse a placeholder User-Agent contact, unless explicitly allowed (offline/CI).

    Returns True when the placeholder was allowed: the deployment must then be offline.
    """
    if not settings.user_agent_is_placeholder():
        return False
    if not allow_placeholder:
        raise SystemExit(PLACEHOLDER_CONTACT_ERROR)
    logger.warning(
        "USER_AGENT holds the placeholder contact (--allow-placeholder-contact): "
        "deploying offline, without the services that call Wikimedia"
    )
    return True


def secret(settings: Settings, allow_placeholder: bool = False) -> None:
    check_contact(settings, allow_placeholder)
    ensure_namespace()
    # Through stdin: the values never touch the disk or show up in a process list.
    run(kubectl("apply", "-f", "-"), stdin=json.dumps(secret_manifest(settings)))


# --- the Helm release ---


def values_file(name: str) -> Path:
    """A values file: one of the chart's (`values-prod.yaml`), or any other path."""
    in_chart = CHART / name
    return in_chart if in_chart.is_file() else Path(name)


def deploy_command(
    tag: str, values: list[str], sets: list[str] | None = None, offline: bool = False
) -> list[str]:
    """`helm upgrade --install`: installs the release the first time, upgrades it after.

    Values are merged in order: the chart's values.yaml, each `values` file, the offline
    overlay, then every `--set`. `--wait` returns once the pods are ready (or fails).
    """
    files = [values_file(name) for name in values]
    if offline:
        files.append(CHART / OFFLINE_VALUES)
    command = helm("upgrade", "--install", RELEASE, str(CHART))
    for path in files:
        command += ["--values", str(path)]
    command += ["--set", f"image.tag={tag}"]
    for assignment in sets or []:
        command += ["--set", assignment]
    return command + ["--wait", "--timeout", DEPLOY_TIMEOUT]


def deploy(
    tag: str, values: list[str], sets: list[str] | None = None, offline: bool = False
) -> None:
    ensure_namespace()
    run(deploy_command(tag, values, sets, offline))
    print(run(helm("status", RELEASE), capture=True))


def history() -> None:
    print(run(helm("history", RELEASE), capture=True))


def rollback(revision: int | None = None) -> None:
    """Back to `revision`, or to the one before the current. A rollback is a new revision."""
    target = [str(revision)] if revision else []
    run(helm("rollback", RELEASE, *target, "--wait", "--timeout", DEPLOY_TIMEOUT))
    history()


def status() -> None:
    print(run(kubectl("-n", NAMESPACE, "get", "pods", "-o", "wide"), capture=True))


def down() -> None:
    run(["kind", "delete", "cluster", "--name", CLUSTER])


COMMANDS = (
    "up", "create", "build", "load", "secret", "deploy", "status", "history", "rollback", "down",
)  # fmt: skip


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--tag", help="image tag for load and deploy (default: newest)")
    parser.add_argument(
        "--values",
        action="append",
        help="values file for deploy, repeatable (default: values-dev.yaml)",
    )
    parser.add_argument(
        "--set", action="append", default=[], help="KEY=VALUE for deploy, repeatable"
    )
    parser.add_argument("--revision", type=int, help="revision for rollback (default: previous)")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="deploy without the services that call Wikimedia (adds values-offline.yaml)",
    )
    parser.add_argument(
        "--allow-placeholder-contact",
        action="store_true",
        help="OFFLINE/CI USE ONLY: go on although USER_AGENT still has the placeholder "
        "contact; implies --offline",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging()
    values = args.values or list(DEFAULT_VALUES)
    if args.command in ("up", "deploy"):
        # Before any slow step: refuse, or force offline, with a placeholder contact.
        offline = check_contact(get_settings(), args.allow_placeholder_contact) or args.offline
        if args.command == "up":
            create()
            tag = build()
            load(tag)
            secret(get_settings(), args.allow_placeholder_contact)
        else:
            tag = args.tag or newest_image_tag()
        deploy(tag, values, args.set, offline)
        status()
    elif args.command == "create":
        create()
    elif args.command == "build":
        build()
    elif args.command == "load":
        load(args.tag or newest_image_tag())
    elif args.command == "secret":
        secret(get_settings(), args.allow_placeholder_contact)
    elif args.command == "status":
        status()
    elif args.command == "history":
        history()
    elif args.command == "rollback":
        rollback(args.revision)
    elif args.command == "down":
        down()
    return 0


if __name__ == "__main__":
    sys.exit(main())
