"""Parsing the object store's S3 event notifications."""

import json

import pytest

from tests.workers.helpers import s3_message
from wikipulse.workers.notifications import ObjectRef, parse_message

KEY = "edits/year=2026/month=11/day=07/hour=13/part-0.parquet"


def test_object_created_record_is_decoded() -> None:
    [ref] = parse_message(
        s3_message("wikipulse-lake", KEY, "s3:ObjectCreated:CompleteMultipartUpload")
    )
    assert ref.bucket == "wikipulse-lake"
    assert ref.key == KEY  # '%2F' and '%3D' decoded
    assert ref.event_name == "s3:ObjectCreated:CompleteMultipartUpload"
    assert ref.size == 492 and ref.etag == "e7d5-1"
    assert ref.event_time == "2026-11-09T15:56:12.375Z"


def test_other_event_types_are_dropped() -> None:
    assert parse_message(s3_message("b", KEY, "s3:ObjectRemoved:Delete")) == []


def test_malformed_records_are_skipped_but_good_ones_kept() -> None:
    good = json.loads(s3_message("b", KEY))["Records"][0]
    message = json.dumps({"Records": [{"eventName": "s3:ObjectCreated:Put"}, good]})
    assert [ref.key for ref in parse_message(message)] == [KEY]


@pytest.mark.parametrize("data", ["not json", "[]", '{"EventName": "x"}', b"\xff"])
def test_messages_that_are_not_notifications_raise(data) -> None:
    with pytest.raises(ValueError):
        parse_message(data)


def test_object_ref_round_trip() -> None:
    ref = ObjectRef("b", KEY, size=3)
    assert ObjectRef.from_dict({**ref.to_dict(), "unknown": 1}) == ref
