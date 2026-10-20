"""Cache hit, cache miss, 304, and TTL for the aggregate read API."""

from __future__ import annotations

import json

import pytest
from ev_api.app import ReadDeps, create_app
from ev_api.read import DEFAULT_KV_TTL_SECONDS, ResourceQuery, read_resource
from fastapi.testclient import TestClient

from texas_turnout_scraper.civix.read_client import (
    cache_key,
    etag_for_version,
    http_path,
    version_key,
)
from texas_turnout_scraper.early_vote_sync import hash_key

_ACCESS_ID = "preview-client.access"
_ACCESS_SECRET = "preview-secret-not-a-voter-id"
_AUTH = {
    "CF-Access-Client-Id": _ACCESS_ID,
    "CF-Access-Client-Secret": _ACCESS_SECRET,
}
_ELECTION = "049664"
_VERSION = 42
_SHA = "abc123"


class MemoryKv:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.puts: list[tuple[str, str, int | None]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        self.puts.append((key, value, expiration_ttl))
        self.values[key] = value


class RecordingStore:
    def __init__(
        self,
        bodies: dict[tuple[str, tuple[str, ...]], dict[str, object]] | None = None,
    ) -> None:
        self.bodies = dict(bodies or {})
        self.calls: list[tuple[str, tuple[str, ...], str]] = []

    async def build_body(
        self,
        source_election_id: str,
        segments: tuple[str, ...],
        *,
        kv_sha256: str,
    ) -> dict[str, object] | None:
        self.calls.append((source_election_id, segments, kv_sha256))
        return self.bodies.get((source_election_id, segments))


def _segments(resource: str = "totals", ev_date: str | None = None) -> tuple[str, ...]:
    base = ("aggregates", resource)
    return (*base, ev_date) if ev_date else base


def _client(kv: MemoryKv, store: RecordingStore) -> TestClient:
    app = create_app(
        ReadDeps(
            kv=kv,
            store=store,
            access_client_id=_ACCESS_ID,
            access_client_secret=_ACCESS_SECRET,
        )
    )
    return TestClient(app)


def _url(resource: str = "totals", ev_date: str | None = None) -> str:
    path = http_path(_ELECTION, *_segments(resource, ev_date))
    return path


@pytest.mark.asyncio
async def test_cache_hit_does_not_query_d1() -> None:
    payload = {"statewide": {"total": 2, "in_person": 2, "mail_in": 0}}
    key = cache_key(_ELECTION, _VERSION, *_segments())
    kv = MemoryKv(
        {
            version_key(_ELECTION): str(_VERSION),
            hash_key(_ELECTION): _SHA,
            key: json.dumps(payload),
        }
    )
    store = RecordingStore()

    outcome = await read_resource(
        ResourceQuery(_ELECTION, _segments(), None),
        kv,
        store,
    )

    assert outcome.status_code == 200
    assert outcome.cache == "hit"
    assert outcome.body == payload
    assert store.calls == []


@pytest.mark.asyncio
async def test_cache_miss_sets_default_ttl() -> None:
    payload = {"statewide": {"total": 4, "in_person": 3, "mail_in": 1}}
    kv = MemoryKv({version_key(_ELECTION): str(_VERSION), hash_key(_ELECTION): _SHA})
    store = RecordingStore({(_ELECTION, _segments()): payload})

    outcome = await read_resource(
        ResourceQuery(_ELECTION, _segments(), None),
        kv,
        store,
    )

    assert outcome.status_code == 200
    assert outcome.cache == "miss"
    assert store.calls == [(_ELECTION, _segments(), _SHA)]
    assert kv.puts[0][2] == DEFAULT_KV_TTL_SECONDS


@pytest.mark.asyncio
async def test_if_none_match_returns_304_without_d1() -> None:
    kv = MemoryKv({version_key(_ELECTION): str(_VERSION), hash_key(_ELECTION): _SHA})
    store = RecordingStore({(_ELECTION, _segments()): {"statewide": {"total": 1}}})

    outcome = await read_resource(
        ResourceQuery(_ELECTION, _segments(), etag_for_version(_VERSION)),
        kv,
        store,
    )

    assert outcome.status_code == 304
    assert outcome.cache == "not_modified"
    assert store.calls == []


def test_http_aggregate_totals_requires_access() -> None:
    kv = MemoryKv({version_key(_ELECTION): str(_VERSION)})
    response = _client(kv, RecordingStore()).get(_url())
    assert response.status_code == 401


def test_http_aggregate_totals_200() -> None:
    payload = {"statewide": {"total": 1, "in_person": 1, "mail_in": 0}}
    key = cache_key(_ELECTION, _VERSION, *_segments())
    kv = MemoryKv({version_key(_ELECTION): str(_VERSION), key: json.dumps(payload)})
    response = _client(kv, RecordingStore()).get(_url(), headers=_AUTH)
    assert response.status_code == 200
    assert response.json()["statewide"]["total"] == 1


def test_http_paths_for_aggregate_surface() -> None:
    assert http_path("53815", "aggregates", "totals") == "/elections/53815/aggregates/totals"
    assert (
        http_path("53815", "aggregates", "county-by-day")
        == "/elections/53815/aggregates/county-by-day"
    )
