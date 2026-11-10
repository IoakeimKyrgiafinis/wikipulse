"""WikiPulse on a local Kubernetes cluster (kind): `uv run python scripts/cluster.py <command>`.

Commands:
  up       everything below in order: create, build, load, secret, migrate, apply, wait
  create   create the kind cluster `wikipulse` (one node) if it doesn't exist
  build    build the image `wikipulse:<tag>` from the Dockerfile
  load     copy the image into the cluster's node (kind has no registry to pull from)
  secret   create or update the Secret `wikipulse-secrets` from your settings
  migrate  run the database migrations as a Job and wait for it
  apply    apply the manifests of k8s/ with the image tag, then wait for the rollouts
  status   show the pods
  down     delete the cluster (the data in Redis, Postgres, Neo4j and S3 stays in Compose)

The tag is the git commit (`3f2a1b9`), plus `-dirty-<time>` when there are uncommitted
changes, so every build has a new tag and a rollout always means a new image. `load`,
`migrate` and `apply` use the newest image built (`--tag` picks another).

Every kubectl call names the context `kind-wikipulse`, so this script never touches
another cluster. Needs Docker Desktop, kind and kubectl.

`up` and `secret` refuse to run while USER_AGENT still holds the placeholder contact:
the cluster's consumer connects to Wikimedia, which requires a real contact address.
Set yours in .env. `--allow-placeholder-contact` skips the check, for offline or CI
use only (a cluster whose consumer never reaches Wikimedia).
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
MANIFESTS = ROOT / "k8s"
KIND_CONFIG = MANIFESTS / "kind-config.yaml"
MIGRATE_JOB = MANIFESTS / "migrate-job.yaml"
SECRET_NAME = "wikipulse-secrets"
DEPLOYMENTS = ("consumer", "detector", "archiver", "enricher", "grouper", "graph", "api")
ROLLOUT_TIMEOUT = "900s"  # the first grouper start downloads the embedding model


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


def with_image_tag(manifests: str, tag: str) -> str:
    """Point every `image: wikipulse:<anything>` of rendered manifests at `tag`."""
    lines = []
    for line in manifests.splitlines():
        key, _, value = line.partition("image: ")
        if value.startswith(f"{IMAGE}:") and not key.strip("- "):
            line = f"{key}image: {IMAGE}:{tag}"
        lines.append(line)
    return "\n".join(lines) + "\n"


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


# What the migration Job needs before the services exist: its namespace, the settings,
# the names of the Compose services, and the network policies (which also let it out).
BASE_MANIFESTS = (
    "namespace.yaml",
    "configmap.yaml",
    "external-services.yaml",
    "network-policies.yaml",
)


def ensure_namespace() -> None:
    run(kubectl("apply", "-f", str(MANIFESTS / "namespace.yaml")))


def apply_base() -> None:
    files = [arg for name in BASE_MANIFESTS for arg in ("-f", str(MANIFESTS / name))]
    run(kubectl("apply", *files))


PLACEHOLDER_CONTACT_ERROR = (
    "USER_AGENT still contains the placeholder contact (e.g. you@example.com). The "
    "cluster's consumer connects to Wikimedia, whose policy requires a real way to reach "
    "you: set USER_AGENT in .env to your own contact address and run this again. "
    "(--allow-placeholder-contact skips this check, for offline or CI use only.)"
)


def check_contact(settings: Settings, allow_placeholder: bool = False) -> None:
    """Refuse a placeholder User-Agent contact, unless explicitly allowed (offline/CI)."""
    if not settings.user_agent_is_placeholder():
        return
    if not allow_placeholder:
        raise SystemExit(PLACEHOLDER_CONTACT_ERROR)
    logger.warning(
        "USER_AGENT holds the placeholder contact (--allow-placeholder-contact): "
        "don't let this cluster's consumer connect to Wikimedia"
    )


def secret(settings: Settings, allow_placeholder: bool = False) -> None:
    check_contact(settings, allow_placeholder)
    ensure_namespace()
    # Through stdin: the values never touch the disk or show up in a process list.
    run(kubectl("apply", "-f", "-"), stdin=json.dumps(secret_manifest(settings)))


def migrate(tag: str) -> None:
    apply_base()
    run(kubectl("-n", NAMESPACE, "delete", "job", "migrate", "--ignore-not-found"))
    job = with_image_tag(MIGRATE_JOB.read_text(encoding="utf-8"), tag)
    run(kubectl("apply", "-f", "-"), stdin=job)
    run(
        kubectl(
            "-n", NAMESPACE, "wait", "--for=condition=complete", "job/migrate", "--timeout=180s"
        )
    )
    print(run(kubectl("-n", NAMESPACE, "logs", "job/migrate"), capture=True))


def apply(tag: str) -> None:
    rendered = run(kubectl("kustomize", str(MANIFESTS)), capture=True)
    run(kubectl("apply", "-f", "-"), stdin=with_image_tag(rendered, tag))
    for name in DEPLOYMENTS:
        run(
            kubectl(
                "-n", NAMESPACE, "rollout", "status", f"deployment/{name}",
                f"--timeout={ROLLOUT_TIMEOUT}",
            )
        )  # fmt: skip


def status() -> None:
    print(run(kubectl("-n", NAMESPACE, "get", "pods", "-o", "wide"), capture=True))


def down() -> None:
    run(["kind", "delete", "cluster", "--name", CLUSTER])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command",
        choices=["up", "create", "build", "load", "secret", "migrate", "apply", "status", "down"],
    )
    parser.add_argument("--tag", help="image tag for load, migrate and apply (default: newest)")
    parser.add_argument(
        "--allow-placeholder-contact",
        action="store_true",
        help="OFFLINE/CI USE ONLY: create the Secret even though USER_AGENT still has the "
        "placeholder contact (the consumer must then never connect to Wikimedia)",
    )
    args = parser.parse_args(argv)
    setup_logging()
    if args.command == "up":
        check_contact(get_settings(), args.allow_placeholder_contact)  # before any slow step
        create()
        tag = build()
        load(tag)
        secret(get_settings(), args.allow_placeholder_contact)
        migrate(tag)
        apply(tag)
        status()
    elif args.command == "create":
        create()
    elif args.command == "build":
        build()
    elif args.command in ("load", "migrate", "apply"):
        tag = args.tag or newest_image_tag()
        {"load": load, "migrate": migrate, "apply": apply}[args.command](tag)
    elif args.command == "secret":
        secret(get_settings(), args.allow_placeholder_contact)
    elif args.command == "status":
        status()
    elif args.command == "down":
        down()
    return 0


if __name__ == "__main__":
    sys.exit(main())
