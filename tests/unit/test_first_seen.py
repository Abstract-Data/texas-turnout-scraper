"""First-appearance voter counts: the build against an SQL oracle, and the published fields."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import random
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, cast

import pytest
from ev_api.read import BODY_SHAPE, ResourceQuery, read_resource

from texas_turnout_scraper.civix.read_client import cache_key, version_key
from texas_turnout_scraper.early_vote_aggregates import (
    build_by_day_series,
    build_county_by_day_series,
    build_cumulative_totals,
)
from texas_turnout_scraper.early_vote_roster_rows import (
    ROW_TABLE_SQL,
    VERSION_TABLE_SQL,
    day_number,
)
from texas_turnout_scraper.early_vote_sync import hash_key
from texas_turnout_scraper.first_seen import (
    FirstSeen,
    Status,
    annotate,
    cacheable,
    load_first_seen,
)
from texas_turnout_scraper.first_seen_build import run_first_seen_build_on_store
from texas_turnout_scraper.tx_counties import COUNTY_FIPS, county_fips, county_key

ELECTION = "53815"
D1, D2, D3 = "2026-10-20", "2026-10-21", "2026-10-24"
ROOT = Path(__file__).resolve().parents[2]


class _Store:
    """Roster tables on sqlite with the real DDL; the build's REST-shaped store."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(ROW_TABLE_SQL)
        self.conn.execute(VERSION_TABLE_SQL)
        self.writes = 0
        self.next_hash = 0

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db"

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        self.writes += 1
        for sql, params in statements:
            self.conn.execute(sql, params)

    def add(
        self,
        ev_date: str,
        vuid: str,
        method: str = "IN-PERSON",
        county: str = "TRAVIS",
        *,
        removed_in: int | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO early_vote_roster_version VALUES (?, ?, 1, 'f', 's', 0, 0)",
            (ELECTION, ev_date),
        )
        self.next_hash += 1
        self.conn.execute(
            "INSERT INTO early_vote_roster_row (county, voter_name, id_voter, voting_method, "
            "precinct, day, row_hash, occ, extra, added_in, removed_in) "
            "VALUES (?, 'FAKE PERSON', ?, ?, '1', ?, ?, 0, '{}', 1, ?)",
            (county, vuid, method, day_number(ev_date), self.next_hash, removed_in),
        )

    def bump(self, ev_date: str) -> None:
        self.conn.execute(
            "UPDATE early_vote_roster_version SET version = version + 1 WHERE ev_date = ?",
            (ev_date,),
        )

    def build(self, *, dry_run: bool = False):
        result = run_first_seen_build_on_store(self, ELECTION, dry_run=dry_run)
        assert result is not None
        return result


class _Prepared:
    def __init__(self, conn: sqlite3.Connection, sql: str) -> None:
        self._conn, self._sql, self._params = conn, sql, ()

    def bind(self, *params: object) -> _Prepared:
        self._params = params
        return self

    async def first(self) -> object | None:
        rows = (await self.all())["results"]
        return rows[0] if rows else None

    async def all(self) -> dict[str, list[dict[str, Any]]]:
        cur = self._conn.execute(self._sql, self._params)
        cols = [c[0] for c in cur.description]
        return {"results": [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]}


