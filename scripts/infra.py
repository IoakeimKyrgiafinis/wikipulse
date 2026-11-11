"""Run Terraform on a stack in infra/ with the secrets from your settings (spec 08c).

    uv run python scripts/infra.py <stack> <terraform arguments...>

Stacks:
  storage   the buckets of the S3 store (RustFS in Compose); its own state, kept for good
  cluster   the kind cluster, namespace, Secret, image and Helm release; destroy freely

Examples:
  uv run python scripts/infra.py cluster init
  uv run python scripts/infra.py cluster plan -var offline=true
  uv run python scripts/infra.py cluster apply -var offline=true
  uv run python scripts/infra.py cluster destroy -var offline=true

Terraform reads every variable named TF_VAR_<name> from the environment. This script
sets those from wikipulse/config.py's settings (.env and the defaults) for the one
Terraform process it starts, so the passwords never land in a file or on the command
line. A TF_VAR_ variable you set yourself wins. For the cluster stack it also sets
image_tag: the image of the current commit if built (`scripts/cluster.py build`),
otherwise the newest wikipulse image; and chart_checksum, a hash of charts/wikipulse, so
an edited template is deployed even when the chart's version stays the same. For the
storage stack, s3_ssl follows S3_ENDPOINT's scheme.

`offline` stays your choice (`-var offline=true`): Terraform itself refuses the
placeholder contact in USER_AGENT unless the release is offline.
"""

import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # so `wikipulse` imports when run as a script
sys.path.insert(0, str(ROOT / "scripts"))  # and cluster.py, for the DSN and image tag

import cluster  # noqa: E402

from wikipulse.config import Settings, get_settings  # noqa: E402
from wikipulse.logging_setup import setup_logging  # noqa: E402

logger = logging.getLogger("infra")

INFRA = ROOT / "infra"
CHART = ROOT / "charts" / "wikipulse"
STACKS = ("storage", "cluster")
TF_VAR = "TF_VAR_"
# Terraform commands that read the input variables (init, fmt, validate and output don't).
COMMANDS_WITH_VARIABLES = ("plan", "apply", "destroy", "refresh", "import", "console")


def s3_server(endpoint: str) -> str:
    """`host:port` of an S3 endpoint URL, as the MinIO provider wants it."""
    parts = urlsplit(endpoint)
    return parts.netloc or endpoint


def s3_ssl(endpoint: str) -> bool:
    """Whether the S3 endpoint uses HTTPS (the scheme s3_server() drops)."""
    return urlsplit(endpoint).scheme.lower() == "https"


def chart_checksum(chart: Path = CHART) -> str:
    """SHA-256 of every file in the chart: their paths relative to it and their contents.

    The Helm provider only notices a changed chart version or values, so a template edit
    would never be deployed; this checksum, passed in as a value, changes with any edit.
    Line endings are normalized, so a CRLF checkout gives the same checksum.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in chart.rglob("*") if p.is_file()):
        digest.update(path.relative_to(chart).as_posix().encode("utf-8") + b"\x00")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\x00")
    return digest.hexdigest()


def langfuse_keys(settings: Settings) -> dict[str, str]:
    """The Langfuse keys (spec 09b), "" when unset: then the pods trace nothing."""
    keys = {
        "langfuse_public_key": settings.langfuse_public_key,
        "langfuse_secret_key": settings.langfuse_secret_key,
    }
    return {name: key.get_secret_value() if key else "" for name, key in keys.items()}


def stack_variables(stack: str, settings: Settings) -> dict[str, str]:
    """The stack's variables from the settings, without the TF_VAR_ prefix."""
    s3_keys = {
        "s3_access_key": settings.s3_access_key.get_secret_value(),
        "s3_secret_key": settings.s3_secret_key.get_secret_value(),
    }
    if stack == "storage":
        return {
            "s3_server": s3_server(settings.s3_endpoint),
            "s3_ssl": str(s3_ssl(settings.s3_endpoint)).lower(),
            "s3_region": settings.s3_region,
            **s3_keys,
        }
    return {
        "postgres_dsn": cluster.cluster_dsn(settings.postgres_dsn),
        "neo4j_password": settings.neo4j_password.get_secret_value(),
        "user_agent": settings.user_agent,
        **langfuse_keys(settings),
        "chart_checksum": chart_checksum(),
        **s3_keys,
    }


def terraform_env(variables: dict[str, str], environ: dict[str, str]) -> dict[str, str]:
    """`environ` plus TF_VAR_<name> for each variable it doesn't set already.

    Terraform matches the name after TF_VAR_ case-sensitively, but Python on Windows
    upper-cases every environment name (`TF_VAR_image_tag` arrives as `TF_VAR_IMAGE_TAG`).
    The stacks' variable names are all lower case, so the names are lower-cased back.
    """
    env = {}
    for key, value in environ.items():
        if key.upper().startswith(TF_VAR):
            key = TF_VAR + key[len(TF_VAR) :].lower()
        env[key] = value
    for name, value in variables.items():
        env.setdefault(TF_VAR + name, value)
    return env


def default_image_tag() -> str:
    """The image of the current commit if it was built, else the newest image."""
    commit = cluster.run(["git", "rev-parse", "--short", "HEAD"], capture=True).strip()
    built = cluster.run(["docker", "image", "ls", f"{cluster.IMAGE}:{commit}", "-q"], capture=True)
    return commit if built.strip() else cluster.newest_image_tag()


def terraform_command(stack: str, args: list[str]) -> list[str]:
    return ["terraform", f"-chdir={INFRA / stack}", *args]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in STACKS:
        print(__doc__)
        return 2
    setup_logging()
    stack, args = argv[0], argv[1:]
    env = terraform_env({}, dict(os.environ))
    if args and args[0] in COMMANDS_WITH_VARIABLES:
        variables = stack_variables(stack, get_settings())
        if stack == "cluster" and TF_VAR + "image_tag" not in env:
            variables["image_tag"] = default_image_tag()
            logger.info("image_tag = %s", variables["image_tag"])
        env = terraform_env(variables, env)
    command = terraform_command(stack, args)
    logger.info("$ %s", " ".join(command))
    return subprocess.run(command, env=env, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
