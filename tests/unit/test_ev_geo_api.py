"""GET /elections/{id}/geo/precincts: auth gate, shape, cache, ETag/304, missing geography."""

from __future__ import annotations

import sqlite3

import pytest
from ev_api.app import ReadDeps, create_app
from ev_api.read import BODY_SHAPE
from fastapi.testclient import TestClient

from tests.unit.test_early_vote_roster_stream import _Db
from texas_turnout_scraper.civix.read_client import cache_key, version_key
from texas_turnout_scraper.consumer_tokens import hash_token, kv_key_for_hash
from texas_turnout_scraper.early_vote_sync import hash_key
from texas_turnout_scraper.geo_tables import GEO_SCHEMA_CREATES

ACCESS = {"CF-Access-Client-Id": "id.access", "CF-Access-Client-Secret": "secret-not-real"}
ROSTER = {"Authorization": "Bearer roster-secret"}
AGGREGATES = {"Authorization": "Bearer agg-secret"}
URL = "/elections/53815/geo/precincts"
TTL = 4321


class _Kv:
    def __init__(self) -> None:
        self.values = {
            kv_key_for_hash(hash_token("roster-secret")): "roster",
            kv_key_for_hash(hash_token("agg-secret")): "aggregates",
            version_key("53815"): "7",
            hash_key("53815"): "sha-of-the-roster-file",
        }
        self.puts: list[tuple[str, int | None]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        if ":lock:" not in key:
            self.puts.append((key, expiration_ttl))
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class _Rosters:
    def __init__(self, dbs: dict[str, _Db]) -> None:
        self._dbs = dbs

    def election_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._dbs))

    def database(self, election_id: str) -> _Db | None:
        return self._dbs.get(election_id)


def _geo_db() -> _Db:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    for sql in GEO_SCHEMA_CREATES:
        conn.execute(sql)
    areas = [
        ("precinct", "4530002", "0002", 30.2, -97.7, 30.1, -97.8, 30.3, -97.6),
        ("precinct", "4530001", "0001", 30.0, -97.0, 29.9, -97.1, 30.1, -96.9),
        ("precinct", "1230007", "0007", 29.1, -97.3, None, None, None, None),
        ("county", "453", "Travis", 30.3, -97.7, 29.0, -98.0, 31.0, -97.0),
    ]
    conn.executemany("INSERT INTO geo_area VALUES (?,?,?,?,?,?,?,?,?)", areas)
    aliases = [
        ("TRAVIS", "2", "4530002", "zfill4"),
        ("TRAVIS", "0001", "4530001", "exact"),
        ("DEWITT", "7", "1230007", "zfill4"),
        ("TRAVIS", "ZZZ", None, "unmatched"),
        ("LA SALLE", "9", None, "unmatched"),
    ]
    conn.executemany("INSERT INTO geo_precinct_alias VALUES (?,?,?,?)", aliases)
    conn.commit()
    return _Db(conn)


def _client(db: _Db | None, kv: _Kv | None = None) -> tuple[TestClient, _Kv]:
    kv = kv or _Kv()
    deps = ReadDeps(
        kv=kv,
        store=None,  # type: ignore[arg-type]
        ttl_seconds=TTL,
        rosters=_Rosters({} if db is None else {"53815": db}),
    )
    return TestClient(create_app(deps)), kv


def test_unauthenticated_requests_are_refused_before_any_d1_or_kv_work() -> None:
    db = _geo_db()
    client, kv = _client(db)
    for headers in ({}, {"Authorization": "Bearer nope"}, ACCESS):
        assert client.get(URL, headers=headers).status_code == 401
    assert db.queries == 0
    assert kv.puts == []


@pytest.mark.parametrize("headers", [ROSTER, AGGREGATES])
def test_any_authorized_credential_may_read_geography(headers: dict[str, str]) -> None:
    client, _ = _client(_geo_db())
    assert client.get(URL, headers=headers).status_code == 200


