"""Roster rows in D1, keyed by file version so a reader never sees a half-written day.

A file's rows and rejects are inserted under its ``file_id`` first. The marker row is written
last, and the day pointer moves only after that, so a version is invisible until it is complete.

This module is imported by ``early_vote_sync`` and the Worker, so it must not import sqlmodel.
The DDL below is generated from ``roster_models`` and pinned by a drift test. The statement
builders take any objects with the model attributes. Rows contain voter names and VUIDs:
nothing here logs or raises with row content.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping
from typing import Protocol

from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError

Statement = tuple[str, tuple[str, ...]]

ROWS_PER_STATEMENT = 4000
DELETE_ROWS_PER_STATEMENT = 50_000
VUID_WIDTH = 10
VOTING_METHODS = frozenset({"IN-PERSON", "MAIL-IN"})

# Header text (stripped, upper-cased) to model field. PRECINCT is the only optional one.
KNOWN_COLUMNS: Mapping[str, str] = {
    "COUNTY": "county",
    "VOTER_NAME": "voter_name",
    "ID_VOTER": "id_voter",
    "VOTING_METHOD": "voting_method",
    "PRECINCT": "precinct",
}
REQUIRED_COLUMNS = frozenset({"COUNTY", "VOTER_NAME", "ID_VOTER", "VOTING_METHOD"})

ROW_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_roster_row ("
    "county VARCHAR NOT NULL, voter_name VARCHAR NOT NULL, id_voter VARCHAR NOT NULL, "
    "voting_method VARCHAR NOT NULL, precinct VARCHAR NOT NULL, file_id VARCHAR NOT NULL, "
    "row_id INTEGER NOT NULL, extra JSON NOT NULL, PRIMARY KEY (file_id, row_id)) WITHOUT ROWID"
)
REJECT_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_roster_reject ("
    "file_id VARCHAR NOT NULL, row_id INTEGER NOT NULL, reason VARCHAR NOT NULL, "
    "raw JSON NOT NULL, PRIMARY KEY (file_id, row_id))"
)
MARKER_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_roster_rows ("
    "source_election_id VARCHAR NOT NULL, file_id VARCHAR NOT NULL, row_count INTEGER NOT NULL, "
    "reject_count INTEGER NOT NULL, PRIMARY KEY (source_election_id, file_id))"
)
ROW_SCHEMA_CREATES: tuple[str, ...] = (ROW_TABLE_SQL, REJECT_TABLE_SQL, MARKER_TABLE_SQL)

_INSERT_ROWS_SQL = (
    "INSERT OR REPLACE INTO early_vote_roster_row "
    "(file_id, row_id, county, voter_name, id_voter, voting_method, precinct, extra) "
    "SELECT ?, CAST(json_extract(value, '$[0]') AS INTEGER), json_extract(value, '$[1]'), "
    "json_extract(value, '$[2]'), json_extract(value, '$[3]'), json_extract(value, '$[4]'), "
    "json_extract(value, '$[5]'), json_extract(value, '$[6]') FROM json_each(?)"
)
_INSERT_REJECTS_SQL = (
    "INSERT OR REPLACE INTO early_vote_roster_reject (file_id, row_id, reason, raw) "
    "SELECT ?, CAST(json_extract(value, '$[0]') AS INTEGER), json_extract(value, '$[1]'), "
    "json_extract(value, '$[2]') FROM json_each(?)"
)
_MARKER_UPSERT_SQL = (
    "INSERT INTO early_vote_roster_rows (source_election_id, file_id, row_count, reject_count) "
    "VALUES (?, ?, ?, ?) ON CONFLICT(source_election_id, file_id) DO UPDATE SET "
    "row_count = excluded.row_count, reject_count = excluded.reject_count"
)
_DELETE_MARKER_SQL = (
    "DELETE FROM early_vote_roster_rows WHERE source_election_id = ? AND file_id = ?"
)
_DELETE_REJECTS_SQL = "DELETE FROM early_vote_roster_reject WHERE file_id = ?"
_DELETE_ROWS_SQL = (
    "DELETE FROM early_vote_roster_row WHERE file_id = ? AND row_id >= ? AND row_id < ?"
)
MARKED_FILES_SQL = "SELECT file_id FROM early_vote_roster_rows WHERE source_election_id = ?"
MAX_ROW_SQL = "SELECT MAX(row_id) AS max_row FROM early_vote_roster_row WHERE file_id = ?"


class RosterSchemaError(EarlyVoteSyncError):
    """The day file's header cannot be mapped to roster rows. Nothing was written."""


