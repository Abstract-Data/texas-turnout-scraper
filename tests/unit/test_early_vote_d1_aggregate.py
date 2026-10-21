"""Parity between D1 SQL aggregate path and legacy Python decoders."""

from __future__ import annotations

import json

import pytest

from texas_turnout_scraper.early_vote_aggregates import aggregates_from_decoded
from texas_turnout_scraper.early_vote_d1_aggregate import (
    _ROSTER_BATCH_SQL,
    build_aggregate_resource,
)
from texas_turnout_scraper.early_vote_sync import iter_chunk_b64, sha256_decoded


class _Prepared:
    def __init__(self, db: _AggregateD1, sql: str) -> None:
        self._db = db
        self._sql = sql
        self._args: tuple[object, ...] = ()

    def bind(self, *args: object) -> _Prepared:
        self._args = args
        return self

    async def first(self) -> object | None:
        return self._db.first(self._sql, self._args)

    async def all(self) -> object:
        return self._db.all(self._sql, self._args)

    async def run(self) -> dict[str, object]:
        return self._db.run(self._sql, self._args)


class _AggregateD1:
    """D1 stub that runs json_each SQL in sqlite3 for parity tests."""

    def __init__(
        self,
        *,
        pointer: str,
        file_id: str,
        sha256: str,
        decoded: bytes,
        days: list[dict[str, str]] | None = None,
        byte_length: int | None = None,
    ) -> None:
        self._pointer = pointer
        self._file_id = file_id
        self._sha256 = sha256
        self._chunks = dict(enumerate(iter_chunk_b64(decoded)))
        self._decoded = decoded
        self._days = days or []
        self._byte_length = byte_length if byte_length is not None else len(decoded)

    def prepare(self, sql: str) -> _Prepared:
        return _Prepared(self, sql)

    def first(self, sql: str, args: tuple[object, ...]) -> object | None:
        if "early_vote_pointer" in sql:
            return {"file_id": self._pointer}
        if "early_vote_day" in sql and "ev_date = ?" in sql:
            ev_date = str(args[1])
            for day in self._days:
                if day["ev_date"] == ev_date:
                    return day
            return None
        if "byte_length" in sql:
            return {"byte_length": self._byte_length}
        if "chunk_index = ?" in sql and "payload_b64" in sql:
            index = int(args[2])
            payload = self._chunks.get(index)
            return {"payload_b64": payload} if payload else None
        if "json_each(?1" in sql:
            return self._sqlite_first(sql, args)
        return None

    def all(self, sql: str, args: tuple[object, ...]) -> object:
        return self.run(sql, args)

    def run(self, sql: str, args: tuple[object, ...]) -> dict[str, object]:
        if "FROM early_vote_file" in sql:
            return {
                "results": [
                    {
                        "file_id": self._file_id,
                        "sha256": self._sha256,
                        "chunk_count": len(self._chunks),
                        "chunks_present": len(self._chunks),
                    }
                ],
                "success": True,
            }
        if "chunk_index FROM early_vote_chunk" in sql.replace("\n", " "):
            return {
                "results": [{"chunk_index": index} for index in sorted(self._chunks.keys())],
                "success": True,
            }
        if "FROM early_vote_day" in sql and "ORDER BY" in sql:
            return {"results": list(self._days), "success": True}
        rows = self._sqlite_all(sql, args)
        return {"results": rows, "success": True}

    def _sqlite_all(self, sql: str, args: tuple[object, ...]) -> list[dict[str, object]]:
        import sqlite3

        conn = sqlite3.connect(":memory:")
        try:
            cur = conn.execute(sql, args)
            columns = [col[0] for col in cur.description or []]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
        finally:
            conn.close()

    def _sqlite_first(self, sql: str, args: tuple[object, ...]) -> object | None:
        rows = self._sqlite_all(sql, args)
        return rows[0] if rows else None


def _turnout_bytes() -> bytes:
    payload = {
        "turnout_by_county": [
            {
                "name": "TRAVIS",
                "total_in_person_votes_for_election": 10,
                "total_mail_votes_for_election": 2,
            },
            {
                "name": "HARRIS",
                "total_in_person_votes_for_election": 5,
                "total_mail_votes_for_election": 1,
            },
        ]
    }
    return json.dumps(payload).encode()


