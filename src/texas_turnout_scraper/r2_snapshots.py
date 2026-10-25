"""Versioned copies of early-vote day files in a private R2 bucket.

R2 object tokens only work with the S3 API (SigV4), so requests are signed here with
hashlib and hmac instead of adding an SDK. Errors carry the HTTP status and never the keys.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from texas_turnout_scraper.civix.ids import require_election_id

logger = logging.getLogger(__name__)

_REGION = "auto"
_SERVICE = "s3"
_TIMEOUT_SECONDS = 120.0


class R2SnapshotError(Exception):
    """An R2 request failed. The message holds the status and operation, no credentials."""


def snapshot_key(election_id: str, ev_date: str, sha256: str) -> str:
    return f"elections/{require_election_id(election_id)}/{ev_date}/{sha256}.csv.gz"


def manifest_key(election_id: str, ev_date: str) -> str:
    return f"elections/{require_election_id(election_id)}/{ev_date}/manifest.json"


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


class R2SnapshotStore:
    def __init__(
        self,
        *,
        account_id: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        client: httpx.Client | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._host = f"{account_id}.r2.cloudflarestorage.com"
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
        self._now = now

    def _signed_headers(
        self,
        method: str,
        path: str,
        body: bytes,
        content_type: str | None,
    ) -> dict[str, str]:
        moment = self._now().astimezone(timezone.utc)
        amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
        day = moment.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            "host": self._host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if content_type:
            headers["content-type"] = content_type
        names = sorted(headers)
        canonical = "\n".join(
            [
                method,
                path,
                "",
                "".join(f"{name}:{headers[name]}\n" for name in names),
                ";".join(names),
                payload_hash,
            ]
        )
        scope = f"{day}/{_REGION}/{_SERVICE}/aws4_request"
        to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = _hmac(
            _hmac(
                _hmac(_hmac(f"AWS4{self._secret_access_key}".encode(), day), _REGION),
                _SERVICE,
            ),
            "aws4_request",
        )
        signature = hmac.new(signing_key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key_id}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}"
        )
        return headers

    def _request(
        self,
        method: str,
        key: str,
        *,
        body: bytes = b"",
        content_type: str | None = None,
    ) -> httpx.Response:
        path = f"/{self._bucket}/{quote(key, safe='/-_.~')}"
        headers = self._signed_headers(method, path, body, content_type)
        try:
            return self._client.request(
                method,
                f"https://{self._host}{path}",
                content=body if method == "PUT" else None,
                headers={k: v for k, v in headers.items() if k != "host"},
            )
        except httpx.HTTPError as exc:
            msg = f"R2 {method} failed: {type(exc).__name__}"
            raise R2SnapshotError(msg) from None

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
        response = self._request("PUT", key, body=data, content_type=content_type)
        if response.status_code >= 300:
            msg = f"R2 PUT failed with HTTP {response.status_code}"
            raise R2SnapshotError(msg)

    def get_object(self, key: str) -> bytes | None:
        response = self._request("GET", key)
        if response.status_code == 404:
            return None
        if response.status_code >= 300:
            msg = f"R2 GET failed with HTTP {response.status_code}"
            raise R2SnapshotError(msg)
        return response.content

    def has_object(self, key: str) -> bool:
        response = self._request("HEAD", key)
        if response.status_code == 404:
            return False
        if response.status_code >= 300:
            msg = f"R2 HEAD failed with HTTP {response.status_code}"
            raise R2SnapshotError(msg)
        return True

    def record_version(
        self,
        election_id: str,
        ev_date: str,
        *,
        sha256: str,
        decoded: bytes,
        rows: int,
    ) -> None:
        """Store this version once and list it in the day's manifest."""
        manifest_path = manifest_key(election_id, ev_date)
        existing = self.get_object(manifest_path)
        manifest: dict[str, list[dict[str, object]]] = (
            json.loads(existing) if existing else {"versions": []}
        )
        if any(item.get("sha256") == sha256 for item in manifest["versions"]):
            return
        key = snapshot_key(election_id, ev_date, sha256)
        if not self.has_object(key):
            self.put_object(key, gzip.compress(decoded), content_type="application/gzip")
        manifest["versions"].append(
            {
                "sha256": sha256,
                "rows": rows,
                "byte_length": len(decoded),
                "loaded_at": self._now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
        self.put_object(
            manifest_path,
            json.dumps(manifest, sort_keys=True).encode("utf-8"),
            content_type="application/json",
        )
        logger.info(
            "r2 snapshot recorded election_id=%s ev_date=%s versions=%s",
            election_id,
            ev_date,
            len(manifest["versions"]),
        )


_SNAPSHOT_ENV: tuple[tuple[str, ...], ...] = (
    ("CLOUDFLARE_ACCOUNT_ID",),
    ("EV_SNAPSHOT_BUCKET",),
    ("EV_SNAPSHOT_R2_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID"),
    ("EV_SNAPSHOT_R2_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY"),
)

_missing_env_warned = False


def _snapshot_env() -> list[str]:
    """One value per ``_SNAPSHOT_ENV`` entry: the first set name wins, '' when none is."""
    values = []
    for names in _SNAPSHOT_ENV:
        set_values = [os.environ.get(name, "").strip() for name in names]
        values.append(next((value for value in set_values if value), ""))
    return values


def snapshot_store_from_env() -> R2SnapshotStore | None:
    """Build the store from environment variables, or None when any is missing.

    The keys default to the shared ``R2_*`` pair; ``EV_SNAPSHOT_R2_*`` overrides them with a
    bucket-scoped pair.
    """
    account, bucket, key_id, secret = _snapshot_env()
    if not (account and bucket and key_id and secret):
        return None
    return R2SnapshotStore(
        account_id=account,
        bucket=bucket,
        access_key_id=key_id,
        secret_access_key=secret,
    )


def warn_missing_snapshot_env() -> None:
    """Log, once per process, which variable names keep snapshot history off. Never values."""
    global _missing_env_warned
    if _missing_env_warned:
        return
    missing = [
        " or ".join(names)
        for names, value in zip(_SNAPSHOT_ENV, _snapshot_env(), strict=True)
        if not value
    ]
    if not missing:
        return
    _missing_env_warned = True
    logger.warning(
        "r2 snapshot history is off: R2 settings missing: %s",
        ", ".join(missing),
    )
