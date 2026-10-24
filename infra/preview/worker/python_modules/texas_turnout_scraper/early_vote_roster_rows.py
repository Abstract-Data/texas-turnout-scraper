"""Roster rows in D1, keyed by file version so a reader never sees a half-written day.

A file's rows are inserted under its ``file_id`` first. The marker row is written last, and the
day pointer moves only after that, so rows of a version are invisible until they are complete.
Rows contain voter names and VUIDs: nothing here logs or raises with row content.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from typing import NamedTuple

Statement = tuple[str, tuple[str, ...]]

ROWS_PER_STATEMENT = 4000
DELETE_ROWS_PER_STATEMENT = 50_000
VUID_WIDTH = 10

ROW_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_roster_row ("
    "file_id TEXT NOT NULL, row_id INTEGER NOT NULL, county TEXT NOT NULL, "
    "voter_name TEXT NOT NULL, id_voter TEXT NOT NULL, voting_method TEXT NOT NULL, "
    "precinct TEXT NOT NULL, PRIMARY KEY (file_id, row_id)) WITHOUT ROWID"
)
MARKER_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_roster_rows ("
    "source_election_id TEXT NOT NULL, file_id TEXT NOT NULL, row_count INTEGER NOT NULL, "
    "PRIMARY KEY (source_election_id, file_id))"
)
ROW_SCHEMA_CREATES: tuple[str, ...] = (ROW_TABLE_SQL, MARKER_TABLE_SQL)

_INSERT_ROWS_SQL = (
    "INSERT OR REPLACE INTO early_vote_roster_row "
    "(file_id, row_id, county, voter_name, id_voter, voting_method, precinct) "
    "SELECT ?, CAST(json_extract(value, '$[0]') AS INTEGER), json_extract(value, '$[1]'), "
    "json_extract(value, '$[2]'), json_extract(value, '$[3]'), json_extract(value, '$[4]'), "
    "json_extract(value, '$[5]') FROM json_each(?)"
)
_MARKER_UPSERT_SQL = (
    "INSERT INTO early_vote_roster_rows (source_election_id, file_id, row_count) "
    "VALUES (?, ?, ?) ON CONFLICT(source_election_id, file_id) DO UPDATE SET "
    "row_count = excluded.row_count"
)
_DELETE_MARKER_SQL = (
    "DELETE FROM early_vote_roster_rows WHERE source_election_id = ? AND file_id = ?"
)
_DELETE_ROWS_SQL = (
    "DELETE FROM early_vote_roster_row WHERE file_id = ? AND row_id >= ? AND row_id < ?"
)
MARKED_FILES_SQL = "SELECT file_id FROM early_vote_roster_rows WHERE source_election_id = ?"
MAX_ROW_SQL = "SELECT MAX(row_id) AS max_row FROM early_vote_roster_row WHERE file_id = ?"


class RosterRow(NamedTuple):
    county: str
    voter_name: str
    id_voter: str
    voting_method: str
    precinct: str


def _vuid(raw: str) -> str:
    text = raw.strip()
    return text.zfill(VUID_WIDTH) if text.isdigit() else text


def iter_roster_rows(decoded: bytes) -> Iterator[RosterRow]:
    """Every data row of a roster CSV in file order, VUIDs zero-padded, other fields verbatim."""
    reader = csv.reader(io.StringIO(decoded.decode("utf-8-sig")))
    header = next(reader, None)
    if header is None:
        return
    index = {name.strip().upper(): position for position, name in enumerate(header)}

    def field(row: list[str], name: str) -> str:
        position = index.get(name)
        return row[position] if position is not None and position < len(row) else ""

    for row in reader:
        if not row:
            continue
        yield RosterRow(
            county=field(row, "COUNTY"),
            voter_name=field(row, "VOTER_NAME"),
            id_voter=_vuid(field(row, "ID_VOTER")),
            voting_method=field(row, "VOTING_METHOD"),
            precinct=field(row, "PRECINCT"),
        )


def count_roster_rows(decoded: bytes) -> int:
    return sum(1 for _ in iter_roster_rows(decoded))


def insert_statements(
    file_id: str, decoded: bytes, *, rows_per_statement: int = ROWS_PER_STATEMENT
) -> tuple[Statement, ...]:
    """Idempotent batched inserts for one file's rows. ``row_id`` is the line order in the file."""
    statements: list[Statement] = []
    batch: list[list[object]] = []
    for row_id, row in enumerate(iter_roster_rows(decoded)):
        batch.append([row_id, *row])
        if len(batch) == rows_per_statement:
            statements.append((_INSERT_ROWS_SQL, (file_id, _dump(batch))))
            batch = []
    if batch:
        statements.append((_INSERT_ROWS_SQL, (file_id, _dump(batch))))
    return tuple(statements)


def _dump(batch: list[list[object]]) -> str:
    return json.dumps(batch, ensure_ascii=False, separators=(",", ":"))


def marker_statement(election_id: str, file_id: str, row_count: int) -> Statement:
    """Written after every row insert; readers serve a file's rows only once this exists."""
    return (_MARKER_UPSERT_SQL, (election_id, file_id, str(row_count)))


def delete_statements(
    election_id: str,
    file_id: str,
    row_count: int,
    *,
    rows_per_statement: int = DELETE_ROWS_PER_STATEMENT,
) -> tuple[Statement, ...]:
    """Marker first, so the version disappears from readers, then its rows in bounded ranges."""
    ranges = tuple(
        (_DELETE_ROWS_SQL, (file_id, str(start), str(start + rows_per_statement)))
        for start in range(0, row_count, rows_per_statement)
    )
    return ((_DELETE_MARKER_SQL, (election_id, file_id)), *ranges)
