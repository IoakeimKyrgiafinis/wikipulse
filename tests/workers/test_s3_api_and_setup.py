"""SigV4 signing (checked against AWS's published examples) and the setup steps."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from wikipulse.config import Settings
from wikipulse.workers import setup
from wikipulse.workers.s3_api import S3Api, S3ApiError, sign_headers

# The example credentials of the AWS S3 documentation ("Signature Calculations for the
# Authorization Header: Transferring Payload in a Single Chunk").
EXAMPLE = {
    "access_key": "AKIAIOSFODNN7EXAMPLE",
    "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "region": "us-east-1",
    "now": datetime(2013, 5, 24, tzinfo=UTC),
}
URL = "https://examplebucket.s3.amazonaws.com/"


@pytest.mark.parametrize(
    ("params", "signature"),
    [
        ({"lifecycle": ""}, "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"),
        (
            {"max-keys": "2", "prefix": "J"},
            "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
        ),
    ],
)
def test_signature_matches_the_aws_documentation(params, signature) -> None:
    headers = sign_headers("GET", URL, params=params, **EXAMPLE)
    assert headers["x-amz-date"] == "20130524T000000Z"
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        f"SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={signature}"
    )


class FakeRustFS:
    """Just enough of RustFS's admin API and S3 bucket API for the setup steps."""

    def __init__(self, target_online_after: int = 0) -> None:
        self.requests: list[httpx.Request] = []
        self.notify_enabled = False
        self.targets: dict[str, dict] = {}
        self.buckets: dict[str, str] = {}
        self.arn_polls = 0
        self.target_online_after = target_online_after

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=")
        path, method = request.url.path, request.method
        if path == "/rustfs/admin/v3/module-switches":
            if method == "PUT":
                self.notify_enabled = json.loads(request.content)["notify_enabled"]
            return httpx.Response(200, json={"notify_enabled": self.notify_enabled})
        if path.startswith("/rustfs/admin/v3/target/notify_redis/"):
            values = json.loads(request.content)["key_values"]
            self.targets[path.rsplit("/", 1)[1]] = {kv["key"]: kv["value"] for kv in values}
            return httpx.Response(200)
        if path == "/rustfs/admin/v3/target/arns":
            self.arn_polls += 1
            online = self.arn_polls > self.target_online_after
            arns = [f"arn:rustfs:sqs:us-east-1:{name}:redis" for name in self.targets]
            return httpx.Response(200, json=arns if online else [])
        bucket = path.strip("/")
        if method == "HEAD":
            return httpx.Response(200 if bucket in self.buckets else 404)
        if method == "PUT" and "notification" in request.url.params:
            self.buckets[bucket] = request.content.decode()
            return httpx.Response(200)
        if method == "PUT":
            self.buckets.setdefault(bucket, "")
            return httpx.Response(200)
        return httpx.Response(400, text="unexpected request")


def api_for(fake: FakeRustFS) -> S3Api:
    return S3Api(Settings(_env_file=None), client=httpx.Client(transport=httpx.MockTransport(fake)))


def test_setup_enables_target_and_bucket_notifications(monkeypatch) -> None:
    monkeypatch.setattr(setup.time, "sleep", lambda seconds: None)
    fake = FakeRustFS(target_online_after=2)
    settings = Settings(_env_file=None)
    arn = setup.setup_notifications(settings, "my-bucket", api=api_for(fake))
    assert arn == "arn:rustfs:sqs:us-east-1:wikipulse:redis"
    assert fake.notify_enabled
    assert fake.targets["wikipulse"] == {
        "url": "redis://redis:6379/0",
        "channel": "lake:notifications",
    }
    xml = fake.buckets["my-bucket"]
    assert f"<Queue>{arn}</Queue>" in xml
    assert "<Event>s3:ObjectCreated:*</Event>" in xml
    assert "<Value>.parquet</Value>" in xml

    # Running it again changes nothing and doesn't switch the module again.
    puts = sum(r.method == "PUT" and r.url.path.endswith("module-switches") for r in fake.requests)
    setup.setup_notifications(settings, "my-bucket", api=api_for(fake))
    puts_after = sum(
        r.method == "PUT" and r.url.path.endswith("module-switches") for r in fake.requests
    )
    assert puts == puts_after == 1


def test_a_target_that_never_comes_online_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(setup.time, "sleep", lambda seconds: None)
    fake = FakeRustFS(target_online_after=1000)
    with pytest.raises(RuntimeError, match="not online"):
        setup.setup_notifications(Settings(_env_file=None), "b", api=api_for(fake))


def test_error_answers_raise() -> None:
    api = api_for(FakeRustFS())
    with pytest.raises(S3ApiError, match="HTTP 400"):
        api.request("GET", "/some-bucket")
