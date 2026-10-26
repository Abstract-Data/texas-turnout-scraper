"""ConsumerTokenAdmin expiry writes against an in-memory D1 and KV."""

from __future__ import annotations

import datetime as dt

import pytest

from texas_turnout_scraper.consumer_token_admin import TokenNotFoundError, TokenRevokedError

from .fake_cloudflare import FakeCloudflare

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


def test_store_writes_expires_at_and_a_kv_ttl() -> None:
    cf = FakeCloudflare([])
    admin = cf.admin()
    issued = admin.issue("partner", days=30, now=NOW)
    admin.store(issued, now=NOW)
    row = cf.db.execute("SELECT expires_at FROM consumer_api_token").fetchone()
    assert row["expires_at"] == "2026-12-01T12:00:00+00:00"
    ((_v, params),) = cf.kv.values()
    assert params == {"expiration_ttl": str(30 * 86400)}


def test_extend_from_a_future_expiry_adds_to_it_and_reputs_kv() -> None:
    cf = FakeCloudflare([])
    admin = cf.admin()
    issued = admin.issue("partner", days=10, now=NOW)
    admin.store(issued, now=NOW)
    new = admin.extend(issued.token_id, 5, now=NOW + dt.timedelta(days=1))
    assert new == "2026-11-16T12:00:00+00:00"
    ((_v, params),) = cf.kv.values()
    assert int(params["expiration_ttl"]) == 15 * 86400 - 86400


def test_extend_after_lapse_counts_from_now() -> None:
    cf = FakeCloudflare([])
    admin = cf.admin()
    issued = admin.issue("partner", days=1, now=NOW)
    admin.store(issued, now=NOW)
    later = NOW + dt.timedelta(days=9)
    assert admin.extend(issued.token_id, 7, now=later) == "2026-11-17T12:00:00+00:00"


def test_extend_revoked_and_unknown() -> None:
    cf = FakeCloudflare([])
    admin = cf.admin()
    issued = admin.issue("partner", days=1, now=NOW)
    admin.store(issued, now=NOW)
    admin.revoke(issued.token_id)
    with pytest.raises(TokenRevokedError):
        admin.extend(issued.token_id, 1)
    with pytest.raises(TokenNotFoundError):
        admin.extend("missing", 1)
