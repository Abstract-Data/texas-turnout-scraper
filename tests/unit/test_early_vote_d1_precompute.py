"""Loader precomputed aggregates: parity, migration idempotency, dry-run counts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from texas_turnout_scraper.early_vote_d1_precompute import (
    PRECOMPUTE_SCHEMA_CREATES,
    aggregate_body_from_decoded,
    load_precomputed_body,
    precompute_statements,
)
from texas_turnout_scraper.early_vote_sync import EmptyHashStore, sync_early_vote

ROSTER_FIXTURE = (
    Path(__file__).parent.parent / "fixtures" / "early_vote" / "statewide_roster_envelope.json"
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


def _apply_schema(conn: sqlite3.Connection) -> None:
    for sql in PRECOMPUTE_SCHEMA_CREATES:
        conn.execute(sql)
    conn.commit()


def _insert_precomputed(
    conn: sqlite3.Connection,
    election_id: str,
    file_id: str,
    decoded: bytes,
    *,
    ev_date: str | None,
) -> None:
    body = aggregate_body_from_decoded(
        decoded,
        source_election_id=election_id,
        ev_date_iso=ev_date,
    )
    for sql, params in precompute_statements(
        election_id,
        file_id,
        body,
        ev_date_iso=ev_date,
    ):
        conn.execute(sql, params)
    conn.commit()


def _turnout_decoded() -> bytes:
    payload = {
        "turnout_by_county": [
            {
                "name": "TRAVIS",
                "total_in_person_votes_for_election": 10,
                "total_mail_votes_for_election": 2,
            },
            {
                "name": "HARRIS",
                "total_in_person_votes_for_election": 1,
                "total_mail_votes_for_election": 0,
            },
        ]
    }
    return json.dumps(payload).encode()


def test_schema_migration_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    _apply_schema(conn)
    _apply_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    }
    assert "early_vote_file_aggregate" in tables
    assert "early_vote_county_aggregate" in tables


@pytest.mark.asyncio
async def test_precomputed_matches_decoded_body() -> None:
    decoded = _turnout_decoded()
    expected = aggregate_body_from_decoded(
        decoded,
        source_election_id="53815",
        ev_date_iso="2026-10-20",
    )
    conn = sqlite3.connect(":memory:")
    _apply_schema(conn)
    _insert_precomputed(conn, "53815", "f-test", decoded, ev_date="2026-10-20")
    loaded = await load_precomputed_body(_SqliteD1(conn), "53815", "f-test", ev_date="2026-10-20")
    assert loaded == expected


def test_dry_run_reports_aggregate_row_count() -> None:
    body = ROSTER_FIXTURE.read_bytes()
    response = httpx.Response(200, content=body)
    result = sync_early_vote(
        "53815",
        response,
        EmptyHashStore(),
        dry_run=True,
        payload_kind="statewide_roster",
    )
    assert result.plan is not None
    assert result.plan.aggregate_rows is not None
    assert result.plan.aggregate_rows >= 2


@pytest.mark.asyncio
async def test_precomputed_parity_with_streaming_on_fixture() -> None:
    from tests.unit.test_early_vote_d1_aggregate import _AggregateD1, _turnout_bytes
    from texas_turnout_scraper.early_vote_d1_aggregate import build_aggregate_resource
    from texas_turnout_scraper.early_vote_d1_precompute_read import (
        build_precomputed_aggregate_resource,
    )
    from texas_turnout_scraper.early_vote_sync import sha256_decoded

    decoded = _turnout_bytes()
    digest = sha256_decoded(decoded)
    db = _AggregateD1(
        pointer="f53815",
        file_id="f53815",
        sha256=digest,
        decoded=decoded,
        days=[{"ev_date": "2026-10-20", "file_id": "f53815", "sha256": digest}],
    )
    streamed = await build_aggregate_resource(
        db,
        "53815",
        "totals",
        kv_sha256=digest,
        ev_date="2026-10-20",
    )
    conn = sqlite3.connect(":memory:")
    _apply_schema(conn)
    _insert_precomputed(conn, "53815", "f53815", decoded, ev_date="2026-10-20")

    class _Hybrid(_AggregateD1):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self._sqlite = conn

        def first(self, sql: str, args: tuple[object, ...]) -> object | None:
            if "early_vote_file_aggregate" in sql:
                cur = self._sqlite.execute(sql, args)
                row = cur.fetchone()
                if row is None:
                    return None
                cols = [c[0] for c in cur.description]
                return dict(zip(cols, row, strict=True))
            return super().first(sql, args)

        def all(self, sql: str, args: tuple[object, ...]) -> object:
            if "early_vote_county_aggregate" in sql:
                cur = self._sqlite.execute(sql, args)
                cols = [c[0] for c in cur.description]
                rows = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
                return {"results": rows, "success": True}
            return super().all(sql, args)

    hybrid = _Hybrid(
        pointer="f53815",
        file_id="f53815",
        sha256=digest,
        decoded=decoded,
        days=[{"ev_date": "2026-10-20", "file_id": "f53815", "sha256": digest}],
    )
    precomputed = await build_precomputed_aggregate_resource(
        hybrid,
        "53815",
        "totals",
        kv_sha256=digest,
        ev_date="2026-10-20",
    )
    assert streamed is not None
    assert precomputed is not None
    assert precomputed.pop("semantics") == "daily"
    assert precomputed == streamed


def test_turnout_duplicate_counties_merge_before_insert() -> None:
    decoded = json.dumps(
        {
            "turnout_by_county": [
                {
                    "name": "TRAVIS",
                    "total_in_person_votes_for_election": 1,
                    "total_mail_votes_for_election": 0,
                },
                {
                    "name": "TRAVIS",
                    "total_in_person_votes_for_election": 2,
                    "total_mail_votes_for_election": 1,
                },
            ]
        }
    ).encode()
    body = aggregate_body_from_decoded(decoded, source_election_id="53815")
    by_county = body.get("by_county")
    assert isinstance(by_county, list)
    assert len(by_county) == 1
    assert by_county[0]["in_person"] == 3
    assert by_county[0]["mail_in"] == 1


def test_precompute_statements_file_row_last_marks_complete_write() -> None:
    body = aggregate_body_from_decoded(_turnout_decoded(), source_election_id="53815")
    statements = precompute_statements("53815", "f1", body)
    assert statements[-1][0].startswith("INSERT INTO early_vote_file_aggregate")


def test_precompute_statements_idempotent_replace() -> None:
    decoded = _turnout_decoded()
    body = aggregate_body_from_decoded(decoded, source_election_id="53815")
    first = precompute_statements("53815", "f1", body)
    second = precompute_statements("53815", "f1", body)
    assert first[0][0] == second[0][0]
    assert "DELETE" in first[0][0]
