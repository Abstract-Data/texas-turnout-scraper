"""404 vs 503 semantics and partial-day rollup for precomputed reads."""

from __future__ import annotations

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

    def prepare(self, sql: str) -> _SqlitePrepared:
        return _SqlitePrepared(self._conn, sql)


class _SqlitePrepared:
    def __init__(self, conn: sqlite3.Connection, sql: str) -> None:
        self._conn = conn
        self._sql = sql
        self._args: tuple[object, ...] = ()

    def bind(self, *args: object) -> _SqlitePrepared:
        self._args = args
        return self

    async def first(self) -> object | None:
        cur = self._conn.execute(self._sql, self._args)
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row, strict=True))

    async def all(self) -> dict[str, object]:
        cur = self._conn.execute(self._sql, self._args)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        return {"results": rows, "success": True}

    async def run(self) -> dict[str, object]:
        self._conn.execute(self._sql, self._args)
        self._conn.commit()
        return {"results": [], "success": True}


def _bootstrap_db(conn: sqlite3.Connection) -> None:
    for sql in _SCHEMA_CREATES:
        conn.execute(sql)
    for sql in PRECOMPUTE_SCHEMA_CREATES:
        conn.execute(sql)
    conn.commit()


def _insert_file(
    conn: sqlite3.Connection,
    election_id: str,
    file_id: str,
    digest: str,
    *,
    chunk_count: int = 1,
    payload_b64: str = "e30=",
) -> None:
    conn.execute(
        _FILE_INSERT_SQL,
        (election_id, file_id, digest, "3", str(chunk_count)),
    )
    for index in range(chunk_count):
        conn.execute(
            _CHUNK_INSERT_SQL,
            (election_id, file_id, str(index), payload_b64),
        )


@pytest.mark.asyncio
async def test_unknown_ev_date_returns_none_not_503() -> None:
    conn = sqlite3.connect(":memory:")
    _bootstrap_db(conn)
    _insert_file(conn, "53815", "f1", "abc")
    conn.execute(_POINTER_UPSERT_SQL, ("53815", "f1"))
    conn.commit()
    db = _SqliteD1(conn)
    result = await build_precomputed_aggregate_resource(
        db,
        "53815",
        "totals",
        kv_sha256="abc",
        ev_date="2099-01-01",
    )
    assert result is None


@pytest.mark.asyncio
async def test_missing_precompute_raises_not_computed() -> None:
    conn = sqlite3.connect(":memory:")
    _bootstrap_db(conn)
    _insert_file(conn, "53815", "f1", "abc")
    conn.execute(_POINTER_UPSERT_SQL, ("53815", "f1"))
    conn.commit()
    db = _SqliteD1(conn)
    with pytest.raises(AggregatesNotComputed):
        await build_precomputed_aggregate_resource(
            db,
            "53815",
            "totals",
            kv_sha256="abc",
        )


@pytest.mark.asyncio
async def test_by_day_skips_missing_day_serves_others() -> None:
    decoded = (
        b'{"turnout_by_county":[{"name":"TRAVIS",'
        b'"total_in_person_votes_for_election":1,"total_mail_votes_for_election":0}]}'
    )
    body = aggregate_body_from_decoded(
        decoded,
        source_election_id="53815",
        ev_date_iso="2026-10-20",
    )
    conn = sqlite3.connect(":memory:")
    _bootstrap_db(conn)
    _insert_file(conn, "53815", "f-day", "sha-day", chunk_count=1)
    _insert_file(conn, "53815", "f-miss", "sha-miss", chunk_count=1)
    conn.execute(_POINTER_UPSERT_SQL, ("53815", "f-day"))
    conn.execute(
        _DAY_UPSERT_SQL,
        ("53815", "2026-10-20", "sha-day", "f-day"),
    )
    conn.execute(
        _DAY_UPSERT_SQL,
        ("53815", "2026-10-21", "sha-miss", "f-miss"),
    )
    for sql, params in precompute_statements(
        "53815",
        "f-day",
        body,
        ev_date_iso="2026-10-20",
    ):
        conn.execute(sql, params)
    conn.commit()
    db = _SqliteD1(conn)
    series = await build_precomputed_aggregate_resource(
        db,
        "53815",
        "by-day",
        kv_sha256="sha-day",
    )
    assert series is not None
    rolled = series.get("series")
    assert isinstance(rolled, list)
    assert len(rolled) == 1
    assert rolled[0].get("ev_date") == "2026-10-20"
