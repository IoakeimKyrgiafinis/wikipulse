"""Which model version is in use: registry aliases, promotion and rollback.

    uv run python -m wikipulse.ml.registry show              # versions, aliases, status
    uv run python -m wikipulse.ml.registry promote 3         # make version 3 the champion
    uv run python -m wikipulse.ml.registry rollback          # back to the previous champion

**Aliases.** A registered model has numbered versions (spec 07b registers them). An
*alias* is a movable name for one of them: `models:/news-classifier@champion` is the
version that serves (`wikipulse.ml.serve` loads it), `@challenger` the newest version
from retraining, under evaluation or just rejected. Moving an alias changes which model
serves without touching any model file, so promoting and rolling back are instant and
reversible.

**History.** When a version becomes champion, the version it replaces is written into
its tag `previous_champion`; `rollback` follows that tag. Each version's tag `status`
says where it stands in the lifecycle: `challenger`, `rejected`, `champion`, `retired`
(replaced by a better one) or `rolled_back`. (Versions 1 to 3 were tagged `latest` and
`superseded` by spec 07b's training sessions; the first promotion here moved version 3
to `champion`.)
"""

import argparse
import logging
import sys
from dataclasses import dataclass

import mlflow
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from wikipulse.config import get_settings
from wikipulse.logging_setup import setup_logging

logger = logging.getLogger(__name__)

CHAMPION = "champion"
CHALLENGER = "challenger"
# Lifecycle states (tag `status`).
STATUS_CHALLENGER = "challenger"
STATUS_REJECTED = "rejected"
STATUS_CHAMPION = "champion"
STATUS_RETIRED = "retired"
STATUS_ROLLED_BACK = "rolled_back"
# Never rolled back *to*: they lost a comparison or were taken out of service.
NOT_FOR_ROLLBACK = {STATUS_REJECTED, STATUS_ROLLED_BACK, STATUS_CHALLENGER}


class RegistryError(RuntimeError):
    """A registry change that can't be made (no champion, unknown version, ...)."""


def alias_version(client: MlflowClient, name: str, alias: str) -> str | None:
    """The version an alias points at, or None if the alias isn't set."""
    try:
        return str(client.get_model_version_by_alias(name, alias).version)
    except MlflowException:
        return None


def set_status(client: MlflowClient, name: str, version: str, status: str, **tags: str) -> None:
    client.set_model_version_tag(name, version, "status", status)
    for key, value in tags.items():
        client.set_model_version_tag(name, version, key, value)


@dataclass(frozen=True)
class AliasChange:
    """What a promotion or rollback did: the champion before and after."""

    before: str | None
    after: str


def promote(client: MlflowClient, name: str, version: str, reason: str) -> AliasChange:
    """Point `@champion` at `version`.

    The old champion becomes `retired` and is remembered as `previous_champion` on the
    new one (what `rollback` returns to). If `version` was the challenger, the challenger
    alias is removed: it is the champion now. Promoting the current champion again
    changes nothing."""
    version = str(client.get_model_version(name, version).version)  # raises if unknown
    before = alias_version(client, name, CHAMPION)
    if before == version:
        logger.info("%s version %s is already the champion", name, version)
        return AliasChange(before, version)
    client.set_registered_model_alias(name, CHAMPION, version)
    tags = {"promotion_reason": reason}
    if before is not None:
        tags["previous_champion"] = before
        set_status(client, name, before, STATUS_RETIRED, retired_by=version)
    set_status(client, name, version, STATUS_CHAMPION, **tags)
    if alias_version(client, name, CHALLENGER) == version:
        client.delete_registered_model_alias(name, CHALLENGER)
    logger.info("%s: champion %s -> %s (%s)", name, before, version, reason)
    return AliasChange(before, version)


def rollback_target(client: MlflowClient, name: str, current: str) -> str | None:
    """The version to roll back to from `current`: its `previous_champion` tag, else the
    newest older version that was never rejected or rolled back."""
    previous = client.get_model_version(name, current).tags.get("previous_champion")
    if previous:
        return previous
    older = [
        v
        for v in client.search_model_versions(f"name = '{name}'")
        if int(v.version) < int(current) and v.tags.get("status") not in NOT_FOR_ROLLBACK
    ]
    return str(max(older, key=lambda v: int(v.version)).version) if older else None


def rollback(client: MlflowClient, name: str, reason: str = "rollback") -> AliasChange:
    """Point `@champion` back at the previous champion; the current one is marked
    `rolled_back` (and is never chosen as a rollback target again)."""
    current = alias_version(client, name, CHAMPION)
    if current is None:
        raise RegistryError(f"{name} has no champion to roll back from")
    target = rollback_target(client, name, current)
    if target is None:
        raise RegistryError(f"{name} version {current} has no earlier version to roll back to")
    client.set_registered_model_alias(name, CHAMPION, target)
    set_status(client, name, current, STATUS_ROLLED_BACK, rolled_back_to=target)
    set_status(client, name, target, STATUS_CHAMPION, promotion_reason=reason)
    logger.info("%s: rolled back from version %s to %s", name, current, target)
    return AliasChange(current, target)


def describe(client: MlflowClient, name: str) -> str:
    """One line per version, newest first, with its aliases and lifecycle tags."""
    model = client.get_registered_model(name)
    aliases: dict[str, list[str]] = {}
    for alias, version in (model.aliases or {}).items():
        aliases.setdefault(str(version), []).append(alias)
    lines = [f"{name}"]
    versions = client.search_model_versions(f"name = '{name}'")
    for v in sorted(versions, key=lambda v: -int(v.version)):
        tags = v.tags
        marks = ",".join(f"@{a}" for a in sorted(aliases.get(str(v.version), [])))
        lines.append(
            f"  v{v.version:<3} {marks:<12} status={tags.get('status', '?'):<11} "
            f"model={tags.get('model_type', '?'):<6} dataset={tags.get('dataset_version', '?'):<4} "
            f"test_AP={tags.get('test_average_precision', '?')}"
            + (
                f"  previous_champion={tags['previous_champion']}"
                if "previous_champion" in tags
                else ""
            )
        )
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-name", help="registered model (default ML_MODEL_NAME)")
    parser.add_argument("--tracking-uri", help="MLflow tracking URI (default MLFLOW_TRACKING_URI)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="list the versions with their aliases and status")
    up = commands.add_parser("promote", help="make a version the champion (roll forward)")
    up.add_argument("version", help="the version number")
    up.add_argument("--reason", default="promoted by hand", help="stored on the version")
    back = commands.add_parser("rollback", help="point the champion back at the previous one")
    back.add_argument("--reason", default="rolled back by hand", help="stored on the version")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level)
    mlflow.set_tracking_uri(args.tracking_uri or settings.mlflow_tracking_uri)
    client = MlflowClient()
    name = args.model_name or settings.ml_model_name
    try:
        if args.command == "promote":
            change = promote(client, name, args.version, args.reason)
            print(f"Champion: version {change.before} -> {change.after}")
        elif args.command == "rollback":
            change = rollback(client, name, args.reason)
            print(f"Rolled back: version {change.before} -> {change.after}")
    except RegistryError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(describe(client, name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
