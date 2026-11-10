"""Logging configuration for WikiPulse entry points, and structured log fields (spec 09b).

Every entry point calls `setup_logging()` once. LOG_FORMAT picks the output:

- `text` (the default, for the laptop): one readable line, fields in brackets,
  `2026-11-10 21:04:12,345 INFO     wikipulse.detect.detector: Spike started ... [...]`
- `json` (the cluster): one JSON object per line, which Loki stores and searches,
  `{"time": "...", "level": "INFO", "service": "detector", "logger": "...",
  "message": "Spike started ...", "correlation_id": "..."}`

Fields beyond the message come from two places:

- `extra={...}` on one call: `logger.info("Event opened", extra={"event_key": key})`;
- `log_context(...)` for a block: everything logged inside it, in this task and in
  threads started with `asyncio.to_thread` (they copy the context), gets the fields.

The fields every service uses for the same thing have one name each, so a search for
one value finds every line about it, whatever service wrote it (`FIELDS`).
"""

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from wikipulse.config import Settings, get_settings

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# The shared field names (documentation; any other name works too).
FIELDS = {
    "correlation_id": "the id of the edit that started a spike, carried by its event",
    "spike_id": "the spike's row id in Postgres",
    "event_key": "an event's key, <grouping run>:<number>",
    "run_id": "a detector or grouping run",
    "trace_id": "the Langfuse trace of the LLM calls made in this block",
}

_CONTEXT: ContextVar[Mapping[str, Any] | None] = ContextVar("wikipulse_log_context", default=None)

# Attributes every LogRecord has; anything else on a record came from `extra` or the context.
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "service",
}


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add `fields` to every record logged inside the block (None values are left out)."""
    added = {name: value for name, value in fields.items() if value is not None}
    token = _CONTEXT.set({**(_CONTEXT.get() or {}), **added})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_context() -> dict[str, Any]:
    """The fields `log_context()` has bound here."""
    return dict(_CONTEXT.get() or {})


def service_name(settings: Settings) -> str:
    """LOG_SERVICE, or the module that was started (`detect.run`, `api`, ...)."""
    if settings.log_service:
        return settings.log_service
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    name = getattr(spec, "name", None) or "wikipulse"
    return name.removeprefix("wikipulse.").removesuffix(".__main__")


class ContextFilter(logging.Filter):
    """Puts the service name and the bound context fields on every record."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self.service
        for name, value in (_CONTEXT.get() or {}).items():
            if not hasattr(record, name):  # a field given with `extra` wins
                setattr(record, name, value)
        return True


def record_fields(record: logging.LogRecord) -> dict[str, Any]:
    """The record's structured fields: everything not standard and not None, in order."""
    return {
        name: value
        for name, value in record.__dict__.items()
        if name not in _STANDARD and not name.startswith("_") and value is not None
    }


class JsonFormatter(logging.Formatter):
    """One JSON object per record: time, level, service, logger, message, fields."""

    def format(self, record: logging.LogRecord) -> str:
        moment = datetime.fromtimestamp(record.created, UTC)
        data: dict[str, Any] = {
            "time": moment.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": getattr(record, "service", ""),
            "logger": record.name,
            "message": record.getMessage(),
        }
        data.update(record_fields(record))
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """The readable line, with the structured fields appended in brackets."""

    def __init__(self) -> None:
        super().__init__(LOG_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = record_fields(record)
        if not fields:
            return line
        pairs = " ".join(f"{name}={value}" for name, value in fields.items())
        first, newline, rest = line.partition("\n")  # keep a traceback below the fields
        return f"{first} [{pairs}]{newline}{rest}"


def build_handler(settings: Settings) -> logging.Handler:
    """A handler to stderr with the LOG_FORMAT formatter and the context filter."""
    handler = logging.StreamHandler()
    json_format = settings.log_format == "json"
    handler.setFormatter(JsonFormatter() if json_format else TextFormatter())
    handler.addFilter(ContextFilter(service_name(settings)))
    return handler


def setup_logging(level: str | None = None) -> None:
    """Configure root logging. Uses LOG_LEVEL from settings unless `level` is given."""
    settings = get_settings()
    chosen = (level or settings.log_level).upper()
    logging.basicConfig(level=chosen, handlers=[build_handler(settings)], force=True)
