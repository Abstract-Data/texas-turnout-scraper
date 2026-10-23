"""Versioned day-file snapshots in a private R2 bucket (S3 API, SigV4 over httpx)."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from texas_turnout_scraper.r2_snapshots import (
    R2SnapshotError,
    R2SnapshotStore,
    snapshot_key,
    snapshot_store_from_env,
)

ACCOUNT = "acct"
BASE = f"https://{ACCOUNT}.r2.cloudflarestorage.com/my-bucket"
KEY = "elections/53815/2026-10-14/abc.csv.gz"
NOW = datetime(2026, 10, 23, 10, 21, 9, tzinfo=timezone.utc)


def _store(client: httpx.Client | None = None) -> R2SnapshotStore:
    return R2SnapshotStore(
        account_id=ACCOUNT,
        bucket="my-bucket",
        access_key_id="AKIDEXAMPLE",
        secret_access_key="SECRETEXAMPLE",
        client=client,
        now=lambda: NOW,
    )


@respx.mock
def test_put_signs_like_botocore() -> None:
    """Expected signature computed once with botocore's S3SigV4Auth for the same request."""
    route = respx.put(f"{BASE}/{KEY}").mock(return_value=httpx.Response(200))
    _store().put_object(KEY, b"hello snapshot", content_type="application/gzip")
    sent = route.calls[0].request
    assert sent.headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261023/auto/s3/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, "
        "Signature=4894efea4262ff12eba5656fa3374eb9d4ad4df57e40b78f3623d0ad672e1da4"
    )
    assert sent.headers["x-amz-date"] == "20261023T102109Z"


@respx.mock
def test_get_returns_none_for_a_missing_object() -> None:
    respx.get(f"{BASE}/{KEY}").mock(return_value=httpx.Response(404))
    assert _store().get_object(KEY) is None


@respx.mock
def test_errors_do_not_leak_credentials() -> None:
    respx.put(f"{BASE}/{KEY}").mock(return_value=httpx.Response(403, text="AccessDenied"))
    with pytest.raises(R2SnapshotError) as err:
        _store().put_object(KEY, b"x", content_type="text/plain")
    text = str(err.value)
    assert "403" in text
    assert "SECRETEXAMPLE" not in text
    assert "AKIDEXAMPLE" not in text


def test_snapshot_key_layout() -> None:
    assert snapshot_key("53815", "2026-10-14", "abc") == KEY


@respx.mock
def test_record_version_stores_gzip_once_and_appends_manifest() -> None:
    puts: dict[str, bytes] = {}
    manifest_url = f"{BASE}/elections/53815/2026-10-14/manifest.json"

    def put(request: httpx.Request) -> httpx.Response:
        puts[request.url.path.removeprefix("/my-bucket/")] = request.content
        return httpx.Response(200)

    respx.put(url__regex=rf"{BASE}/.*").mock(side_effect=put)
    respx.get(manifest_url).mock(return_value=httpx.Response(404))
    respx.head(f"{BASE}/{KEY}").mock(return_value=httpx.Response(404))
    store = _store()
    store.record_version("53815", "2026-10-14", sha256="abc", decoded=b"a,b\n1,2\n", rows=1)
    assert gzip.decompress(puts[KEY]) == b"a,b\n1,2\n"
    manifest = json.loads(puts["elections/53815/2026-10-14/manifest.json"])
    assert [v["sha256"] for v in manifest["versions"]] == ["abc"]
    assert manifest["versions"][0]["rows"] == 1
    assert manifest["versions"][0]["loaded_at"] == "2026-10-23T10:21:09Z"


@respx.mock
def test_record_version_is_idempotent_for_the_same_hash() -> None:
    existing = {"versions": [{"sha256": "abc", "rows": 1, "loaded_at": "t0"}]}
    respx.get(f"{BASE}/elections/53815/2026-10-14/manifest.json").mock(
        return_value=httpx.Response(200, content=json.dumps(existing).encode())
    )
    puts = respx.put(url__regex=rf"{BASE}/.*").mock(return_value=httpx.Response(200))
    _store().record_version("53815", "2026-10-14", sha256="abc", decoded=b"x", rows=1)
    assert puts.call_count == 0


def test_from_env_requires_every_variable_or_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CLOUDFLARE_ACCOUNT_ID",
        "EV_SNAPSHOT_BUCKET",
        "EV_SNAPSHOT_R2_ACCESS_KEY_ID",
        "EV_SNAPSHOT_R2_SECRET_ACCESS_KEY",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert snapshot_store_from_env() is None
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("EV_SNAPSHOT_BUCKET", "my-bucket")
    monkeypatch.setenv("EV_SNAPSHOT_R2_ACCESS_KEY_ID", "k")
    assert snapshot_store_from_env() is None
    monkeypatch.setenv("EV_SNAPSHOT_R2_SECRET_ACCESS_KEY", "s")
    assert snapshot_store_from_env() is not None


def test_from_env_falls_back_to_the_shared_r2_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("EV_SNAPSHOT_R2_ACCESS_KEY_ID", "EV_SNAPSHOT_R2_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setenv("EV_SNAPSHOT_BUCKET", "my-bucket")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "shared-id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "shared-secret")
    store = snapshot_store_from_env()
    assert store is not None
    assert store._access_key_id == "shared-id"
    monkeypatch.setenv("EV_SNAPSHOT_R2_ACCESS_KEY_ID", "own-id")
    monkeypatch.setenv("EV_SNAPSHOT_R2_SECRET_ACCESS_KEY", "own-secret")
    own = snapshot_store_from_env()
    assert own is not None
    assert own._access_key_id == "own-id"