def test_body_lists_matched_precincts_sorted_with_point_box_and_unmatched_count() -> None:
    client, _ = _client(_geo_db())
    response = client.get(URL, headers=AGGREGATES)
    body = response.json()
    assert body["source_election_id"] == "53815"
    assert isinstance(body["source_election_id"], str)
    assert body["version"] == "7"
    assert (body["count"], body["unmatched"]) == (3, 2)
    assert [(p["county"], p["precinct"]) for p in body["precincts"]] == [
        ("DEWITT", "7"),
        ("TRAVIS", "0001"),
        ("TRAVIS", "2"),
    ]
    assert body["precincts"][1] == {
        "county": "TRAVIS",
        "precinct": "0001",
        "pctkey": "4530001",
        "lat": 30.0,
        "lon": -97.0,
        "bbox": [-97.1, 29.9, -96.9, 30.1],
    }
    assert body["precincts"][0]["bbox"] is None
    assert set(body) == {"source_election_id", "version", "count", "unmatched", "precincts"}
    assert response.headers["etag"] == '"v7"'
    assert "private" in response.headers["cache-control"]


def test_a_second_request_is_served_from_kv_without_touching_d1() -> None:
    db = _geo_db()
    client, kv = _client(db)
    first = client.get(URL, headers=AGGREGATES)
    queries = db.queries
    assert queries > 0
    assert kv.puts == [(cache_key("53815", 7, "geo", "precincts", BODY_SHAPE), TTL)]
    second = client.get(URL, headers=ROSTER)
    assert second.json() == first.json()
    assert db.queries == queries


def test_matching_if_none_match_returns_304_once_cached() -> None:
    db = _geo_db()
    client, _ = _client(db)
    etag = client.get(URL, headers=AGGREGATES).headers["etag"]
    queries = db.queries
    again = client.get(URL, headers={**AGGREGATES, "If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag
    assert db.queries == queries
    stale = client.get(URL, headers={**AGGREGATES, "If-None-Match": '"v6"'})
    assert stale.status_code == 200


def test_a_new_election_version_gets_a_new_cache_entry_and_etag() -> None:
    db = _geo_db()
    client, kv = _client(db)
    client.get(URL, headers=AGGREGATES)
    kv.values[version_key("53815")] = "8"
    response = client.get(URL, headers=AGGREGATES)
    assert response.headers["etag"] == '"v8"'
    assert response.json()["version"] == "8"
    assert [key for key, _ in kv.puts] == [
        cache_key("53815", 7, "geo", "precincts", BODY_SHAPE),
        cache_key("53815", 8, "geo", "precincts", BODY_SHAPE),
    ]


def test_missing_geography_tables_return_503_and_cache_nothing() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    client, kv = _client(_Db(conn))
    response = client.get(URL, headers=AGGREGATES)
    assert response.status_code == 503
    assert response.json() == {"detail": "geo_not_loaded"}
    assert kv.puts == []


def test_an_old_shaped_geo_area_returns_503_not_a_traceback() -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        "CREATE TABLE geo_area (kind VARCHAR NOT NULL, code VARCHAR NOT NULL, name VARCHAR "
        "NOT NULL, lat FLOAT NOT NULL, lon FLOAT NOT NULL, PRIMARY KEY (kind, code))"
    )
    conn.execute(GEO_SCHEMA_CREATES[2])
    client, kv = _client(_Db(conn))
    response = client.get(URL, headers=AGGREGATES)
    assert response.status_code == 503
    assert response.json() == {"detail": "geo_not_loaded"}
    assert kv.puts == []


def test_bad_and_unknown_elections() -> None:
    client, _ = _client(_geo_db())
    assert client.get("/elections/abc/geo/precincts", headers=AGGREGATES).status_code == 400
    assert client.get("/elections/99999/geo/precincts", headers=AGGREGATES).status_code == 404


def test_an_election_with_no_published_version_is_404() -> None:
    kv = _Kv()
    del kv.values[version_key("53815")]
    client, _ = _client(_geo_db(), kv)
    assert client.get(URL, headers=AGGREGATES).status_code == 404


def test_docs_stay_off_and_the_route_is_mounted_once() -> None:
    client, _ = _client(_geo_db())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path, headers=AGGREGATES).status_code == 404
    paths = [getattr(r, "path", "") for r in client.app.routes]  # type: ignore[attr-defined]
    assert paths.count("/elections/{source_election_id}/geo/precincts") == 1
    assert "/elections/{source_election_id}/roster" in paths
