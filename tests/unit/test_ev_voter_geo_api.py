"""GET /elections/{id}/roster/{ev_date}/geo: roster-scope gate, shape, paging, ETag, 503."""

from __future__ import annotations

import json
import random
import re
import sqlite3
from typing import Any

import pytest
from ev_api.app import ReadDeps, create_app
from fastapi.testclient import TestClient

from tests.unit.test_early_vote_roster_stream import DAY1, DAY2, _Db, _store_day
from texas_turnout_scraper.consumer_tokens import hash_token, kv_key_for_hash
from texas_turnout_scraper.early_vote_roster_rows import day_number, key_ranges
from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES, sha256_decoded
from texas_turnout_scraper.voter_geo_tables import VOTER_GEO_PAGES, VOTER_GEO_SCHEMA_CREATES

ACCESS = {"CF-Access-Client-Id": "id.access", "CF-Access-Client-Secret": "secret-not-real"}
ROSTER = {"Authorization": "Bearer roster-secret"}
AGGREGATES = {"Authorization": "Bearer agg-secret"}
EV_DATE = "2026-10-19"
OTHER_DATE = "2026-10-20"
SIGNATURE = "ab" * 32
URL = f"/elections/53815/roster/{EV_DATE}/geo"
VOTER_KEYS = {
    "vuid",
    "county_fips",
    "pctkey",
    "cd",
    "sd",
    "hd",
    "district_source",
    "split_flag",
    "flags",
    "lat",
    "lon",
    "geocode_source",
    "geocode_quality",
    "address_points_vintage",
    "matched_at",
}


class _Kv:
    def __init__(self) -> None:
        self.values = {
            kv_key_for_hash(hash_token("roster-secret")): "roster",
            kv_key_for_hash(hash_token("agg-secret")): "aggregates",
        }

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
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


def _vuid(n: int) -> str:
    return f"{n:010d}"


def _insert_row(
    conn: sqlite3.Connection, ev_date: str, vuid: str, row_hash: int, **kw: Any
) -> None:
    conn.execute(
        "INSERT INTO early_vote_roster_row (county, voter_name, id_voter, voting_method, "
        "precinct, day, row_hash, occ, extra, added_in, removed_in) "
        "VALUES ('TRAVIS', 'FAKE PERSON', ?, 'MAIL-IN', '0101', ?, ?, 0, '{}', 1, ?)",
        (vuid, day_number(ev_date), row_hash, kw.get("removed_in")),
    )


