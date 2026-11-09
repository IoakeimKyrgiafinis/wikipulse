"""Signed requests to the S3 API, for the few calls pyarrow doesn't offer.

pyarrow reads and writes objects, but can't configure bucket notifications, and the
object store's admin API (which defines where notifications go) is a separate HTTP API.
Both expect requests signed with AWS Signature Version 4 ("SigV4"), the scheme every
S3-compatible server uses. Signing is short enough to do here, so no AWS SDK is needed:

1. Write the request in a *canonical* form: method, path, sorted query string, the
   signed headers (host, x-amz-content-sha256, x-amz-date), and the SHA-256 of the body.
2. Hash that and combine it with the date and *scope* (day/region/service) into the
   "string to sign".
3. Derive a signing key from the secret key by HMAC-ing the day, region, service and
   'aws4_request' in turn, and HMAC the string to sign with it.

The server repeats the same steps with its copy of the secret and compares. The secret
itself never travels; a signature is valid only for this exact request and for a
few minutes around its date.
"""

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

import httpx

from wikipulse.config import Settings

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _encode(text: str, safe: str = "-_.~") -> str:
    return quote(text, safe=safe)


def canonical_query(params: dict[str, str]) -> str:
    """'a=1&b=' with keys sorted and everything percent-encoded (SigV4's rules)."""
    return "&".join(f"{_encode(k)}={_encode(str(v))}" for k, v in sorted(params.items()))


def sign_headers(
    method: str,
    url: str,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    params: dict[str, str] | None = None,
    body: bytes = b"",
    now: datetime | None = None,
) -> dict[str, str]:
    """The headers (including Authorization) that sign this request with SigV4."""
    parsed = urlparse(url)
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    day = moment.strftime("%Y%m%d")
    payload_hash = _sha256(body)
    headers = {"host": parsed.netloc, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical_request = "\n".join(
        [
            method.upper(),
            _encode(parsed.path or "/", safe="/-_.~"),
            canonical_query(params or {}),
            "".join(f"{name}:{headers[name]}\n" for name in sorted(headers)),
            signed,
            payload_hash,
        ]
    )
    scope = f"{day}/{region}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join([ALGORITHM, amz_date, scope, _sha256(canonical_request.encode())])
    key = _hmac(
        _hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), SERVICE), "aws4_request"
    )
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return headers


class S3ApiError(RuntimeError):
    """The server answered a signed request with an error status."""


class S3Api:
    """Minimal signed HTTP client for S3_ENDPOINT (bucket settings, admin API)."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.endpoint = settings.s3_endpoint.rstrip("/")
        self.region = settings.s3_region
        self._access_key = settings.s3_access_key.get_secret_value()
        self._secret_key = settings.s3_secret_key.get_secret_value()
        self._client = client or httpx.Client(timeout=15)

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> httpx.Response:
        """Send a signed request; raises S3ApiError on a 4xx or 5xx answer."""
        url = f"{self.endpoint}{path}"
        headers = sign_headers(
            method,
            url,
            access_key=self._access_key,
            secret_key=self._secret_key,
            region=self.region,
            params=params,
            body=body,
        )
        # The query string must be sent exactly as it was signed.
        query = canonical_query(params or {})
        response = self._client.request(
            method, f"{url}?{query}" if query else url, content=body, headers=headers
        )
        if response.status_code >= 400:
            raise S3ApiError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text[:500]}"
            )
        return response

    def close(self) -> None:
        self._client.close()
