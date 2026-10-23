"""Listing, per-day streaming and all-days streaming of stored roster files from D1 chunks."""

from __future__ import annotations

import sqlite3

import pytest

from texas_turnout_scraper import early_vote_sync
from texas_turnout_scraper.early_vote_d1_precompute import (
    PRECOMPUTE_SCHEMA_CREATES,
    aggregate_body_from_decoded,
    precompute_statements,
)
from texas_turnout_scraper.early_vote_d1_roster import (
    find_day,
    list_days,
    roster_etag,
    stream_all_days,
    stream_day,
)
from texas_turnout_scraper.early_vote_sync import (
    _DAY_UPSERT_SQL,
    _SCHEMA_CREATES,
    build_publish_plan,
    sha256_decoded,
)

HEADER = "ID_VOTER,VOTER_NAME,VOTING_METHOD,PRECINCT,COUNTY\n"
DAY1 = (
    HEADER
    + '0000000001,"DOE, JANE",MAIL-IN,101,TRAVIS\n'
    + "0000000002,JOHN Q PUBLIC,IN-PERSON,102,BEXAR\n"
).encode()
DAY2 = (HEADER + "0000000003,MARY ROE,IN-PERSON,7,HARRIS\n").encode()


class _Prepared:
    def __init__(self, conn: sqlite3.Connection, sql: str) -> None:
        self._conn, self._sql, self._args = conn, sql, ()

    def bind(self, *args: object) -> _Prepared:
        self._args = args
        return self

    async def first(self) -> object | None:
        cur = self._conn.execute(self._sql, self._args)
        row = cur.fetchone()
        return None if row is None else dict(zip([c[0] for c in cur.description], row, strict=True))

    async def all(self) -> dict[str, object]:
        cur = self._conn.execute(self._sql, self._args)
        cols = [c[0] for c in cur.description]
        return {"results": [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]}


class _Db:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.queries = 0

    def prepare(self, sql: str) -> _Prepared:
        self.queries += 1
        return _Prepared(self.conn, sql)


def _store_day(conn: sqlite3.Connection, ev_date: str, decoded: bytes, *, drop: int | None = None):
    digest = sha256_decoded(decoded)
    file_id = f"f-{ev_date}"
    plan = build_publish_plan("53815", decoded, digest, file_id)
    for sql, params in plan.chunk_inserts:
        conn.execute(sql, params)
    conn.execute(plan.file_insert[0], plan.file_insert[1])
    conn.execute(_DAY_UPSERT_SQL, ("53815", ev_date, digest, file_id))
    body = aggregate_body_from_decoded(decoded, source_election_id="53815", ev_date_iso=ev_date)
    for sql, params in precompute_statements("53815", file_id, body, ev_date_iso=ev_date):
        conn.execute(sql, params)
    if drop is not None:
        conn.execute(
            "DELETE FROM early_vote_chunk WHERE file_id = ? AND chunk_index = ?", (file_id, drop)
        )
    conn.commit()
    return digest


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> _Db:
    monkeypatch.setattr(early_vote_sync, "CHUNK_RAW_BYTES", 16)
    conn = sqlite3.connect(":memory:")
    for sql in (*_SCHEMA_CREATES, *PRECOMPUTE_SCHEMA_CREATES):
        conn.execute(sql)
    _store_day(conn, "2026-10-19", DAY1)
    _store_day(conn, "2026-10-20", DAY2)
    return _Db(conn)


async def _collect(stream) -> bytes:
    return b"".join([piece async for piece in stream])


@pytest.mark.asyncio
async def test_list_days_has_rows_hash_and_size(db: _Db) -> None:
    days = await list_days(db, "53815")
    assert [d.ev_date for d in days] == ["2026-10-19", "2026-10-20"]
    assert [d.rows for d in days] == [2, 1]
    assert days[0].sha256 == sha256_decoded(DAY1)
    assert days[0].byte_length == len(DAY1)
    assert days[0].chunk_count > 3


@pytest.mark.asyncio
async def test_incomplete_days_are_not_listed_or_served(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(":memory:")
    for sql in (*_SCHEMA_CREATES, *PRECOMPUTE_SCHEMA_CREATES):
        conn.execute(sql)
    monkeypatch.setattr(early_vote_sync, "CHUNK_RAW_BYTES", 16)
    _store_day(conn, "2026-10-19", DAY1, drop=1)
    db = _Db(conn)
    assert await list_days(db, "53815") == ()
    assert await find_day(db, "53815", "2026-10-19") is None


@pytest.mark.asyncio
async def test_stream_day_returns_the_exact_stored_bytes_across_batches(db: _Db) -> None:
    day = await find_day(db, "53815", "2026-10-19")
    assert day is not None
    assert await _collect(stream_day(db, "53815", day, batch_chunks=2)) == DAY1
    assert await _collect(stream_day(db, "53815", day, batch_chunks=1000)) == DAY1


@pytest.mark.asyncio
async def test_unknown_day_is_none(db: _Db) -> None:
    assert await find_day(db, "53815", "2099-01-01") is None


@pytest.mark.asyncio
async def test_all_days_adds_one_header_and_a_date_column(db: _Db) -> None:
    days = await list_days(db, "53815")
    body = await _collect(stream_all_days(db, "53815", days, batch_chunks=2))
    assert body.decode().splitlines() == [
        "EV_DATE,ID_VOTER,VOTER_NAME,VOTING_METHOD,PRECINCT,COUNTY",
        '2026-10-19,0000000001,"DOE, JANE",MAIL-IN,101,TRAVIS',
        "2026-10-19,0000000002,JOHN Q PUBLIC,IN-PERSON,102,BEXAR",
        "2026-10-20,0000000003,MARY ROE,IN-PERSON,7,HARRIS",
    ]


@pytest.mark.asyncio
async def test_all_days_handles_a_missing_final_newline_and_crlf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(early_vote_sync, "CHUNK_RAW_BYTES", 8)
    conn = sqlite3.connect(":memory:")
    for sql in (*_SCHEMA_CREATES, *PRECOMPUTE_SCHEMA_CREATES):
        conn.execute(sql)
    _store_day(conn, "2026-10-19", b"ID_VOTER,COUNTY\r\n0000000001,TRAVIS\r\n0000000002,BEXAR")
    db = _Db(conn)
    days = await list_days(db, "53815")
    body = await _collect(stream_all_days(db, "53815", days, batch_chunks=1))
    assert body == (
        b"EV_DATE,ID_VOTER,COUNTY\r\n2026-10-19,0000000001,TRAVIS\r\n2026-10-19,0000000002,BEXAR\n"
    )


@pytest.mark.asyncio
async def test_chunks_are_read_in_batches_not_one_query_each(db: _Db) -> None:
    day = await find_day(db, "53815", "2026-10-19")
    assert day is not None
    before = db.queries
    await _collect(stream_day(db, "53815", day, batch_chunks=1000))
    assert db.queries - before <= 2


@pytest.mark.asyncio
async def test_etag_changes_when_any_day_changes(db: _Db) -> None:
    days = await list_days(db, "53815")
    full = roster_etag(days)
    assert full == roster_etag(days)
    assert roster_etag(days[:1]) != full
    assert roster_etag(days[:1]) != days[0].sha256