def _db(*, voter_geo: bool = True, signature: bool = True) -> tuple[_Db, list[str]]:
    """A day with 300 live voters spread over the hash space; returns the live VUIDs."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    creates = [s for s in _SCHEMA_CREATES if voter_geo or s not in VOTER_GEO_SCHEMA_CREATES]
    for sql in creates:
        conn.execute(sql)
    _store_day(conn, EV_DATE, DAY1)
    _store_day(conn, OTHER_DATE, DAY2)
    rng = random.Random(7)
    live: list[str] = []
    for n in range(300):
        vuid = _vuid(n + 1)
        _insert_row(conn, EV_DATE, vuid, rng.randrange(-(2**63), 2**63))
        live.append(vuid)
        if not voter_geo or n % 10 == 9:
            continue  # every tenth voter has no voter_geo row yet
        pctkey = None if n % 7 == 0 else "4530101"
        conn.execute(
            "INSERT INTO voter_geo VALUES (?, '453', ?, ?, ?, ?, ?, ?, 99, '2026-10-27T01:00:00Z')",
            (
                vuid,
                pctkey,
                None if pctkey is None else "37",
                None if pctkey is None else "14",
                None if pctkey is None else "46",
                None if pctkey is None else 5,
                1 if n % 5 == 0 else 0,
            ),
        )
    _insert_row(conn, EV_DATE, _vuid(900), 0, removed_in=2)
    _insert_row(conn, OTHER_DATE, _vuid(901), 0)
    if voter_geo and signature:
        conn.execute("INSERT INTO voter_geo_meta VALUES ('signature', ?)", (SIGNATURE,))
    conn.commit()
    return _Db(conn), live


def _client(db: _Db | None) -> TestClient:
    deps = ReadDeps(
        kv=_Kv(),
        store=None,  # type: ignore[arg-type]
        access_client_id="id.access",
        access_client_secret="secret-not-real",
        ttl_seconds=60,
        rosters=_Rosters({} if db is None else {"53815": db}),
    )
    return TestClient(create_app(deps))


def _page(client: TestClient, number: int, **headers: str) -> Any:
    return client.get(f"{URL}?page={number}", headers={**ROSTER, **headers})


def test_unauthenticated_requests_are_refused_before_any_d1_work() -> None:
    db, _ = _db()
    client = _client(db)
    for headers in ({}, {"Authorization": "Bearer nope"}, ACCESS):
        assert client.get(f"{URL}?page=0", headers=headers).status_code == 401
    assert db.queries == 0


def test_an_aggregates_token_is_forbidden() -> None:
    db, _ = _db()
    response = _client(db).get(f"{URL}?page=0", headers=AGGREGATES)
    assert response.status_code == 403
    assert db.queries == 0


def test_a_roster_token_reads_a_page() -> None:
    db, _ = _db()
    response = _page(_client(db), 0)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "private, no-cache"


def test_body_shape_and_no_names_method_or_county_text() -> None:
    db, live = _db()
    client = _client(db)
    bodies = [_page(client, k) for k in range(VOTER_GEO_PAGES)]
    body = next(b for b in bodies if json.loads(b.text)["count"] > 0).json()
    assert set(body) == {"source_election_id", "ev_date", "page", "pages", "count", "voters"}
    assert isinstance(body["source_election_id"], str)
    assert (body["source_election_id"], body["ev_date"], body["pages"]) == (
        "53815",
        EV_DATE,
        VOTER_GEO_PAGES,
    )
    assert body["count"] == len(body["voters"])
    for response in bodies:
        for voter in response.json()["voters"]:
            assert set(voter) == VOTER_KEYS
        for forbidden in ("FAKE", "TRAVIS", "voter_name", "voting_method", "MAIL-IN", 'county"'):
            assert forbidden not in response.text
    assert sum(r.json()["count"] for r in bodies) == len(live)


def test_a_point_is_present_exactly_when_the_quality_is_address_point() -> None:
    db, _ = _db()
    client = _client(db)
    qualities: set[str | None] = set()
    for k in range(VOTER_GEO_PAGES):
        for voter in _page(client, k).json()["voters"]:
            has_point = voter["lat"] is not None or voter["lon"] is not None
            assert has_point == (voter["geocode_quality"] == "address_point")
            assert voter["lat"] is None and voter["lon"] is None
            assert voter["address_points_vintage"] is None
            qualities.add(voter["geocode_quality"])
    assert qualities == {"stated_precinct", "not_located", None}


def test_each_voter_reports_the_stored_district_and_the_source_of_the_assignment() -> None:
    db, _ = _db()
    client = _client(db)
    seen = {}
    for k in range(VOTER_GEO_PAGES):
        for voter in _page(client, k).json()["voters"]:
            seen[voter["vuid"]] = voter
    placed = seen[_vuid(2)]
    assert placed == {
        "vuid": _vuid(2),
        "county_fips": "453",
        "pctkey": "4530101",
        "cd": "37",
        "sd": "14",
        "hd": "46",
        "district_source": "precinct_alias",
        "split_flag": 5,
        "flags": 0,
        "lat": None,
        "lon": None,
        "geocode_source": "none",
        "geocode_quality": "stated_precinct",
        "address_points_vintage": None,
        "matched_at": "2026-10-27T01:00:00Z",
    }
    unmatched = seen[_vuid(1)]  # n = 0: a row without a precinct
    assert (unmatched["pctkey"], unmatched["district_source"], unmatched["split_flag"]) == (
        None,
        None,
        None,
    )
    assert unmatched["geocode_quality"] == "not_located"
    unbuilt = seen[_vuid(10)]  # n = 9: no voter_geo row yet
    assert unbuilt["county_fips"] is None and unbuilt["geocode_source"] is None
    assert unbuilt["geocode_quality"] is None and unbuilt["matched_at"] is None


def test_pages_partition_the_days_live_vuids_in_vuid_order() -> None:
    db, live = _db()
    client = _client(db)
    seen: list[str] = []
    for k in range(VOTER_GEO_PAGES):
        vuids = [v["vuid"] for v in _page(client, k).json()["voters"]]
        assert vuids == sorted(vuids)
        low, high = key_ranges(VOTER_GEO_PAGES)[k]
        expected = {
            r[0]
            for r in db.conn.execute(
                "SELECT id_voter FROM early_vote_roster_row WHERE day = ? AND row_hash "
                "BETWEEN ? AND ? AND removed_in IS NULL",
                (day_number(EV_DATE), low, high),
            )
        }
        assert set(vuids) == expected
        seen += vuids
    assert sorted(seen) == sorted(live)
    assert len(seen) == len(set(seen))
    assert _vuid(900) not in seen and _vuid(901) not in seen


def test_two_rows_of_one_voter_in_a_page_are_listed_once() -> None:
    db, _ = _db()
    low, _high = key_ranges(VOTER_GEO_PAGES)[10]
    for offset in (1, 2):
        _insert_row(db.conn, EV_DATE, _vuid(777), int(low) + offset)
    body = _page(_client(db), 10).json()
    assert [v["vuid"] for v in body["voters"]].count(_vuid(777)) == 1
    assert body["count"] == len(body["voters"])


@pytest.mark.parametrize(
    "query",
    ["", "?page=", "?page=-1", "?page=64", "?page=064", "?page=abc", "?page=1.5", "?page=1000"],
)
def test_a_missing_or_out_of_range_page_is_400(query: str) -> None:
    db, _ = _db()
    assert _client(db).get(URL + query, headers=ROSTER).status_code == 400


def test_the_last_page_and_a_padded_first_page_are_valid() -> None:
    db, _ = _db()
    client = _client(db)
    assert _page(client, VOTER_GEO_PAGES - 1).status_code == 200
    assert client.get(URL + "?page=007", headers=ROSTER).json()["page"] == 7


@pytest.mark.parametrize("ev_date", ["2026-13-45", "yesterday", "2026-1-1"])
def test_a_bad_date_is_400(ev_date: str) -> None:
    db, _ = _db()
    response = _client(db).get(f"/elections/53815/roster/{ev_date}/geo?page=0", headers=ROSTER)
    assert response.status_code == 400


def test_unknown_day_election_and_malformed_election() -> None:
    db, _ = _db()
    client = _client(db)
    assert (
        client.get("/elections/53815/roster/2026-10-30/geo?page=0", headers=ROSTER).status_code
        == 404
    )
    assert (
        client.get(f"/elections/99999/roster/{EV_DATE}/geo?page=0", headers=ROSTER).status_code
        == 404
    )
    assert (
        client.get(f"/elections/abc/roster/{EV_DATE}/geo?page=0", headers=ROSTER).status_code == 400
    )


def test_missing_tables_or_signature_is_503() -> None:
    for kwargs in ({"voter_geo": False}, {"signature": False}):
        db, _ = _db(**kwargs)
        response = _page(_client(db), 0)
        assert response.status_code == 503
        assert response.json() == {"detail": "voter_geo_not_loaded"}


def test_etag_names_the_day_the_build_and_the_page_and_gets_304() -> None:
    db, _ = _db()
    client = _client(db)
    first = _page(client, 3)
    day_sha = sha256_decoded(DAY1)
    assert first.headers["etag"] == f'"{day_sha}.{SIGNATURE[:16]}.p3"'
    queries = db.queries
    again = _page(client, 3, **{"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == first.headers["etag"]
    # Only the day lookup and the signature are read, never the page itself.
    page_queries = db.queries - queries
    assert page_queries < queries
    other = _page(client, 4, **{"If-None-Match": first.headers["etag"]})
    assert other.status_code == 200
    assert other.headers["etag"] != first.headers["etag"]
    db.conn.execute("UPDATE voter_geo_meta SET value = ? WHERE name = 'signature'", ("cd" * 32,))
    rebuilt = _page(client, 3, **{"If-None-Match": first.headers["etag"]})
    assert rebuilt.status_code == 200
    assert re.fullmatch(r'"[0-9a-f]{64}\.cdcdcdcdcdcdcdcd\.p3"', rebuilt.headers["etag"])


def test_a_page_costs_one_page_query_and_the_route_is_mounted_once() -> None:
    db, _ = _db()
    client = _client(db)
    before = db.queries
    _page(client, 0)
    # list_days, present check, signature, page.
    assert db.queries - before == 4
    paths = [getattr(r, "path", "") for r in client.app.routes]  # type: ignore[attr-defined]
    assert paths.count("/elections/{source_election_id}/roster/{ev_date}/geo") == 1
    assert "/elections/{source_election_id}/roster/{ev_date}" in paths


# --- Stage 2: residential points ----------------------------------------------------------------

POINT_VINTAGE = "2026-03-18"
POINT_SIGNATURE = "ef" * 32


def _add_point(
    db: _Db, vuid: str, *, cd: str | None = "9", matched_at: str = "2026-10-28T01:00:00Z"
) -> None:
    db.conn.execute(
        "INSERT INTO voter_geo_point VALUES (?, 30.267153, -97.743061, ?, '21', '49', "
        "'txgio_address_point', 'address_point', ?, 7, ?)",
        (vuid, cd, POINT_VINTAGE, matched_at),
    )
    db.conn.commit()


def _all_voters(client: TestClient) -> dict[str, dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for k in range(VOTER_GEO_PAGES):
        for voter in _page(client, k).json()["voters"]:
            seen[voter["vuid"]] = voter
    return seen


def test_empty_point_tables_serve_the_stage_1_answer() -> None:
    # All four tables exist but hold no point: present is not the same as loaded.
    db, _ = _db()
    assert db.conn.execute("SELECT COUNT(*) FROM voter_geo_point").fetchone() == (0,)
    response = _page(_client(db), 0)
    assert response.status_code == 200
    placed = _all_voters(_client(db))[_vuid(2)]
    assert placed["district_source"] == "precinct_alias"
    assert placed["geocode_quality"] == "stated_precinct"


def test_a_point_replaces_the_districts_and_keeps_the_precinct_fields() -> None:
    db, _ = _db()
    _add_point(db, _vuid(2))  # has a voter_geo row with a precinct
    _add_point(db, _vuid(1), cd=None)  # n = 0: a voter_geo row without a precinct
    _add_point(db, _vuid(10))  # n = 9: no voter_geo row at all
    seen = _all_voters(_client(db))
    assert seen[_vuid(2)] == {
        "vuid": _vuid(2),
        "county_fips": "453",
        "pctkey": "4530101",
        "cd": "9",
        "sd": "21",
        "hd": "49",
        "district_source": "address_point",
        "split_flag": 5,
        "flags": 0,
        "lat": 30.267153,
        "lon": -97.743061,
        "geocode_source": "txgio_address_point",
        "geocode_quality": "address_point",
        "address_points_vintage": POINT_VINTAGE,
        "matched_at": "2026-10-28T01:00:00Z",
    }
    # A point in no polygon keeps its null district rather than borrowing the precinct's.
    assert seen[_vuid(1)]["cd"] is None and seen[_vuid(1)]["district_source"] == "address_point"
    # No voter_geo row: the point stands alone, with no precinct or county.
    alone = seen[_vuid(10)]
    assert (alone["county_fips"], alone["pctkey"], alone["flags"]) == (None, None, None)
    assert alone["geocode_quality"] == "address_point" and alone["lat"] == 30.267153


def test_matched_at_is_the_later_of_the_precinct_row_and_the_point() -> None:
    db, _ = _db()
    _add_point(db, _vuid(2), matched_at="2026-10-26T00:00:00Z")  # older than 2026-10-27T01
    _add_point(db, _vuid(3), matched_at="2026-10-28T00:00:00Z")
    seen = _all_voters(_client(db))
    assert seen[_vuid(2)]["matched_at"] == "2026-10-27T01:00:00Z"
    assert seen[_vuid(3)]["matched_at"] == "2026-10-28T00:00:00Z"


def test_lat_and_lon_are_present_exactly_when_the_quality_is_address_point() -> None:
    db, live = _db()
    for index, vuid in enumerate(live):
        if index % 3 == 0:
            _add_point(db, vuid)
    seen = _all_voters(_client(db))
    qualities: dict[str | None, int] = {}
    for voter in seen.values():
        located = voter["lat"] is not None and voter["lon"] is not None
        assert (voter["lat"] is None) == (voter["lon"] is None)
        assert located == (voter["geocode_quality"] == "address_point")
        assert located == (voter["district_source"] == "address_point")
        assert located == (voter["address_points_vintage"] == POINT_VINTAGE)
        qualities[voter["geocode_quality"]] = qualities.get(voter["geocode_quality"], 0) + 1
    assert set(qualities) == {"address_point", "stated_precinct", "not_located", None}
    assert qualities["address_point"] == len(live[::3])
    text = "".join(_page(_client(db), k).text for k in range(VOTER_GEO_PAGES))
    for forbidden in ("FAKE", "TRAVIS", "voter_name", "voting_method", "MAIL-IN", 'county"'):
        assert forbidden not in text


def test_the_etag_names_the_point_apply_and_stays_put_without_one() -> None:
    db, _ = _db()
    client = _client(db)
    before = _page(client, 3).headers["etag"]
    assert re.fullmatch(r'"[0-9a-f]{64}\.abababababababab\.p3"', before)
    queries = db.queries
    _page(client, 3)
    assert db.queries - queries == 4  # the combined signature query adds no round trip
    db.conn.execute("INSERT INTO voter_geo_point_meta VALUES ('signature', ?)", (POINT_SIGNATURE,))
    after = _page(client, 3, **{"If-None-Match": before})
    assert after.status_code == 200
    assert re.fullmatch(
        r'"[0-9a-f]{64}\.abababababababab\.efefefefefefefef\.p3"', after.headers["etag"]
    )
    db.conn.execute(
        "UPDATE voter_geo_point_meta SET value = ? WHERE name = 'signature'", ("12" * 32,)
    )
    assert _page(client, 3).headers["etag"] != after.headers["etag"]


def test_missing_point_tables_are_503() -> None:
    db, _ = _db()
    db.conn.execute("DROP TABLE voter_geo_point")
    response = _page(_client(db), 0)
    assert response.status_code == 503
    assert response.json() == {"detail": "voter_geo_not_loaded"}
