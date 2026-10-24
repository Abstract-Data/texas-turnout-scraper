"""Roster rows stored per file version: one row iterator, batched inserts, marker last, cleanup."""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx
import pytest

from texas_turnout_scraper.early_vote_roster_rows import (
    ROW_SCHEMA_CREATES,
    RosterRow,
    delete_statements,
    insert_statements,
    iter_roster_rows,
    marker_statement,
)
from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    build_publish_plan,
)

HEADER = "COUNTY,VOTER_NAME,ID_VOTER,VOTING_METHOD,PRECINCT\n"
CSV = (
    "﻿"
    + HEADER
    + 'TRAVIS,"DOE, JANE",12345,MAIL-IN,101\n'
    + 'BEXAR,"O""BRIEN, PAT\nJR",0000000002,IN-PERSON,102\n'
    + "HARRIS,ÑUÑEZ MARÍA,0000000003,IN-PERSON,\n"
).encode()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for sql in ROW_SCHEMA_CREATES:
        conn.execute(sql)
    return conn


def _run(conn: sqlite3.Connection, statements: tuple[tuple[str, tuple[str, ...]], ...]) -> None:
    for sql, params in statements:
        conn.execute(sql, params)


def test_rows_follow_the_file_with_quoting_and_padded_ids() -> None:
    rows = list(iter_roster_rows(CSV))
    assert rows == [
        RosterRow("TRAVIS", "DOE, JANE", "0000012345", "MAIL-IN", "101"),
        RosterRow("BEXAR", 'O"BRIEN, PAT\nJR', "0000000002", "IN-PERSON", "102"),
        RosterRow("HARRIS", "ÑUÑEZ MARÍA", "0000000003", "IN-PERSON", ""),
    ]


def test_missing_columns_are_empty_strings() -> None:
    rows = list(iter_roster_rows(b"ID_VOTER,COUNTY\n7,TRAVIS\n"))
    assert rows == [RosterRow("TRAVIS", "", "0000000007", "", "")]


def test_a_header_only_file_has_no_rows() -> None:
    assert list(iter_roster_rows(HEADER.encode())) == []


def test_inserted_rows_round_trip_exactly_in_file_order() -> None:
    conn = _db()
    _run(conn, insert_statements("f1", CSV, rows_per_statement=2))
    got = conn.execute(
        "SELECT row_id, county, voter_name, id_voter, voting_method, precinct, typeof(id_voter) "
        "FROM early_vote_roster_row WHERE file_id = 'f1' ORDER BY row_id"
    ).fetchall()
    assert [r[0] for r in got] == [0, 1, 2]
    assert [tuple(r[1:6]) for r in got] == [tuple(r) for r in iter_roster_rows(CSV)]
    assert {r[6] for r in got} == {"text"}


def test_statements_are_batched() -> None:
    assert len(insert_statements("f1", CSV, rows_per_statement=2)) == 2
    assert len(insert_statements("f1", CSV, rows_per_statement=3)) == 1
    assert insert_statements("f1", HEADER.encode()) == ()


def test_rerunning_the_inserts_leaves_one_copy() -> None:
    conn = _db()
    statements = insert_statements("f1", CSV, rows_per_statement=2)
    _run(conn, statements)
    _run(conn, statements)
    assert conn.execute("SELECT COUNT(*) FROM early_vote_roster_row").fetchone() == (3,)


def test_delete_removes_one_versions_rows_and_marker_only() -> None:
    conn = _db()
    for file_id in ("old", "keep"):
        _run(conn, insert_statements(file_id, CSV, rows_per_statement=2))
        conn.execute(*marker_statement("53815", file_id, 3))
    _run(conn, delete_statements("53815", "old", 3, rows_per_statement=2))
    left = conn.execute("SELECT file_id, COUNT(*) FROM early_vote_roster_row GROUP BY 1").fetchall()
    assert left == [("keep", 3)]
    assert conn.execute("SELECT file_id FROM early_vote_roster_rows").fetchall() == [("keep",)]


def test_marker_deletes_before_rows_so_a_reader_never_sees_half_a_version() -> None:
    statements = delete_statements("53815", "old", 120_000, rows_per_statement=50_000)
    assert statements[0][0].startswith("DELETE FROM early_vote_roster_rows ")
    assert len(statements) == 1 + 3


HEADER_ONLY_NEW = (HEADER + "TRAVIS,A B,1,MAIL-IN,1\nBEXAR,C D,2,IN-PERSON,2\n").encode()


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
    assert statements[marker][1] == ["53815", "new-file", "2"]


def test_the_replaced_versions_rows_are_deleted_after_the_pointer_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, statements = _store(monkeypatch, old_rows=5)
    _write(store)
    day = _index(statements, "INSERT INTO early_vote_day ")
    cleanup = [
        i
        for i, s in enumerate(statements)
        if s[0].startswith("DELETE FROM early_vote_roster_row") and "old-file" in s[1]
    ]
    assert cleanup
    assert min(cleanup) > day


def test_a_replaced_version_with_no_rows_needs_no_row_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, statements = _store(monkeypatch, old_rows=None)
    _write(store)
    assert not any(
        s[0].startswith("DELETE FROM early_vote_roster_row") and "old-file" in s[1]
        for s in statements
    )


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
    assert "A B" not in text
    assert "C D" not in text