class _Binding:
    """The Worker's async D1 binding over the same sqlite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def prepare(self, sql: str) -> _Prepared:
        return _Prepared(self._conn, sql)


def _oracle_first_days(store: _Store) -> dict[str, Counter[str]]:
    """New voters per day, by county, straight from the live rows in Python."""
    first: dict[str, tuple[int, str]] = {}
    for vuid, day, county in store.conn.execute(
        "SELECT id_voter, day, county FROM early_vote_roster_row WHERE removed_in IS NULL"
    ):
        if vuid not in first or day < first[vuid][0]:
            first[vuid] = (day, county)
    by_day: dict[str, Counter[str]] = {}
    for day, county in first.values():
        iso = dt.date.fromordinal(day).isoformat()
        by_day.setdefault(iso, Counter())[county] += 1
    return by_day


def _random_roster(seed: int) -> _Store:
    rng = random.Random(seed)
    store = _Store()
    days = [D1, D2, D3]
    counties = ["TRAVIS", "LASALLE", "HARRIS"]
    for n in range(400):
        vuid = str(rng.randrange(10**9, 10**10)) if n % 7 else f"10000{n:05d}"
        home = rng.choice(counties)
        for ev_date in rng.sample(days, rng.choice([1, 1, 1, 2, 3])):
            method = rng.choice(["IN-PERSON", "MAIL-IN"])
            removed = 2 if rng.random() < 0.05 else None
            store.add(ev_date, vuid, method, home, removed_in=removed)
            if rng.random() < 0.03:
                store.add(ev_date, vuid, method, home)
    return store


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_build_matches_a_count_distinct_oracle(seed: int) -> None:
    store = _random_roster(seed)
    result = store.build()
    distinct = store.conn.execute(
        "SELECT COUNT(DISTINCT id_voter) FROM early_vote_roster_row WHERE removed_in IS NULL"
    ).fetchone()[0]
    assert result.status == "wrote"
    assert result.distinct_voters == distinct
    stored = store.conn.execute(
        "SELECT ev_date, county, SUM(n) FROM early_vote_first_seen GROUP BY 1, 2"
    ).fetchall()
    got = {(d, c): n for d, c, n in stored}
    want = {(d, c): n for d, by in _oracle_first_days(store).items() for c, n in by.items()}
    assert got == want


def test_repeats_and_removed_rows_count_once_on_the_first_live_day() -> None:
    store = _Store()
    store.add(D1, "0000000001", county="LASALLE")
    store.add(D3, "0000000001", county="LASALLE")
    store.add(D2, "0000000002")
    store.add(D2, "0000000002")
    store.add(D1, "0000000003", removed_in=1)
    store.add(D3, "0000000003", "MAIL-IN")
    store.build()
    rows = store.conn.execute(
        "SELECT ev_date, county, voting_method, n FROM early_vote_first_seen ORDER BY 1, 2, 3"
    ).fetchall()
    assert rows == [
        (D1, "LASALLE", "in_person", 1),
        (D2, "TRAVIS", "in_person", 1),
        (D3, "TRAVIS", "mail_in", 1),
    ]


def test_an_unchanged_roster_is_not_rebuilt_and_a_new_version_is() -> None:
    store = _Store()
    store.add(D1, "0000000001")
    assert store.build().status == "wrote"
    assert store.build().status == "unchanged"
    assert store.writes == 1
    store.bump(D1)
    assert store.build().status == "wrote"


def test_dry_run_writes_nothing() -> None:
    store = _Store()
    store.add(D1, "0000000001")
    result = store.build(dry_run=True)
    assert (result.status, result.distinct_voters, store.writes) == ("dry_run", 1, 0)


@pytest.mark.asyncio
async def test_the_worker_reads_current_then_stale_after_a_version_changes() -> None:
    store = _Store()
    binding = _Binding(store.conn)
    assert (await load_first_seen(binding, ELECTION)).status == "missing"
    store.add(D1, "0000000001")
    store.build()
    loaded = await load_first_seen(binding, ELECTION)
    assert loaded.status == "current"
    assert loaded.totals({D1}) == (1, 0)
    store.bump(D1)
    assert (await load_first_seen(binding, ELECTION)).status == "stale"


def _any(body: dict[str, object]) -> dict[str, Any]:
    return cast("dict[str, Any]", body)


def _day(ev_date: str, rows: dict[str, tuple[int, int]]) -> dict[str, object]:
    by_county = [
        {"county": c, "total": ip + mail, "in_person": ip, "mail_in": mail}
        for c, (ip, mail) in sorted(rows.items())
    ]
    ip = sum(v[0] for v in rows.values())
    mail = sum(v[1] for v in rows.values())
    return {
        "source_election_id": ELECTION,
        "payload_kind": "statewide_roster",
        "ev_date": ev_date,
        "statewide": {"total": ip + mail, "in_person": ip, "mail_in": mail},
        "by_county": by_county,
    }


# Day 3 re-lists the two LASALLE voters first seen on day 1 and adds one new TRAVIS voter.
_DAYS = (
    (D1, _day(D1, {"LASALLE": (2, 0)})),
    (D3, _day(D3, {"LASALLE": (2, 0), "TRAVIS": (0, 1)})),
)
_SEEN = FirstSeen(
    "current",
    {(D1, "LASALLE", "in_person"): 2, (D3, "TRAVIS", "mail_in"): 1},
)


def test_cumulative_totals_publish_rows_and_unique_voters_separately() -> None:
    body = _any(build_cumulative_totals(source_election_id=ELECTION, days=_DAYS))
    annotate(body, "totals", _SEEN, ev_date=None)
    assert body["statewide"] == {
        "total": 5,
        "in_person": 4,
        "mail_in": 1,
        "rows_reported": 5,
        "unique_voters": 3,
        "unique_in_person": 2,
        "unique_mail_in": 1,
    }
    lasalle = body["by_county"][0]
    assert (lasalle["county_fips"], lasalle["rows_reported"], lasalle["unique_voters"]) == (
        "48283",
        4,
        2,
    )
    assert body["dedupe"] == {"status": "current", "basis": "first_appearance"}
    assert body["county_fips_unmatched"] == 0
    assert cacheable(body)


def test_by_day_marks_restated_days_and_runs_a_distinct_cumulative() -> None:
    body = _any(build_by_day_series(source_election_id=ELECTION, days=_DAYS))
    annotate(body, "by-day", _SEEN, ev_date=None)
    first, second = body["series"]
    assert (first["new_voters"], first["restated_rows"], first["restated"]) == (2, 0, False)
    assert (second["total"], second["new_voters"], second["restated_rows"]) == (3, 1, 2)
    assert second["restated"] is True
    assert (second["cumulative_total"], second["cumulative_unique_voters"]) == (5, 3)


def test_one_day_and_county_by_day_publish_new_voters() -> None:
    one = _any(annotate(dict(_DAYS[1][1], semantics="daily"), "totals", _SEEN, ev_date=D3))
    assert (one["statewide"]["new_voters"], one["statewide"]["restated_rows"]) == (1, 2)
    series = _any(build_county_by_day_series(source_election_id=ELECTION, days=_DAYS))
    annotate(series, "county-by-day", _SEEN, ev_date=None)
    day3 = {row["county"]: row for row in series["series"][1]["by_county"]}
    assert (day3["LASALLE"]["new_voters"], day3["LASALLE"]["restated_rows"]) == (0, 2)
    assert (day3["TRAVIS"]["new_voters"], day3["TRAVIS"]["county_fips"]) == (1, "48453")


@pytest.mark.parametrize("status", ["stale", "missing"])
def test_stale_or_missing_counts_are_null_and_never_cached(status: Status) -> None:
    body = _any(build_cumulative_totals(source_election_id=ELECTION, days=_DAYS))
    annotate(body, "totals", FirstSeen(status), ev_date=None)
    assert body["statewide"]["unique_voters"] is None
    assert body["statewide"]["rows_reported"] == 5
    assert body["dedupe"]["status"] == status
    assert not cacheable(body)


def test_turnout_payloads_are_not_deduplicated_and_unknown_counties_are_counted() -> None:
    body: dict[str, Any] = {
        "payload_kind": "turnout",
        "statewide": {"total": 1, "in_person": 1, "mail_in": 0},
        "by_county": [{"county": "NOT A COUNTY", "total": 1, "in_person": 1, "mail_in": 0}],
    }
    annotate(body, "totals", _SEEN, ev_date=None)
    assert body["dedupe"] == {"status": "not_applicable"}
    assert body["by_county"][0]["county_fips"] is None
    assert body["county_fips_unmatched"] == 1
    assert cacheable(body)


@pytest.mark.parametrize(
    ("name", "fips"),
    [("LASALLE", "48283"), ("La Salle", "48283"), ("DE WITT", "48123"), ("DeWitt", "48123")],
)
def test_county_spellings_resolve_to_one_fips(name: str, fips: str) -> None:
    assert county_fips(name) == fips


def test_the_crosswalk_matches_the_geography_file_and_the_vendored_table() -> None:
    areas = json.loads((ROOT / "data/geo/tx_geo.json").read_text())["areas"]
    geo = {
        county_key(a["name"]): "48" + str(a["code"]).zfill(3)
        for a in areas
        if a["kind"] == "county"
    }
    assert COUNTY_FIPS == geo
    assert len(COUNTY_FIPS) == 254
    path = (
        ROOT
        / "vendor/voterfile-audit-pipeline/src/voterfile_audit_pipeline/reference/county_fips.py"
    )
    spec = importlib.util.spec_from_file_location("vendored_county_fips", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert {county_key(k): v for k, v in module.TEXAS_COUNTY_FIPS.items()} == COUNTY_FIPS


class _Kv:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def put(self, key: str, value: str, *, expiration_ttl: int | None) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


class _BodyStore:
    def __init__(self, status: Status) -> None:
        self.status = status

    async def build_body(
        self, source_election_id: str, segments: tuple[str, ...], *, kv_sha256: str
    ) -> dict[str, object] | None:
        body = build_cumulative_totals(source_election_id=ELECTION, days=_DAYS)
        return annotate(body, "totals", FirstSeen(self.status), ev_date=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "kept"), [("stale", False), ("current", True)])
async def test_a_stale_body_is_served_without_an_etag_and_never_cached(
    status: Status, kept: bool
) -> None:
    kv = _Kv({version_key(ELECTION): "3", hash_key(ELECTION): "sha"})
    segments = ("aggregates", "totals")
    outcome = await read_resource(
        ResourceQuery(source_election_id=ELECTION, segments=segments, if_none_match=None),
        kv,
        _BodyStore(status),
    )
    assert outcome.status_code == 200
    assert (outcome.etag is not None) is kept
    assert (cache_key(ELECTION, 3, *segments, BODY_SHAPE) in kv.values) is kept