class RowLike(Protocol):
    row_id: int
    county: str
    voter_name: str
    id_voter: str
    voting_method: str
    precinct: str
    extra: dict[str, str]


class RejectLike(Protocol):
    row_id: int
    reason: str
    raw: dict[str, object]


def header_fields(decoded: bytes) -> tuple[list[str], dict[int, str]]:
    """The header as written, and the column position of each known field.

    Raises ``RosterSchemaError`` (header names only) when a required column is missing or two
    headers are the same after strip and upper-case.
    """
    header: list[str] = next(csv.reader(io.StringIO(decoded.decode("utf-8-sig"))), [])
    normalized = [name.strip().upper() for name in header]
    duplicates = sorted({name for name in normalized if normalized.count(name) > 1})
    if duplicates:
        raise RosterSchemaError(f"roster header repeats columns: {','.join(duplicates)}")
    missing = sorted(REQUIRED_COLUMNS - set(normalized))
    if missing:
        raise RosterSchemaError(f"roster header is missing columns: {','.join(missing)}")
    fields = {
        position: KNOWN_COLUMNS[name]
        for position, name in enumerate(normalized)
        if name in KNOWN_COLUMNS
    }
    return header, fields


def check_roster_header(decoded: bytes) -> None:
    """Hard stop before any write when the header cannot be loaded."""
    header_fields(decoded)


def _dump(batch: list[list[object]]) -> str:
    return json.dumps(batch, ensure_ascii=False, separators=(",", ":"))


def _batched(
    sql: str, file_id: str, payloads: list[list[object]], per_statement: int
) -> tuple[Statement, ...]:
    return tuple(
        (sql, (file_id, _dump(payloads[start : start + per_statement])))
        for start in range(0, len(payloads), per_statement)
    )


def insert_statements(
    file_id: str, rows: Iterable[RowLike], *, rows_per_statement: int = ROWS_PER_STATEMENT
) -> tuple[Statement, ...]:
    """Idempotent batched row inserts. ``extra`` travels as a nested JSON object."""
    payloads: list[list[object]] = [
        [r.row_id, r.county, r.voter_name, r.id_voter, r.voting_method, r.precinct, r.extra]
        for r in rows
    ]
    return _batched(_INSERT_ROWS_SQL, file_id, payloads, rows_per_statement)


def reject_statements(
    file_id: str, rejects: Iterable[RejectLike], *, rows_per_statement: int = ROWS_PER_STATEMENT
) -> tuple[Statement, ...]:
    payloads: list[list[object]] = [[r.row_id, r.reason, r.raw] for r in rejects]
    return _batched(_INSERT_REJECTS_SQL, file_id, payloads, rows_per_statement)


def marker_statement(
    election_id: str, file_id: str, row_count: int, reject_count: int
) -> Statement:
    """Written after every row and reject insert; readers serve a version only once this exists."""
    return (_MARKER_UPSERT_SQL, (election_id, file_id, str(row_count), str(reject_count)))


def delete_statements(
    election_id: str,
    file_id: str,
    row_count: int,
    *,
    rows_per_statement: int = DELETE_ROWS_PER_STATEMENT,
) -> tuple[Statement, ...]:
    """Marker first, so the version disappears from readers, then rejects, then row ranges."""
    ranges = tuple(
        (_DELETE_ROWS_SQL, (file_id, str(start), str(start + rows_per_statement)))
        for start in range(0, row_count, rows_per_statement)
    )
    return (
        (_DELETE_MARKER_SQL, (election_id, file_id)),
        (_DELETE_REJECTS_SQL, (file_id,)),
        *ranges,
    )
