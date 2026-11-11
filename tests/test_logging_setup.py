"""Structured logging (spec 09b): JSON lines, context fields, the service name."""

import asyncio
import json
import logging

import pytest

from wikipulse.config import Settings
from wikipulse.logging_setup import (
    ContextFilter,
    JsonFormatter,
    TextFormatter,
    build_handler,
    current_context,
    log_context,
    service_name,
)


class ListHandler(logging.Handler):
    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__()
        self.setFormatter(formatter)
        self.addFilter(ContextFilter("detector"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def json_lines() -> list[str]:
    logger = logging.getLogger("test.structured")
    handler = ListHandler(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield handler.lines
    logger.removeHandler(handler)


def test_a_json_line_has_the_standard_fields(json_lines: list[str]) -> None:
    logging.getLogger("test.structured").info("Spike started: %s", "Foo")
    line = json.loads(json_lines[0])
    assert line["level"] == "INFO"
    assert line["service"] == "detector"
    assert line["logger"] == "test.structured"
    assert line["message"] == "Spike started: Foo"
    assert line["time"].endswith("Z")
    assert "correlation_id" not in line


def test_context_fields_and_extra_reach_the_line(json_lines: list[str]) -> None:
    logger = logging.getLogger("test.structured")
    with log_context(correlation_id="edit-1", event_key="replay:3", spike_id=None):
        logger.info("one", extra={"spike_id": 42})
        logger.info("two", extra={"correlation_id": "other"})  # `extra` wins
    logger.info("three")
    one, two, three = (json.loads(line) for line in json_lines)
    assert one["correlation_id"] == "edit-1" and one["event_key"] == "replay:3"
    assert one["spike_id"] == 42
    assert two["correlation_id"] == "other"
    assert "correlation_id" not in three and current_context() == {}


def test_none_fields_are_left_out(json_lines: list[str]) -> None:
    logging.getLogger("test.structured").info("x", extra={"correlation_id": None})
    assert "correlation_id" not in json.loads(json_lines[0])


def test_exceptions_become_a_field(json_lines: list[str]) -> None:
    try:
        raise ValueError("broken")
    except ValueError:
        logging.getLogger("test.structured").exception("failed")
    line = json.loads(json_lines[0])
    assert "ValueError: broken" in line["exception"]


def test_the_context_follows_into_worker_threads(json_lines: list[str]) -> None:
    logger = logging.getLogger("test.structured")

    async def main() -> None:
        with log_context(correlation_id="edit-2"):
            await asyncio.to_thread(logger.info, "in a thread")

    asyncio.run(main())
    assert json.loads(json_lines[0])["correlation_id"] == "edit-2"


def test_nested_contexts_add_and_restore() -> None:
    with log_context(correlation_id="a"):
        with log_context(event_key="r:1"):
            assert current_context() == {"correlation_id": "a", "event_key": "r:1"}
        assert current_context() == {"correlation_id": "a"}


def test_the_text_format_appends_the_fields() -> None:
    record = logging.LogRecord("x", logging.INFO, "", 0, "Event opened", None, None)
    record.correlation_id = "edit-3"
    ContextFilter("grouper").filter(record)
    line = TextFormatter().format(record)
    assert line.endswith("x: Event opened [correlation_id=edit-3]")


def test_service_name_from_the_setting_or_the_started_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert service_name(Settings(_env_file=None, log_service="detector")) == "detector"

    class Spec:
        name = "wikipulse.detect.run"

    class Main:
        __spec__ = Spec()

    monkeypatch.setitem(__import__("sys").modules, "__main__", Main())
    assert service_name(Settings(_env_file=None)) == "detect.run"
    Spec.name = "wikipulse.api.__main__"
    assert service_name(Settings(_env_file=None)) == "api"


def test_log_format_picks_the_formatter() -> None:
    json_handler = build_handler(Settings(_env_file=None, log_format="json"))
    text_handler = build_handler(Settings(_env_file=None))
    assert isinstance(json_handler.formatter, JsonFormatter)
    assert isinstance(text_handler.formatter, TextFormatter)
