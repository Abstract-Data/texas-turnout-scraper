"""Election-to-date totals over daily files, with an explicit semantics field."""

from __future__ import annotations

import json
import sqlite3

import pytest

from texas_turnout_scraper.early_vote_d1_precompute import (
    PRECOMPUTE_SCHEMA_CREATES,
    aggregate_body_from_decoded,
    precompute_statements,
)
from texas_turnout_scraper.early_vote_d1_precompute_read import (
    AggregatesNotComputed,
    build_precomputed_aggregate_resource,
)
from texas_turnout_scraper.early_vote_sync import (
    _CHUNK_INSERT_SQL,
    _DAY_UPSERT_SQL,
    _FILE_INSERT_SQL,
    _POINTER_UPSERT_SQL,
    _SCHEMA_CREATES,
)


class _SqliteD1:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def prepare(self, sql: str) -> _Prepared:
        return _Prepared(self._conn, sql)


class _Prepared:
    def __init__(self, conn: sqlite3.Connection, sql: str) -> None:
        self._conn, self._sql, self._args = conn, sql, ()

    def bind(self, *args: object) -> _Prepared:
        self._args = args
        return self

    async def first(self) -> object | None:
        cur = self._conn.execute(self._sql, self._args)
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([c[0] for c in cur.description], row, strict=True))

    async def all(self) -> dict[str, object]:
        cur = self._conn.execute(self._sql, self._args)
        cols = [c[0] for c in cur.description]
        return {"results": [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]}


def _turnout(**counties: tuple[int, int]) -> bytes:
    rows = [
        {
            "name": name,
            "total_in_person_votes_for_election": ip,
            "total_mail_votes_for_election": mail,
        }
        for name, (ip, mail) in counties.items()
    ]
    return json.dumps({"turnout_by_county": rows}).encode()


def _db(days: dict[str, bytes], *, skip_precompute: tuple[str, ...] = ()) -> _SqliteD1:
    conn = sqlite3.connect(":memory:")
    for sql in (*_SCHEMA_CREATES, *PRECOMPUTE_SCHEMA_CREATES):
        conn.execute(sql)
    for ev_date, decoded in days.items():
        file_id, digest = f"f-{ev_date}", f"sha-{ev_date}"
        conn.execute(_FILE_INSERT_SQL, ("53815", file_id, digest, "3", "1"))
        conn.execute(_CHUNK_INSERT_SQL, ("53815", file_id, "0", "e30="))
        conn.execute(_DAY_UPSERT_SQL, ("53815", ev_date, digest, file_id))
        if ev_date in skip_precompute:
            continue
        body = aggregate_body_from_decoded(
            decoded, source_election_id="53815", ev_date_iso=ev_date
        )
        for sql, params in precompute_statements("53815", file_id, body, ev_date_iso=ev_date):
            conn.execute(sql, params)
    last = max(days)
    conn.execute(_POINTER_UPSERT_SQL, ("53815", f"f-{last}"))
    conn.commit()
    return _SqliteD1(conn)


DAYS = {
    "2026-10-19": _turnout(TRAVIS=(10, 5), BEXAR=(3, 1)),
    "2026-10-20": _turnout(TRAVIS=(20, 0), HARRIS=(7, 2)),
}
KV = "sha-2026-10-20"


async def _get(db: _SqliteD1, resource: str, **kw: str) -> dict:
    result = await build_precomputed_aggregate_resource(
        db, "53815", resource, kv_sha256=KV, **kw
    )
    assert result is not None
    return result


@pytest.mark.asyncio
async def test_default_totals_sum_every_day() -> None:
    body = await _get(_db(DAYS), "totals")
    assert body["statewide"] == {"total": 48, "in_person": 40, "mail_in": 8}
    assert body["semantics"] == "cumulative"
    assert body["through_ev_date"] == "2026-10-20"
    assert body["days"] == ["2026-10-19", "2026-10-20"]
    by_county = {row["county"]: row for row in body["by_county"]}
    assert by_county["TRAVIS"] == {"county": "TRAVIS", "total": 35, "in_person": 30, "mail_in": 5}
    assert set(by_county) == {"TRAVIS", "BEXAR", "HARRIS"}


@pytest.mark.asyncio
async def test_a_given_date_stays_that_days_numbers() -> None:
    body = await _get(_db(DAYS), "totals", ev_date="2026-10-19")
    assert body["statewide"] == {"total": 19, "in_person": 13, "mail_in": 6}
    assert body["semantics"] == "daily"
    assert body["ev_date"] == "2026-10-19"


@pytest.mark.asyncio
async def test_county_and_by_method_default_to_cumulative() -> None:
    db = _db(DAYS)
    county = await _get(db, "county")
    by_method = await _get(db, "by-method")
    assert county["semantics"] == "cumulative"
    assert {r["county"]: r["total"] for r in county["by_county"]}["TRAVIS"] == 35
    assert by_method["semantics"] == "cumulative"
    assert by_method["statewide"] == {"in_person": 40, "mail_in": 8}


@pytest.mark.asyncio
async def test_by_day_adds_running_cumulative_totals() -> None:
    body = await _get(_db(DAYS), "by-day")
    assert body["semantics"] == "daily_with_cumulative"
    first, second = body["series"]
    assert (first["total"], first["cumulative_total"]) == (19, 19)
    assert (second["total"], second["cumulative_total"]) == (29, 48)
    assert second["cumulative_in_person"] == 40
    assert second["cumulative_mail_in"] == 8


@pytest.mark.asyncio
async def test_county_by_day_is_marked_daily() -> None:
    body = await _get(_db(DAYS), "county-by-day")
    assert body["semantics"] == "daily"


@pytest.mark.asyncio
async def test_cumulative_fails_fast_when_a_day_is_not_computed() -> None:
    db = _db(DAYS, skip_precompute=("2026-10-19",))
    with pytest.raises(AggregatesNotComputed):
        await build_precomputed_aggregate_resource(db, "53815", "totals", kv_sha256=KV)
