"""Object-created notifications from the object store, as small Python objects.

When a file is written to the bucket, the object store publishes a JSON message in the
format AWS S3 uses for its event notifications (the same JSON a Lambda function gets):

    {"EventName": "s3:ObjectCreated:Put", "Key": "<bucket>/<key>",
     "Records": [{"eventName": "s3:ObjectCreated:Put", "eventTime": "...",
                  "s3": {"bucket": {"name": "wikipulse-lake"},
                         "object": {"key": "edits%2Fyear%3D2026...", "size": 492,
                                    "eTag": "..."}}}]}

Object keys inside the records are URL-encoded ('=' is '%3D', '/' may be '%2F'), so
they are decoded here once, and the rest of the code sees plain keys.
"""

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import unquote_plus

logger = logging.getLogger(__name__)

OBJECT_CREATED = "s3:ObjectCreated:"


@dataclass(frozen=True)
class ObjectRef:
    """One new (or replaced) object: what a handler is invoked with."""

    bucket: str
    key: str
    event_name: str = "s3:ObjectCreated:Put"
    event_time: str | None = None  # when the object store saw the write (its clock)
    size: int | None = None
    etag: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ObjectRef":
        fields = cls.__dataclass_fields__
        return cls(**{name: value for name, value in data.items() if name in fields})


def parse_message(data: str | bytes) -> list[ObjectRef]:
    """The object-created records of one notification message (others are dropped).

    Raises ValueError for a message that isn't an S3 event notification at all.
    """
    try:
        message = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ValueError(f"notification is not JSON: {exc}") from exc
    records = message.get("Records") if isinstance(message, dict) else None
    if not isinstance(records, list):
        raise ValueError("notification has no Records list")
    refs = [_record_to_ref(record) for record in records]
    return [ref for ref in refs if ref is not None]


def _record_to_ref(record: Any) -> ObjectRef | None:
    try:
        name = record["eventName"]
        bucket = record["s3"]["bucket"]["name"]
        obj = record["s3"]["object"]
        key = unquote_plus(obj["key"])
    except (KeyError, TypeError):
        logger.warning("Skipping a malformed notification record: %r", record)
        return None
    if not str(name).startswith(OBJECT_CREATED):
        logger.debug("Ignoring %s for %s/%s", name, bucket, key)
        return None
    size = obj.get("size")
    return ObjectRef(
        bucket=bucket,
        key=key,
        event_name=name,
        event_time=record.get("eventTime"),
        size=int(size) if size is not None else None,
        etag=obj.get("eTag"),
    )
