"""Data model for one normalized Wikipedia edit."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

COMMENT_MAX_CHARS = 500


class EditEvent(BaseModel):
    """An English Wikipedia article edit, normalized from a raw `recentchange` message."""

    model_config = ConfigDict(frozen=True)

    event_id: str  # meta.id: unique per event, used for deduplication
    event_time: AwareDatetime  # meta.dt: when the edit happened (event time)
    received_at: AwareDatetime  # wall-clock time the consumer processed it (metadata only)
    title: str
    type: Literal["edit", "new"]
    user: str
    is_bot: bool
    is_minor: bool
    rev_old: int | None  # None for new pages
    rev_new: int | None
    len_old: int | None  # None for new pages
    len_new: int | None
    comment: str

    @field_validator("comment")
    @classmethod
    def _truncate_comment(cls, value: str) -> str:
        """Keep at most COMMENT_MAX_CHARS characters, so one huge comment can't bloat Redis."""
        return value[:COMMENT_MAX_CHARS]
