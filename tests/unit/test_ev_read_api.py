"""Cache hit, cache miss, and 304 for the early-vote Worker read API."""

from __future__ import annotations

import json

import pytest
from ev_api.auth import access_authorized
from ev_api.bindings import WorkersElectionStore, WorkersKvNamespace
from ev_api.read import CountyQuery, read_county

from texas_turnout_scraper.civix.read_client import (
    cache_key,
    etag_for_version,
    http_path,
    version_key,
)

_ACCESS_ID = "preview-client.access"
_ACCESS_SECRET = "preview-secret-not-a-voter-id"
_ELECTION = "049664"
_COUNTY = "0453"
_VERSION = 42


class MemoryKv:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.puts: list[tuple[str, str, int | None]] = []
        self.gets: list[str] = []

    async def get(self, key: str) -> str | None:
        self.gets.append(key)
        return self.values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        self.puts.append((key, value, expiration_ttl))
        self.values[key] = value


class RecordingStore:
    def __init__(self, rows: dict[tuple[str, str], str] | None = None) -> None:
        self.rows = dict(rows or {})
        self.calls: list[tuple[str, str]] = []

    async def fetch_body(self, source_election_id: str, resource: str) -> str | None:
        self.calls.append((source_election_id, resource))
        return self.rows.get((source_election_id, resource))


def _query(if_none_match: str | None = None) -> CountyQuery:
    return CountyQuery(
        source_election_id=_ELECTION,
        county_id=_COUNTY,
        if_none_match=if_none_match,
    )


def _cached(version: int = _VERSION) -> str:
    return cache_key(_ELECTION, version, "county", _COUNTY)


@pytest.mark.asyncio
async def test_cache_hit_does_not_query_d1() -> None:
    payload = {"county": _COUNTY, "rows": 2, "VOTER_NAME": "Synthetic", "ID_VOTER": "0000000001"}
    raw = json.dumps(payload)
    key = _cached()
    kv = MemoryKv({version_key(_ELECTION): str(_VERSION), key: raw})
    store = RecordingStore()

    outcome = await read_county(_query(), kv, store)

    assert outcome.status_code == 200
    assert outcome.cache == "hit"
    assert outcome.etag == etag_for_version(_VERSION)
    assert outcome.body == payload
    assert outcome.body is not None
    assert outcome.body["ID_VOTER"] == "0000000001"
    assert store.calls == []
    assert kv.puts == []
    assert http_path(_ELECTION, "county", _COUNTY) == f"/elections/{_ELECTION}/county/{_COUNTY}"


@pytest.mark.asyncio
async def test_cache_miss_queries_d1_once_and_does_not_purge(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {"county": _COUNTY, "rows": 3, "VOTER_NAME": "Synthetic", "ID_VOTER": "0000000002"}
    raw = json.dumps(payload)
    previous = cache_key(_ELECTION, 41, "county", _COUNTY)
    kv = MemoryKv({version_key(_ELECTION): str(_VERSION), previous: '{"rows": 1}'})
    store = RecordingStore({(_ELECTION, f"county:{_COUNTY}"): raw})

    with caplog.at_level("DEBUG"):
        outcome = await read_county(_query(), kv, store)

    assert outcome.status_code == 200
    assert outcome.cache == "miss"
    assert outcome.etag == '"v42"'
    assert outcome.body == payload
    assert store.calls == [(_ELECTION, f"county:{_COUNTY}")]
    assert kv.puts == [(_cached(), raw, None)]
    assert kv.values[previous] == '{"rows": 1}'
    logged = caplog.text
    assert "Synthetic" not in logged
    assert "0000000002" not in logged


@pytest.mark.asyncio
async def test_if_none_match_returns_304_without_d1_or_body_read() -> None:
    class VersionOnlyKv(MemoryKv):
        async def get(self, key: str) -> str | None:
            self.gets.append(key)
            if key != version_key(_ELECTION):
                raise AssertionError(key)
            return str(_VERSION)

    kv = VersionOnlyKv()
    store = RecordingStore({(_ELECTION, f"county:{_COUNTY}"): '{"rows": 1}'})

    outcome = await read_county(_query(etag_for_version(_VERSION)), kv, store)

    assert outcome.status_code == 304
    assert outcome.cache == "not_modified"
    assert outcome.etag == etag_for_version(_VERSION)
    assert outcome.body is None
    assert store.calls == []
    assert kv.gets == [version_key(_ELECTION)]


@pytest.mark.asyncio
async def test_ttl_stays_unset_unless_the_caller_passes_one() -> None:
    raw = '{"rows": 1}'
    kv = MemoryKv({version_key(_ELECTION): str(_VERSION)})
    store = RecordingStore({(_ELECTION, f"county:{_COUNTY}"): raw})

    unset = await read_county(_query(), kv, store)
    assert unset.cache == "miss"
    assert kv.puts[-1][2] is None

    explicit = 120
    kv.values.pop(_cached(), None)
    await read_county(_query(), kv, store, ttl_seconds=explicit)
    assert kv.puts[-1][2] == explicit


def test_access_service_token_fails_closed() -> None:
    headers = {
        "CF-Access-Client-Id": _ACCESS_ID,
        "CF-Access-Client-Secret": _ACCESS_SECRET,
    }
    assert access_authorized(headers, client_id=_ACCESS_ID, client_secret=_ACCESS_SECRET)
    assert not access_authorized({}, client_id=_ACCESS_ID, client_secret=_ACCESS_SECRET)
    assert not access_authorized(headers, client_id="", client_secret="")


@pytest.mark.asyncio
async def test_kv_put_omits_ttl_when_unset_and_d1_binds_resource() -> None:
    class Namespace:
        def __init__(self) -> None:
            self.puts: list[tuple[str, str, object]] = []

        async def get(self, key: str) -> str | None:
            return None

        async def put(self, key: str, value: str, options: dict[str, int] | None = None) -> None:
            self.puts.append((key, value, options))

    namespace = Namespace()
    kv = WorkersKvNamespace(namespace)
    await kv.put(cache_key(_ELECTION, _VERSION, "county", _COUNTY), "{}", expiration_ttl=None)
    assert namespace.puts == [(cache_key(_ELECTION, _VERSION, "county", _COUNTY), "{}", None)]

    class Database:
        def __init__(self) -> None:
            self.sql: list[str] = []
            self.bound: list[str] = []

        def prepare(self, sql: str) -> Database:
            self.sql.append(sql)
            return self

        def bind(self, resource: str) -> Database:
            self.bound.append(resource)
            return self

        async def first(self) -> dict[str, str]:
            return {"body": '{"rows": 1}'}

    database = Database()

    class Env:
        EV_D1_049664 = database

    store = WorkersElectionStore(Env(), {_ELECTION: "EV_D1_049664"})
    body = await store.fetch_body(_ELECTION, f"county:{_COUNTY}")
    assert body == '{"rows": 1}'
    assert database.bound == [f"county:{_COUNTY}"]
    assert database.sql == ["SELECT body FROM early_vote WHERE resource = ?"]
    assert _ELECTION not in database.sql[0]
    missing = WorkersElectionStore(Env(), {})
    assert await missing.fetch_body(_ELECTION, f"county:{_COUNTY}") is None
