"""Backfill: days whose rows marker is missing or behind the day file get loaded, once."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from tests.unit.test_early_vote_roster_stream import DAY1, DAY2, HEADER, _store_day
from texas_turnout_scraper import early_vote_sync
from texas_turnout_scraper.early_vote_d1_precompute import PRECOMPUTE_SCHEMA_CREATES
from texas_turnout_scraper.early_vote_roster_rows import (
    VISIBLE_AT_SQL,
    day_number,
    visible_params,
)
from texas_turnout_scraper.early_vote_roster_rows_backfill import (
    run_roster_rows_backfill_on_store,
)
from texas_turnout_scraper.early_vote_sync import (
    _DAY_UPSERT_SQL,
    _SCHEMA_CREATES,
    build_publish_plan,
    sha256_decoded,
)


class _SqliteStore:
    """Just the store methods the backfill uses, backed by a real sqlite database."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.statement_calls = 0

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db"

    def _ensure_schema(self, database_id: str) -> None:
        for sql in (*_SCHEMA_CREATES, *PRECOMPUTE_SCHEMA_CREATES):
            self.conn.execute(sql)

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        self.statement_calls += 1
        for sql, params in statements:
            self.conn.execute(sql, params)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _SqliteStore:
    monkeypatch.setattr(early_vote_sync, "CHUNK_RAW_BYTES", 16)
    conn = sqlite3.connect(":memory:")
    store = _SqliteStore(conn)
    store._ensure_schema("db")
    _store_day(conn, "2026-10-19", DAY1)
    _store_day(conn, "2026-10-20", DAY2)
    return store


def _replace_day(conn: sqlite3.Connection, ev_date: str, decoded: bytes, file_id: str) -> None:
    digest = sha256_decoded(decoded)
    plan = build_publish_plan("53815", decoded, digest, file_id)
    for sql, params in plan.chunk_inserts:
        conn.execute(sql, params)
    conn.execute(plan.file_insert[0], plan.file_insert[1])
    conn.execute(_DAY_UPSERT_SQL, ("53815", ev_date, digest, file_id))


def _versions(store: _SqliteStore) -> list[tuple[Any, ...]]:
    return store.conn.execute(
        "SELECT ev_date, version, file_id, row_count, reject_count FROM early_vote_roster_version "
        "ORDER BY ev_date"
    ).fetchall()


def _visible(store: _SqliteStore, ev_date: str, version: int) -> list[str]:
    rows = store.conn.execute(
        f"SELECT voter_name FROM early_vote_roster_row WHERE day = ? AND {VISIBLE_AT_SQL}",
        (day_number(ev_date), *visible_params(version)),
    ).fetchall()
    return sorted(r[0] for r in rows)


def test_days_without_a_marker_get_their_rows(store: _SqliteStore) -> None:
    result = run_roster_rows_backfill_on_store(store, "53815", dry_run=False)
    assert (result.files_considered, result.files_written, result.status) == (2, 2, "wrote")
    assert (result.rows_written, result.rejects) == (3, 0)
    assert _versions(store) == [
        ("2026-10-19", 1, "f-2026-10-19", 2, 0),
        ("2026-10-20", 1, "f-2026-10-20", 1, 0),
    ]
    assert _visible(store, "2026-10-19", 1) == ["DOE, JANE", "JOHN Q PUBLIC"]


def test_a_second_run_writes_nothing(store: _SqliteStore) -> None:
    run_roster_rows_backfill_on_store(store, "53815", dry_run=False)
    calls = store.statement_calls
    again = run_roster_rows_backfill_on_store(store, "53815", dry_run=False)
    assert (again.files_written, again.files_skipped, again.status) == (0, 2, "unchanged")
    assert store.statement_calls == calls


def test_dry_run_reports_without_writing(store: _SqliteStore) -> None:
    result = run_roster_rows_backfill_on_store(store, "53815", dry_run=True)
    assert (result.files_written, result.status) == (2, "dry_run")
    assert _versions(store) == []


