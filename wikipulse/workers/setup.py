"""Set up lake notifications: `uv run python -m wikipulse.workers.setup`.

After this, the object store publishes an "object created" event on the Redis pub/sub
channel LAKE_NOTIFY_CHANNEL for every Parquet file written to S3_BUCKET, and
`uv run python -m wikipulse.workers.run` reacts to it. Safe to run again any time.

Three steps, each idempotent:

1. Turn on the object store's notification module (RustFS ships with it off).
2. Define a notification *target*: "publish to Redis at LAKE_NOTIFY_REDIS_URL on
   channel LAKE_NOTIFY_CHANNEL", named LAKE_NOTIFY_TARGET. The store gives it an ARN
   (Amazon Resource Name), e.g. arn:rustfs:sqs:us-east-1:wikipulse:redis.
3. Give the bucket a *notification configuration*: "on s3:ObjectCreated:* for keys
   ending in .parquet, send the event to that ARN". This is the standard S3 API call
   (PutBucketNotificationConfiguration), the same one that connects a bucket to SQS,
   SNS or Lambda on AWS.

Steps 1 and 2 use RustFS's admin API (`/rustfs/admin/v3/...`): every S3 server has
its own way to define targets (MinIO: `mc admin config set ... notify_redis`; AWS: an
SQS queue or Lambda function with a resource policy). Step 3 is plain S3. The bucket's
notification configuration is replaced as a whole: this script owns it.
"""

import argparse
import json
import logging
import sys
import time
from xml.sax.saxutils import escape

from wikipulse.config import Settings, get_settings
from wikipulse.logging_setup import setup_logging
from wikipulse.workers.s3_api import S3Api, S3ApiError

logger = logging.getLogger(__name__)

ADMIN = "/rustfs/admin/v3"
TARGET_TYPE = "notify_redis"
CONFIG_ID = "wikipulse-hourly-stats"
EVENTS = ("s3:ObjectCreated:*",)
SUFFIX = ".parquet"


def enable_notify_module(api: S3Api) -> bool:
    """Turn notifications on (leaving audit logging as it is). True if it was off."""
    switches = api.request("GET", f"{ADMIN}/module-switches").json()
    if switches.get("notify_enabled"):
        return False
    body = {"notify_enabled": True, "audit_enabled": bool(switches.get("audit_enabled"))}
    api.request("PUT", f"{ADMIN}/module-switches", body=json.dumps(body).encode())
    return True


def put_redis_target(api: S3Api, name: str, redis_url: str, channel: str) -> None:
    """Create or update the target that publishes events to a Redis channel."""
    body = {
        "key_values": [
            {"key": "url", "value": redis_url},
            {"key": "channel", "value": channel},
        ]
    }
    api.request("PUT", f"{ADMIN}/target/{TARGET_TYPE}/{name}", body=json.dumps(body).encode())


def target_arn(api: S3Api, name: str, attempts: int = 10, wait_s: float = 1.0) -> str:
    """The ARN of the target, once it is online (it must reach Redis first)."""
    suffix = f":{name}:redis"
    for attempt in range(attempts):
        arns = api.request("GET", f"{ADMIN}/target/arns").json() or []
        for arn in arns:
            if arn.endswith(suffix):
                return arn
        if attempt < attempts - 1:
            time.sleep(wait_s)
    raise RuntimeError(
        f"notification target {name!r} is not online; can the object store reach Redis? "
        "(LAKE_NOTIFY_REDIS_URL is the address *from inside* the object store's container)"
    )


def ensure_bucket(api: S3Api, bucket: str) -> bool:
    """Create the bucket if it doesn't exist. True if it was created."""
    try:
        api.request("HEAD", f"/{bucket}")
        return False
    except S3ApiError:
        api.request("PUT", f"/{bucket}")
        return True


def notification_xml(arn: str, config_id: str = CONFIG_ID, suffix: str = SUFFIX) -> str:
    """The S3 NotificationConfiguration document: ObjectCreated of '*<suffix>' -> arn."""
    events = "".join(f"<Event>{escape(event)}</Event>" for event in EVENTS)
    return (
        '<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        "<QueueConfiguration>"
        f"<Id>{escape(config_id)}</Id><Queue>{escape(arn)}</Queue>{events}"
        "<Filter><S3Key><FilterRule><Name>suffix</Name>"
        f"<Value>{escape(suffix)}</Value></FilterRule></S3Key></Filter>"
        "</QueueConfiguration></NotificationConfiguration>"
    )


def put_bucket_notification(api: S3Api, bucket: str, arn: str) -> None:
    api.request(
        "PUT", f"/{bucket}", params={"notification": ""}, body=notification_xml(arn).encode()
    )


def get_bucket_notification(api: S3Api, bucket: str) -> str:
    return api.request("GET", f"/{bucket}", params={"notification": ""}).text


def setup_notifications(
    settings: Settings, bucket: str | None = None, api: S3Api | None = None
) -> str:
    """Run all three steps for `bucket` (default S3_BUCKET). Returns the target's ARN."""
    bucket = bucket or settings.s3_bucket
    own_api = api is None
    api = api or S3Api(settings)
    try:
        if enable_notify_module(api):
            logger.info("Turned on the object store's notification module")
        put_redis_target(
            api,
            settings.lake_notify_target,
            settings.lake_notify_redis_url,
            settings.lake_notify_channel,
        )
        arn = target_arn(api, settings.lake_notify_target)
        if ensure_bucket(api, bucket):
            logger.info("Created bucket %s", bucket)
        put_bucket_notification(api, bucket, arn)
        logger.info(
            "Bucket %s now sends %s events for *%s to %s (Redis channel %s)",
            bucket,
            ", ".join(EVENTS),
            SUFFIX,
            arn,
            settings.lake_notify_channel,
        )
        return arn
    finally:
        if own_api:
            api.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", help="bucket to configure (default: S3_BUCKET)")
    parser.add_argument(
        "--show", action="store_true", help="only print the bucket's notification configuration"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging()
    if args.show:
        api = S3Api(settings)
        try:
            print(get_bucket_notification(api, args.bucket or settings.s3_bucket))
        finally:
            api.close()
        return 0
    arn = setup_notifications(settings, args.bucket)
    print(f"Notifications set up: {args.bucket or settings.s3_bucket} -> {arn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
