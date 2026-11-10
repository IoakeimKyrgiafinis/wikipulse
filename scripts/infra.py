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
image_tag to the newest wikipulse image (build one with `scripts/cluster.py build`).

`offline` stays your choice (`-var offline=true`): Terraform itself refuses the
placeholder contact in USER_AGENT unless the release is offline.
"""

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
STACKS = ("storage", "cluster")
# Terraform commands that read the variables (init, fmt and validate don't need them).
COMMANDS_WITH_VARIABLES = ("plan", "apply", "destroy", "refresh", "import", "console", "output")


def s3_server(endpoint: str) -> str:
    """`host:port` of an S3 endpoint URL, as the MinIO provider wants it."""
    parts = urlsplit(endpoint)
    return parts.netloc or endpoint


def stack_variables(stack: str, settings: Settings) -> dict[str, str]:
    """The stack's variables from the settings, without the TF_VAR_ prefix."""
    s3_keys = {
        "s3_access_key": settings.s3_access_key.get_secret_value(),
        "s3_secret_key": settings.s3_secret_key.get_secret_value(),
    }
    if stack == "storage":
        return {
            "s3_server": s3_server(settings.s3_endpoint),
            "s3_region": settings.s3_region,
            **s3_keys,
        }
    return {
        "postgres_dsn": cluster.cluster_dsn(settings.postgres_dsn),
        "neo4j_password": settings.neo4j_password.get_secret_value(),
        "user_agent": settings.user_agent,
        **s3_keys,
    }


def terraform_env(variables: dict[str, str], environ: dict[str, str]) -> dict[str, str]:
    """`environ` plus TF_VAR_<name> for each variable it doesn't set already."""
    env = dict(environ)
    for name, value in variables.items():
        env.setdefault(f"TF_VAR_{name}", value)
    return env


def terraform_command(stack: str, args: list[str]) -> list[str]:
    return ["terraform", f"-chdir={INFRA / stack}", *args]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in STACKS:
        print(__doc__)
        return 2
    setup_logging()
    stack, args = argv[0], argv[1:]
    env = dict(os.environ)
    if args and args[0] in COMMANDS_WITH_VARIABLES:
        variables = stack_variables(stack, get_settings())
        if stack == "cluster" and "TF_VAR_image_tag" not in env:
            variables["image_tag"] = cluster.newest_image_tag()
            logger.info("image_tag = %s (the newest wikipulse image)", variables["image_tag"])
        env = terraform_env(variables, env)
    command = terraform_command(stack, args)
    logger.info("$ %s", " ".join(command))
    return subprocess.run(command, env=env, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
