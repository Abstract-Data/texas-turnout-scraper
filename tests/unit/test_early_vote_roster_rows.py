"""Roster rows stored per file version: batched inserts, rejects, marker last, cleanup."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest

from texas_turnout_scraper.early_vote_roster_rows import (
    ROW_SCHEMA_CREATES,
    RosterSchemaError,
    delete_statements,
    insert_statements,
    marker_statement,
    reject_statements,
)
from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    build_publish_plan,
)
from texas_turnout_scraper.roster_models import RosterReject, RosterRow, parse_roster_batches

HEADER = "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT,POLL_PLACE_NAME\n"
CSV = (
    "\ufeff"
    + HEADER
    + 'TRAVIS,"DOE, JANE",12345,MAIL-IN,101,CITY HALL\n'
    + 'BEXAR,"O""BRIEN, PAT\nJR",0000000002,IN-PERSON,102,\n'
    + "HARRIS,ÑUÑEZ MARÍA,0000000003,IN-PERSON,,LIBRARY\n"
    + "HARRIS,BAD ROW,NOPE,IN-PERSON,,LIBRARY\n"
).encode()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for sql in ROW_SCHEMA_CREATES:
        conn.execute(sql)
    return conn


def _run(conn: sqlite3.Connection, statements: tuple[tuple[str, tuple[str, ...]], ...]) -> None:
    for sql, params in statements:
        conn.execute(sql, params)


def _parsed(file_id: str = "f1") -> tuple[list[RosterRow], list[RosterReject]]:
    rows: list[RosterRow] = []
    rejects: list[RosterReject] = []
    for batch in parse_roster_batches(CSV, file_id):
        rows.extend(batch.rows)
        rejects.extend(batch.rejects)
    return rows, rejects


def _load(conn: sqlite3.Connection, file_id: str) -> None:
    rows, rejects = _parsed(file_id)
    _run(conn, insert_statements(file_id, rows, rows_per_statement=2))
    _run(conn, reject_statements(file_id, rejects, rows_per_statement=2))
    conn.execute(*marker_statement("53815", file_id, len(rows), len(rejects)))


def test_rows_round_trip_with_extra_as_json_text_and_padded_ids_as_text() -> None:
    conn = _db()
    rows, _ = _parsed()
    _run(conn, insert_statements("f1", rows, rows_per_statement=2))
    got = conn.execute(
        "SELECT row_id, county, voter_name, id_voter, voting_method, precinct, extra, "
        "typeof(id_voter), typeof(extra) FROM early_vote_roster_row ORDER BY row_id"
    ).fetchall()
    assert [r[:6] for r in got] == [
        (0, "TRAVIS", "DOE, JANE", "0000012345", "MAIL-IN", "101"),
        (1, "BEXAR", 'O"BRIEN, PAT\nJR', "0000000002", "IN-PERSON", "102"),
        (2, "HARRIS", "ÑUÑEZ MARÍA", "0000000003", "IN-PERSON", ""),
    ]
    assert [json.loads(r[6]) for r in got] == [
        {"POLL_PLACE_NAME": "CITY HALL"},
        {"POLL_PLACE_NAME": ""},
        {"POLL_PLACE_NAME": "LIBRARY"},
    ]
    assert {(r[7], r[8]) for r in got} == {("text", "text")}


def test_rejects_round_trip_with_the_full_original_row() -> None:
    conn = _db()
    _, rejects = _parsed()
    _run(conn, reject_statements("f1", rejects, rows_per_statement=2))
    got = conn.execute("SELECT row_id, reason, raw FROM early_vote_roster_reject").fetchall()
    assert [(r[0], r[1]) for r in got] == [(3, "id_voter:value_error")]
    assert json.loads(got[0][2]) == {
        "COUNTY": "HARRIS",
        "VOTER_NAME": "BAD ROW",
        "ID_VOTER": "NOPE",
        "VOTING_METHOD": "IN-PERSON",
        "PRECINCT": "",
        "POLL_PLACE_NAME": "LIBRARY",
    }


def test_the_marker_records_rows_and_rejects() -> None:
    conn = _db()
    _load(conn, "f1")
    assert conn.execute(
        "SELECT source_election_id, file_id, row_count, reject_count FROM early_vote_roster_rows"
    ).fetchall() == [("53815", "f1", 3, 1)]


def test_statements_are_batched() -> None:
    rows, rejects = _parsed()
    assert len(insert_statements("f1", rows, rows_per_statement=2)) == 2
    assert len(insert_statements("f1", rows, rows_per_statement=3)) == 1
    assert insert_statements("f1", []) == ()
    assert reject_statements("f1", []) == ()
    assert len(reject_statements("f1", rejects)) == 1


def test_rerunning_the_inserts_leaves_one_copy() -> None:
    conn = _db()
    _load(conn, "f1")
    _load(conn, "f1")
    assert conn.execute("SELECT COUNT(*) FROM early_vote_roster_row").fetchone() == (3,)
    assert conn.execute("SELECT COUNT(*) FROM early_vote_roster_reject").fetchone() == (1,)


def test_delete_removes_one_versions_rows_rejects_and_marker_only() -> None:
    conn = _db()
    for file_id in ("old", "keep"):
        _load(conn, file_id)
    _run(conn, delete_statements("53815", "old", 4, rows_per_statement=2))
    for table in ("early_vote_roster_row", "early_vote_roster_reject", "early_vote_roster_rows"):
        assert conn.execute(f"SELECT DISTINCT file_id FROM {table}").fetchall() == [("keep",)]


def test_delete_goes_marker_then_rejects_then_bounded_row_ranges() -> None:
    statements = delete_statements("53815", "old", 120_000, rows_per_statement=50_000)
    assert statements[0][0].startswith("DELETE FROM early_vote_roster_rows ")
    assert statements[1][0].startswith("DELETE FROM early_vote_roster_reject ")
    assert all(s[0].startswith("DELETE FROM early_vote_roster_row ") for s in statements[2:])
    assert len(statements) == 2 + 3
    assert len(delete_statements("53815", "old", 0)) == 2


HEADER_ONLY_NEW = (
    HEADER + "TRAVIS,A B,1,MAIL-IN,1,SITE\nBEXAR,C D,2,IN-PERSON,2,\nBEXAR,E F,BAD,IN-PERSON,2,\n"
).encode()


def _store(monkeypatch: pytest.MonkeyPatch, *, old_rows: int | None = 5):
    store = CloudflareEarlyVoteStore(
        account_id="a",
        api_token="t",
        kv_namespace_id="n",
        client=httpx.Client(),
        snapshots=None,
    )
    statements: list[tuple[str, list[str]]] = []

    def query_rows(self: Any, database_id: str, sql: str, params: list[str]) -> list[dict]:
        if "FROM early_vote_pointer" in sql:
            return []
        if "FROM early_vote_day" in sql:
            return [{"file_id": "old-file", "sha256": "oldsha"}]
        if "MAX(row_id)" in sql:
            return [] if old_rows is None else [{"max_row": old_rows - 1}]
        return []

    monkeypatch.setattr(CloudflareEarlyVoteStore, "_query_rows", query_rows)
    monkeypatch.setattr(
        CloudflareEarlyVoteStore, "_statements", lambda self, db, stmts: statements.extend(stmts)
    )
    monkeypatch.setattr(CloudflareEarlyVoteStore, "_insert_chunks", lambda self, db, ins: None)
    monkeypatch.setattr(
        CloudflareEarlyVoteStore, "_write_precomputed_aggregates", lambda self, *a, **k: None
    )
    monkeypatch.setattr(
        CloudflareEarlyVoteStore, "_load_file_bytes", lambda self, *a: HEADER_ONLY_NEW
    )
    return store, statements


def _write(store: CloudflareEarlyVoteStore, decoded: bytes = HEADER_ONLY_NEW) -> None:
    plan = build_publish_plan("53815", decoded, "newsha", "new-file")
    store._write_day_scoped("db", "53815", "2026-10-19", plan, "newsha", decoded)


def _index(statements: list[tuple[str, list[str]]], prefix: str) -> int:
    return next(i for i, s in enumerate(statements) if s[0].startswith(prefix))


def test_a_day_write_inserts_rows_then_marker_then_the_day_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, statements = _store(monkeypatch)
    _write(store)
    rows = _index(statements, "INSERT OR REPLACE INTO early_vote_roster_row ")
    marker = _index(statements, "INSERT INTO early_vote_roster_rows ")
    day = _index(statements, "INSERT INTO early_vote_day ")
    assert rows < marker < day
    assert statements[marker][1] == ["53815", "new-file", "2", "1"]


def test_the_replaced_versions_rows_are_deleted_after_the_pointer_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, statements = _store(monkeypatch, old_rows=5)
    _write(store)
    day = _index(statements, "INSERT INTO early_vote_day ")
    cleanup = [
        i
        for i, s in enumerate(statements)
        if s[0].startswith("DELETE FROM early_vote_roster_") and "old-file" in s[1]
    ]
    assert len(cleanup) == 3
    assert min(cleanup) > day


def test_a_replaced_version_with_no_rows_still_loses_its_marker_and_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, statements = _store(monkeypatch, old_rows=None)
    _write(store)
    deletes = [
        s[0].split(" WHERE")[0]
        for s in statements
        if s[0].startswith("DELETE") and "old-file" in s[1]
    ]
    assert "DELETE FROM early_vote_roster_rows" in deletes
    assert "DELETE FROM early_vote_roster_reject" in deletes
    assert "DELETE FROM early_vote_roster_row" not in deletes


def test_a_bad_header_stops_before_any_chunk_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    store, statements = _store(monkeypatch)
    chunks: list[object] = []
    monkeypatch.setattr(
        CloudflareEarlyVoteStore, "_insert_chunks", lambda self, db, ins: chunks.append(ins)
    )
    with pytest.raises(RosterSchemaError):
        _write(store, b"COUNTY,VOTER_NAME,ID_VOTER\nTRAVIS,A B,1\n")
    assert chunks == []
    assert statements == []


def test_turnout_json_payloads_store_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    store, statements = _store(monkeypatch)
    _write(store, b'{"turnout_by_county":[]}')
    assert not any("INTO early_vote_roster_row" in s[0] for s in statements)


def test_logs_carry_counts_never_names(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, _ = _store(monkeypatch)
    with caplog.at_level("INFO"):
        _write(store)
    text = caplog.text
    assert "rows=2 rejects=1" in text
    for name in ("A B", "C D", "E F", "BAD"):
        assert name not in text


def test_a_replaced_versions_rows_go_before_its_chunks_so_a_crash_leaves_it_findable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, statements = _store(monkeypatch, old_rows=5)
    _write(store)
    rows_gone = next(
        i
        for i, s in enumerate(statements)
        if s[0].startswith("DELETE FROM early_vote_roster_") and "old-file" in s[1]
    )
    chunks_gone = _index(statements, "DELETE FROM early_vote_chunk")
    assert rows_gone < chunks_gone


def _sqlite_store(conn: sqlite3.Connection) -> CloudflareEarlyVoteStore:
    store = CloudflareEarlyVoteStore(
        account_id="a", api_token="t", kv_namespace_id="n", client=httpx.Client(), snapshots=None
    )

    def query_rows(database_id: str, sql: str, params: list[str]) -> list[dict]:
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def statements(database_id: str, stmts: list[tuple[str, list[str]]]) -> None:
        for sql, params in stmts:
            conn.execute(sql, params)

    store._query_rows = query_rows  # type: ignore[method-assign]
    store._statements = statements  # type: ignore[method-assign]
    store.stored_hash = lambda election_id, **kw: None  # type: ignore[method-assign]
    return store


def _stage_version(conn: sqlite3.Connection, file_id: str, *, chunks: bool, marker: bool) -> None:
    if chunks:
        conn.execute(
            "INSERT INTO early_vote_chunk VALUES ('53815', ?, 0, 'QQ==')",
            (file_id,),
        )
        conn.execute(
            "INSERT INTO early_vote_file VALUES ('53815', ?, 'sha', 1, 1)",
            (file_id,),
        )
    rows, rejects = _parsed(file_id)
    _run(conn, insert_statements(file_id, rows, rows_per_statement=2))
    _run(conn, reject_statements(file_id, rejects))
    if marker:
        conn.execute(*marker_statement("53815", file_id, len(rows), len(rejects)))


def test_stale_cleanup_removes_unreferenced_rows_and_markers_but_keeps_live_ones() -> None:
    from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES

    conn = sqlite3.connect(":memory:")
    for sql in _SCHEMA_CREATES:
        conn.execute(sql)
    _stage_version(conn, "live", chunks=True, marker=True)
    conn.execute("INSERT INTO early_vote_day VALUES ('53815', '2026-10-19', 'sha', 'live')")
    _stage_version(conn, "failed-midway", chunks=True, marker=False)
    _stage_version(conn, "moved-on", chunks=True, marker=True)
    _stage_version(conn, "marker-only", chunks=False, marker=True)
    store = _sqlite_store(conn)
    store._delete_stale_files("db", "53815")
    left = conn.execute("SELECT DISTINCT file_id FROM early_vote_roster_row").fetchall()
    assert left == [("live",)]
    assert conn.execute("SELECT file_id FROM early_vote_roster_rows").fetchall() == [("live",)]
    assert conn.execute("SELECT DISTINCT file_id FROM early_vote_roster_reject").fetchall() == [
        ("live",)
    ]
    assert conn.execute("SELECT file_id FROM early_vote_chunk").fetchall() == [("live",)]