def test_only_the_day_whose_marker_is_behind_its_file_is_reloaded_as_a_diff(
    store: _SqliteStore,
) -> None:
    run_roster_rows_backfill_on_store(store, "53815", dry_run=False)
    newer = DAY1 + b"0000000009,NEW ARRIVAL,MAIL-IN,101,TRAVIS\n"
    _replace_day(store.conn, "2026-10-19", newer, "f2-2026-10-19")
    result = run_roster_rows_backfill_on_store(store, "53815", dry_run=False)
    assert (result.files_written, result.files_skipped, result.rows_written) == (1, 1, 1)
    assert _versions(store) == [
        ("2026-10-19", 2, "f2-2026-10-19", 3, 0),
        ("2026-10-20", 1, "f-2026-10-20", 1, 0),
    ]
    assert _visible(store, "2026-10-19", 1) == ["DOE, JANE", "JOHN Q PUBLIC"]
    assert _visible(store, "2026-10-19", 2) == ["DOE, JANE", "JOHN Q PUBLIC", "NEW ARRIVAL"]


def test_leftovers_of_a_partial_earlier_attempt_are_replaced_not_duplicated(
    store: _SqliteStore,
) -> None:
    store.conn.execute(
        "INSERT INTO early_vote_roster_row (day, row_hash, occ, county, voter_name, id_voter, "
        "voting_method, precinct, extra, added_in) VALUES (?, 7, 0, 'X', 'STALE', '1', 'M', '1', "
        "'{}', 1)",
        (day_number("2026-10-19"),),
    )
    run_roster_rows_backfill_on_store(store, "53815", dry_run=False)
    assert _visible(store, "2026-10-19", 1) == ["DOE, JANE", "JOHN Q PUBLIC"]
    assert store.conn.execute("SELECT COUNT(*) FROM early_vote_roster_row").fetchone() == (3,)


def test_an_incomplete_file_is_skipped_not_written(store: _SqliteStore) -> None:
    store.conn.execute(
        "DELETE FROM early_vote_chunk WHERE file_id = 'f-2026-10-19' AND chunk_index = 1"
    )
    result = run_roster_rows_backfill_on_store(store, "53815", dry_run=False)
    assert (result.files_written, result.files_skipped) == (1, 1)
    assert [v[0] for v in _versions(store)] == ["2026-10-20"]


def test_rejected_rows_are_quarantined_counted_and_a_rerun_is_a_no_op(
    store: _SqliteStore, caplog: pytest.LogCaptureFixture
) -> None:
    _store_day(
        store.conn,
        "2026-10-21",
        (HEADER + "0000000004,ZEBEDIAH QUINCEWORTH,EARLY,7,HARRIS\n").encode(),
    )
    with caplog.at_level("INFO"):
        result = run_roster_rows_backfill_on_store(store, "53815", dry_run=False)
    assert (result.files_written, result.rows_written, result.rejects) == (3, 3, 1)
    assert "rejects=1" in caplog.text
    assert "ZEBEDIAH" not in caplog.text
    assert "0000000004" not in caplog.text
    assert _versions(store)[-1] == ("2026-10-21", 1, "f-2026-10-21", 0, 1)
    calls = store.statement_calls
    again = run_roster_rows_backfill_on_store(store, "53815", dry_run=False)
    assert (again.files_written, again.rejects, store.statement_calls) == (0, 0, calls)


def test_a_file_whose_header_fails_the_schema_is_skipped(store: _SqliteStore) -> None:
    _store_day(store.conn, "2026-10-21", b"ID_VOTER,COUNTY\n1,HARRIS\n")
    result = run_roster_rows_backfill_on_store(store, "53815", dry_run=False)
    assert (result.files_written, result.files_skipped) == (2, 1)
    assert "2026-10-21" not in [v[0] for v in _versions(store)]
