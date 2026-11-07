"""Decide which raw `recentchange` messages we keep, and turn them into `EditEvent`s."""

from datetime import UTC, datetime
from typing import Any

from wikipulse.stream.models import EditEvent

WANTED_WIKI = "enwiki"
WANTED_NAMESPACE = 0  # the article namespace
WANTED_TYPES = frozenset({"edit", "new"})
CANARY_DOMAIN = "canary"


def is_canary(raw: dict[str, Any]) -> bool:
    """True for Wikimedia's synthetic test events (meta.domain == "canary")."""
    meta = raw.get("meta")
    return isinstance(meta, dict) and meta.get("domain") == CANARY_DOMAIN


def is_wanted(raw: dict[str, Any]) -> bool:
    """True for edits or page creations of English Wikipedia articles, excluding canaries.

    Never raises: odd shapes simply count as "not wanted" here, and `normalize`
    decides whether a wanted message is well-formed.
    """
    return (
        raw.get("wiki") == WANTED_WIKI
        and raw.get("namespace") == WANTED_NAMESPACE
        and raw.get("type") in WANTED_TYPES
        and not is_canary(raw)
    )


def _nested(raw: dict[str, Any], field: str, key: str) -> Any:
    """raw[field][key], or None if missing (e.g. `revision.old` for a new page)."""
    value = raw.get(field)
    return value.get(key) if isinstance(value, dict) else None


def normalize(raw: dict[str, Any], received_at: datetime | None = None) -> EditEvent:
    """Build an `EditEvent` from a raw message.

    `received_at` is wall-clock metadata (when we processed the message); it defaults
    to now. Processing logic must use `event_time` instead.

    Raises KeyError, TypeError or pydantic.ValidationError when required fields
    (meta.id, meta.dt, title, type) are missing or have the wrong type.
    """
    meta = raw["meta"]
    return EditEvent(
        event_id=meta["id"],
        event_time=meta["dt"],
        received_at=received_at or datetime.now(UTC),
        title=raw["title"],
        type=raw["type"],
        user=raw.get("user") or "",
        is_bot=bool(raw.get("bot", False)),
        is_minor=bool(raw.get("minor", False)),
        rev_old=_nested(raw, "revision", "old"),
        rev_new=_nested(raw, "revision", "new"),
        len_old=_nested(raw, "length", "old"),
        len_new=_nested(raw, "length", "new"),
        comment=raw.get("comment") or "",
    )
