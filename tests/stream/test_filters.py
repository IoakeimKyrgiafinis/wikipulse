"""Filtering, canary handling and normalization, on real recorded messages."""

import copy
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tests.stream.helpers import fixture_raws
from wikipulse.stream.filters import is_canary, is_wanted, normalize
from wikipulse.stream.models import COMMENT_MAX_CHARS

RECEIVED = datetime(2026, 1, 1, tzinfo=UTC)


def first_wanted() -> dict:
    return copy.deepcopy(next(raw for raw in fixture_raws() if is_wanted(raw)))


def test_fixture_has_a_mix_of_kept_and_filtered_messages() -> None:
    raws = fixture_raws()
    wanted = [raw for raw in raws if is_wanted(raw)]
    assert 250 <= len(raws) <= 350
    assert 0 < len(wanted) < len(raws)


def test_kept_messages_are_enwiki_articles_edits_or_new() -> None:
    for raw in fixture_raws():
        expected = (
            raw["wiki"] == "enwiki"
            and raw["namespace"] == 0
            and raw["type"] in ("edit", "new")
            and raw["meta"]["domain"] != "canary"
        )
        assert is_wanted(raw) == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [("wiki", "dewiki"), ("namespace", 1), ("namespace", "0"), ("type", "log")],
)
def test_other_wikis_namespaces_and_types_are_discarded(field: str, value: object) -> None:
    raw = first_wanted()
    raw[field] = value
    assert not is_wanted(raw)


def test_canary_events_are_discarded() -> None:
    raw = first_wanted()
    raw["meta"]["domain"] = "canary"
    assert is_canary(raw)
    assert not is_wanted(raw)


def test_odd_shapes_do_not_crash_the_filter() -> None:
    assert is_wanted({}) is False
    odd = {"wiki": "enwiki", "namespace": 0, "type": "edit", "meta": "not a dict"}
    assert is_wanted(odd) is True  # looks wanted; normalize then rejects it as malformed
    with pytest.raises(TypeError):
        normalize(odd, received_at=RECEIVED)


def test_normalize_edit() -> None:
    raw = first_wanted()
    event = normalize(raw, received_at=RECEIVED)
    assert event.event_id == raw["meta"]["id"]
    assert event.event_time == datetime.fromisoformat(raw["meta"]["dt"])
    assert event.event_time.tzinfo is not None
    assert event.received_at == RECEIVED
    assert event.title == raw["title"]
    assert event.type == raw["type"]
    assert event.user == raw["user"]
    assert event.is_bot == raw["bot"]
    assert event.is_minor == raw["minor"]
    assert (event.rev_old, event.rev_new) == (raw["revision"]["old"], raw["revision"]["new"])
    assert (event.len_old, event.len_new) == (raw["length"]["old"], raw["length"]["new"])


def test_normalize_new_page_has_no_old_revision() -> None:
    raw = first_wanted()
    raw["type"] = "new"
    raw["revision"] = {"new": 123}
    raw["length"] = {"new": 456}
    event = normalize(raw, received_at=RECEIVED)
    assert event.type == "new"
    assert (event.rev_old, event.rev_new, event.len_old, event.len_new) == (None, 123, None, 456)


def test_bot_edits_are_kept_and_flagged() -> None:
    raw = first_wanted()
    raw["bot"] = True
    assert is_wanted(raw)
    assert normalize(raw, received_at=RECEIVED).is_bot


def test_comment_is_truncated() -> None:
    raw = first_wanted()
    raw["comment"] = "x" * 2000
    assert len(normalize(raw, received_at=RECEIVED).comment) == COMMENT_MAX_CHARS


def test_missing_optional_fields_get_defaults() -> None:
    raw = first_wanted()
    for field in ("user", "comment", "minor", "bot", "revision", "length"):
        raw.pop(field, None)
    event = normalize(raw, received_at=RECEIVED)
    assert (event.user, event.comment, event.is_minor, event.is_bot) == ("", "", False, False)


def test_missing_required_field_raises() -> None:
    raw = first_wanted()
    del raw["meta"]["id"]
    with pytest.raises(KeyError):
        normalize(raw, received_at=RECEIVED)


def test_bad_timestamp_raises_validation_error() -> None:
    raw = first_wanted()
    raw["meta"]["dt"] = "not a date"
    with pytest.raises(ValidationError):
        normalize(raw, received_at=RECEIVED)
