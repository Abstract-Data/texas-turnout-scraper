"""Cloudflare REST client retries for D1 query rate limits (synthetic HTTP only)."""

from __future__ import annotations

import httpx
import pytest
import respx

from texas_turnout_scraper.early_vote_sync import (
    CLOUDFLARE_API_BASE,
    CLOUDFLARE_API_MAX_ATTEMPTS,
    CloudflareEarlyVoteStore,
)

ACCOUNT = "00000000000000000000000000000001"
NAMESPACE = "ns-test"


def _cf_store(client: httpx.Client) -> CloudflareEarlyVoteStore:
    return CloudflareEarlyVoteStore(
        account_id=ACCOUNT,
        api_token="token-not-logged",
        kv_namespace_id=NAMESPACE,
        client=client,
    )


def _ok_rows() -> httpx.Response:
    return httpx.Response(
        200,
        json={"success": True, "result": [{"results": [{"n": 1}], "success": True}]},
    )


def test_d1_query_retries_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "texas_turnout_scraper.early_vote_sync.time.sleep", lambda s: sleeps.append(s)
    )
    query_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database/db-1/query"
    with respx.mock:
        route = respx.post(query_url).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                _ok_rows(),
            ]
        )
        with httpx.Client(base_url=CLOUDFLARE_API_BASE) as client:
            store = _cf_store(client)
            rows = store._query_rows("db-1", "SELECT 1 AS n", [])
    assert route.call_count == 2
    assert rows == [{"n": 1}]
    assert sleeps == [pytest.approx(2.0, rel=0, abs=0.75)]


def test_d1_query_raises_after_retry_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("texas_turnout_scraper.early_vote_sync.time.sleep", lambda _s: None)
    query_url = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/d1/database/db-1/query"
    with respx.mock:
        route = respx.post(query_url).mock(return_value=httpx.Response(429))
        with httpx.Client(base_url=CLOUDFLARE_API_BASE) as client:
            store = _cf_store(client)
            with pytest.raises(Exception, match="429") as exc_info:
                store._query_rows("db-1", "SELECT 1", [])
    assert route.call_count == CLOUDFLARE_API_MAX_ATTEMPTS
    assert getattr(exc_info.value, "http_status", None) == 429