def _roster_bytes() -> bytes:
    return (
        b"COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\n"
        b"TRAVIS,Secret,0000000001,IN-PERSON,1\n"
        b"TRAVIS,Secret,0000000002,MAIL-IN,1\n"
        b"HARRIS,Secret,0000000003,IN-PERSON,2\n"
    )


@pytest.fixture
def turnout_db() -> _AggregateD1:
    decoded = _turnout_bytes()
    digest = sha256_decoded(decoded)
    return _AggregateD1(
        pointer="f53815",
        file_id="f53815",
        sha256=digest,
        decoded=decoded,
        days=[
            {"ev_date": "2026-10-20", "file_id": "f53815", "sha256": digest},
        ],
    )


@pytest.fixture
def roster_db() -> _AggregateD1:
    decoded = _roster_bytes()
    digest = sha256_decoded(decoded)
    return _AggregateD1(
        pointer="f53815",
        file_id="f53815",
        sha256=digest,
        decoded=decoded,
        days=[
            {"ev_date": "2026-10-20", "file_id": "f53815", "sha256": digest},
        ],
    )


@pytest.mark.asyncio
async def test_sql_totals_matches_python_turnout(turnout_db: _AggregateD1) -> None:
    digest = turnout_db._sha256
    expected = aggregates_from_decoded(turnout_db._decoded, source_election_id="53815")
    actual = await build_aggregate_resource(turnout_db, "53815", "totals", kv_sha256=digest)
    assert actual == expected


@pytest.mark.asyncio
async def test_sql_county_matches_python_turnout(turnout_db: _AggregateD1) -> None:
    digest = turnout_db._sha256
    expected = aggregates_from_decoded(turnout_db._decoded, source_election_id="53815")
    actual = await build_aggregate_resource(turnout_db, "53815", "county", kv_sha256=digest)
    assert actual["by_county"] == expected["by_county"]


@pytest.mark.asyncio
async def test_sql_by_method_matches_python_turnout(turnout_db: _AggregateD1) -> None:
    digest = turnout_db._sha256
    expected = aggregates_from_decoded(turnout_db._decoded, source_election_id="53815")
    actual = await build_aggregate_resource(turnout_db, "53815", "by-method", kv_sha256=digest)
    assert actual is not None
    assert actual["statewide"] == {
        "in_person": expected["statewide"]["in_person"],
        "mail_in": expected["statewide"]["mail_in"],
    }


@pytest.mark.asyncio
async def test_sql_by_day_matches_python_turnout(turnout_db: _AggregateD1) -> None:
    digest = turnout_db._sha256
    expected = aggregates_from_decoded(turnout_db._decoded, source_election_id="53815")
    actual = await build_aggregate_resource(turnout_db, "53815", "by-day", kv_sha256=digest)
    assert actual is not None
    assert actual["series"][0]["total"] == expected["statewide"]["total"]


@pytest.mark.asyncio
async def test_sql_county_by_day_matches_python_turnout(turnout_db: _AggregateD1) -> None:
    digest = turnout_db._sha256
    expected = aggregates_from_decoded(turnout_db._decoded, source_election_id="53815")
    actual = await build_aggregate_resource(turnout_db, "53815", "county-by-day", kv_sha256=digest)
    assert actual is not None
    assert actual["series"][0]["by_county"] == expected["by_county"]


@pytest.mark.asyncio
async def test_sql_totals_matches_python_roster(roster_db: _AggregateD1) -> None:
    digest = roster_db._sha256
    expected = aggregates_from_decoded(roster_db._decoded, source_election_id="53815")
    actual = await build_aggregate_resource(roster_db, "53815", "totals", kv_sha256=digest)
    assert actual == expected


@pytest.mark.asyncio
async def test_roster_batch_sql_groups_methods() -> None:
    import sqlite3

    payload = json.dumps(
        [
            {"county": "TRAVIS", "method": "IN-PERSON"},
            {"county": "TRAVIS", "method": "MAIL-IN"},
        ]
    )
    conn = sqlite3.connect(":memory:")
    row = conn.execute(_ROSTER_BATCH_SQL, (payload,)).fetchone()
    assert row[0] == "TRAVIS"
    assert row[1] == 1
    assert row[2] == 1
