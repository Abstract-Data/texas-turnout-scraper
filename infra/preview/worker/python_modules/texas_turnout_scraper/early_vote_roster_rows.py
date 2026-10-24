"""Roster rows in D1: keyed by content within a day, each live over a range of day versions.

A row's key is ``(day, row_hash, occ)``: ``day`` is the date's ordinal, ``row_hash`` a signed
64-bit BLAKE2b of the file header and the row's cells, and ``occ`` counts identical lines. A row
was added in version ``added_in`` and, once gone from the file, removed in ``removed_in``. The
day's current version lives in one ``early_vote_roster_version`` row, written after every row
change, so a reader pinned to a version sees one whole file (``VISIBLE_AT_SQL``).

This module is imported by ``early_vote_sync`` and the Worker, so it must not import sqlmodel.
The DDL below is generated from ``roster_models`` and pinned by a drift test. Rows contain voter
names and VUIDs: nothing here logs or raises with row content.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError

Statement = tuple[str, tuple[str, ...]]
RowKey = tuple[int, int]

ROWS_PER_STATEMENT = 4000
KEYS_PER_STATEMENT = 10_000
# Range statements (recovery, retire, collection) each cover 1/KEY_RANGES of the hash space,
# about 50k rows for an 800k-row day.
KEY_RANGES = 16
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

ROW_TABLE = "early_vote_roster_row"
REJECT_TABLE = "early_vote_roster_reject"
ROW_TABLES = (ROW_TABLE, REJECT_TABLE)

ROW_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_roster_row ("
    "county VARCHAR NOT NULL, voter_name VARCHAR NOT NULL, id_voter VARCHAR NOT NULL, "
    "voting_method VARCHAR NOT NULL, precinct VARCHAR NOT NULL, day INTEGER NOT NULL, "
    "row_hash INTEGER NOT NULL, occ INTEGER NOT NULL, extra JSON NOT NULL, "
    "added_in INTEGER NOT NULL, removed_in INTEGER, PRIMARY KEY (day, row_hash, occ)) "
    "WITHOUT ROWID"
)
REJECT_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_roster_reject ("
    "day INTEGER NOT NULL, row_hash INTEGER NOT NULL, occ INTEGER NOT NULL, "
    "added_in INTEGER NOT NULL, removed_in INTEGER, reason VARCHAR NOT NULL, "
    "raw JSON NOT NULL, PRIMARY KEY (day, row_hash, occ)) WITHOUT ROWID"
)
VERSION_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_roster_version ("
    "source_election_id VARCHAR NOT NULL, ev_date VARCHAR NOT NULL, version INTEGER NOT NULL, "
    "file_id VARCHAR NOT NULL, sha256 VARCHAR NOT NULL, row_count INTEGER NOT NULL, "
    "reject_count INTEGER NOT NULL, PRIMARY KEY (source_election_id, ev_date))"
)
ROW_SCHEMA_CREATES: tuple[str, ...] = (ROW_TABLE_SQL, REJECT_TABLE_SQL, VERSION_TABLE_SQL)

# The file_id-keyed tables of the first loader. Their content is rebuilt from the chunks.
LEGACY_ROW_COLUMNS_SQL = "SELECT name FROM pragma_table_info('early_vote_roster_row')"
LEGACY_DRAIN_TABLES: tuple[str, ...] = ("early_vote_roster_row", "early_vote_roster_reject")


def legacy_remaining_sql(table: str) -> str:
    return f"SELECT 1 AS n FROM {table} LIMIT 1"


def legacy_drain_sql(table: str, batch: int) -> str:
    """Delete one bounded slice of a file-keyed table; D1 limits how long one statement may run."""
    return (
        f"DELETE FROM {table} WHERE (file_id, row_id) IN "
        f"(SELECT file_id, row_id FROM {table} LIMIT {int(batch)})"
    )


LEGACY_ROW_DROPS: tuple[str, ...] = (
    "DROP TABLE IF EXISTS early_vote_roster_rows",
    "DROP TABLE IF EXISTS early_vote_roster_reject",
    "DROP TABLE IF EXISTS early_vote_roster_row",
)

# Rows of a day as of version V. Parameters: V, V.
VISIBLE_AT_SQL = "added_in <= ? AND (removed_in IS NULL OR removed_in > ?)"
# One page of a day's rows at version V in key order. Parameters: day, V, V, the last
# (row_hash, occ) already served (start at (-2**63, -1)), page size.
VISIBLE_PAGE_SQL = (
    "SELECT row_hash, occ, county, voter_name, id_voter, voting_method, precinct, extra "
    f"FROM early_vote_roster_row WHERE day = ? AND {VISIBLE_AT_SQL} "
    "AND (row_hash, occ) > (?, ?) ORDER BY row_hash, occ LIMIT ?"
)

VERSION_SELECT_SQL = (
    "SELECT version, file_id, sha256, row_count, reject_count FROM early_vote_roster_version "
    "WHERE source_election_id = ? AND ev_date = ?"
)
VERSIONS_SQL = "SELECT ev_date, file_id FROM early_vote_roster_version WHERE source_election_id = ?"
_VERSION_UPSERT_SQL = (
    "INSERT INTO early_vote_roster_version "
    "(source_election_id, ev_date, version, file_id, sha256, row_count, reject_count) "
    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source_election_id, ev_date) DO UPDATE SET "
    "version = excluded.version, file_id = excluded.file_id, sha256 = excluded.sha256, "
    "row_count = excluded.row_count, reject_count = excluded.reject_count"
)
COUNT_VISIBLE_SQL = (
    f"SELECT (SELECT COUNT(*) FROM early_vote_roster_row WHERE day = ? AND {VISIBLE_AT_SQL}) "
    "AS row_count, "
    f"(SELECT COUNT(*) FROM early_vote_roster_reject WHERE day = ? AND {VISIBLE_AT_SQL}) "
    "AS reject_count"
)

# A key added again while its old row still exists (removed, not yet collected) takes the new
# added_in, so readers of the current version keep not seeing it. A key that is still live keeps
# its added_in, so they keep seeing it. The WHERE true is SQLite's rule for INSERT ... SELECT
# with an upsert clause.
_REVIVE = (
    "ON CONFLICT(day, row_hash, occ) DO UPDATE SET added_in = CASE WHEN removed_in IS NULL "
    "OR removed_in >= excluded.added_in THEN added_in ELSE excluded.added_in END, "
    "removed_in = NULL"
)
_INSERT_ROWS_SQL = (
    "INSERT INTO early_vote_roster_row (day, added_in, row_hash, occ, county, voter_name, "
    "id_voter, voting_method, precinct, extra) SELECT ?, ?, "
    "CAST(json_extract(value, '$[0]') AS INTEGER), CAST(json_extract(value, '$[1]') AS INTEGER), "
    "json_extract(value, '$[2]'), json_extract(value, '$[3]'), json_extract(value, '$[4]'), "
    "json_extract(value, '$[5]'), json_extract(value, '$[6]'), json_extract(value, '$[7]') "
    f"FROM json_each(?) WHERE true {_REVIVE}"
)
_INSERT_REJECTS_SQL = (
    "INSERT INTO early_vote_roster_reject (day, added_in, row_hash, occ, reason, raw) "
    "SELECT ?, ?, CAST(json_extract(value, '$[0]') AS INTEGER), "
    "CAST(json_extract(value, '$[1]') AS INTEGER), json_extract(value, '$[2]'), "
    f"json_extract(value, '$[3]') FROM json_each(?) WHERE true {_REVIVE}"
)
_REMOVE_SQL = (
    "UPDATE {table} SET removed_in = ? WHERE day = ? AND removed_in IS NULL AND (row_hash, occ) "
    "IN (SELECT CAST(json_extract(value, '$[0]') AS INTEGER), "
    "CAST(json_extract(value, '$[1]') AS INTEGER) FROM json_each(?))"
)
_RANGE = "day = ? AND row_hash BETWEEN ? AND ?"
_RECOVER_ADDED_SQL = f"DELETE FROM {{table}} WHERE {_RANGE} AND added_in > ?"
_RECOVER_REMOVED_SQL = f"UPDATE {{table}} SET removed_in = NULL WHERE {_RANGE} AND removed_in > ?"
_RETIRE_SQL = (
    f"UPDATE {{table}} SET removed_in = ? WHERE {_RANGE} AND added_in <= ? AND removed_in IS NULL"
)
_GC_SQL = f"DELETE FROM {{table}} WHERE {_RANGE} AND removed_in IS NOT NULL AND removed_in <= ?"


class RosterSchemaError(EarlyVoteSyncError):
    """The day file's header cannot be mapped to roster rows. Nothing was written."""


class RowLike(Protocol):
    row_hash: int
    occ: int
    county: str
    voter_name: str
    id_voter: str
    voting_method: str
    precinct: str
    extra: dict[str, str]


class RejectLike(Protocol):
    row_hash: int
    occ: int
    reason: str
    raw: dict[str, object]


@dataclass(frozen=True)
class DayVersion:
    """The marker row of one day: which version readers serve and the file it came from."""

    version: int
    file_id: str
    sha256: str
    row_count: int
    reject_count: int


def day_number(ev_date: str) -> int:
    return date.fromisoformat(ev_date).toordinal()


def visible_params(version: int) -> tuple[str, str]:
    return (str(version), str(version))


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


def _feed(hasher: hashlib.blake2b, cells: Sequence[str]) -> None:
    for cell in cells:
        data = cell.encode("utf-8")
        hasher.update(len(data).to_bytes(4, "big"))
        hasher.update(data)


def keyed_lines(decoded: bytes) -> Iterator[tuple[RowKey, list[str]]]:
    """Each non-blank data line with its ``(row_hash, occ)``, in file order.

    The hash covers the header's cells too, so a header change gives every line a new key.
    """
    reader = csv.reader(io.StringIO(decoded.decode("utf-8-sig")))
    header = next(reader, [])
    seed = hashlib.blake2b(digest_size=8)
    seed.update(len(header).to_bytes(4, "big"))
    _feed(seed, header)
    seen: dict[int, int] = {}
    for cells in reader:
        if not cells:
            continue
        hasher = seed.copy()
        _feed(hasher, cells)
        row_hash = int.from_bytes(hasher.digest(), "big", signed=True)
        occ = seen.get(row_hash, 0)
        seen[row_hash] = occ + 1
        yield (row_hash, occ), cells


def key_ranges(count: int = KEY_RANGES) -> tuple[tuple[str, str], ...]:
    """Inclusive ``row_hash`` bounds that split the signed 64-bit space into ``count`` parts."""
    step = 2**64 // count
    lows = [-(2**63) + index * step for index in range(count)]
    highs = [low - 1 for low in lows[1:]] + [2**63 - 1]
    return tuple((str(low), str(high)) for low, high in zip(lows, highs, strict=True))


def _dump(batch: Sequence[object]) -> str:
    return json.dumps(batch, ensure_ascii=False, separators=(",", ":"))


def _chunks(items: Sequence[object], size: int) -> Iterator[Sequence[object]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def insert_statements(
    day: int, added_in: int, rows: Iterable[RowLike], *, per_statement: int = ROWS_PER_STATEMENT
) -> tuple[Statement, ...]:
    """Batched row inserts at ``added_in``. ``extra`` travels as a nested JSON object."""
    payloads = [
        [
            r.row_hash,
            r.occ,
            r.county,
            r.voter_name,
            r.id_voter,
            r.voting_method,
            r.precinct,
            r.extra,
        ]
        for r in rows
    ]
    return tuple(
        (_INSERT_ROWS_SQL, (str(day), str(added_in), _dump(chunk)))
        for chunk in _chunks(payloads, per_statement)
    )


def reject_statements(
    day: int,
    added_in: int,
    rejects: Iterable[RejectLike],
    *,
    per_statement: int = ROWS_PER_STATEMENT,
) -> tuple[Statement, ...]:
    payloads = [[r.row_hash, r.occ, r.reason, r.raw] for r in rejects]
    return tuple(
        (_INSERT_REJECTS_SQL, (str(day), str(added_in), _dump(chunk)))
        for chunk in _chunks(payloads, per_statement)
    )


def remove_statements(
    day: int, removed_in: int, keys: Sequence[RowKey], *, per_statement: int = KEYS_PER_STATEMENT
) -> tuple[Statement, ...]:
    """Mark keys gone at ``removed_in`` in both tables; a key lives in exactly one of them."""
    return tuple(
        (_REMOVE_SQL.format(table=table), (str(removed_in), str(day), _dump(chunk)))
        for chunk in _chunks([list(key) for key in keys], per_statement)
        for table in ROW_TABLES
    )


def _ranged(
    sql: str,
    day: int,
    ranges: int,
    *,
    head: tuple[str, ...] = (),
    tail: tuple[str, ...] = (),
) -> tuple[Statement, ...]:
    return tuple(
        (sql.format(table=table), (*head, str(day), low, high, *tail))
        for table in ROW_TABLES
        for low, high in key_ranges(ranges)
    )


def recovery_statements(
    day: int, version: int, *, ranges: int = KEY_RANGES
) -> tuple[Statement, ...]:
    """Undo what a failed attempt after ``version`` wrote: its adds go, its removals revert."""
    tail = (str(version),)
    return _ranged(_RECOVER_ADDED_SQL, day, ranges, tail=tail) + _ranged(
        _RECOVER_REMOVED_SQL, day, ranges, tail=tail
    )


def retire_statements(day: int, version: int, *, ranges: int = KEY_RANGES) -> tuple[Statement, ...]:
    """Mark every row live at ``version`` removed in ``version + 1``."""
    return _ranged(_RETIRE_SQL, day, ranges, head=(str(version + 1),), tail=(str(version),))


def gc_statements(day: int, version: int, *, ranges: int = KEY_RANGES) -> tuple[Statement, ...]:
    """Delete rows no version from ``version`` on can see."""
    return _ranged(_GC_SQL, day, ranges, tail=(str(version),))


def version_statement(election_id: str, ev_date: str, state: DayVersion) -> Statement:
    """The flip: readers serve ``state.version`` from the moment this is written."""
    return (
        _VERSION_UPSERT_SQL,
        (
            election_id,
            ev_date,
            str(state.version),
            state.file_id,
            state.sha256,
            str(state.row_count),
            str(state.reject_count),
        ),
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        raise EarlyVoteSyncError("roster version column is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    raise EarlyVoteSyncError("roster version column is not an integer")


def parse_day_version(rows: Sequence[Mapping[str, object]]) -> DayVersion | None:
    if not rows:
        return None
    row = rows[0]
    file_id, sha256 = row.get("file_id"), row.get("sha256")
    if not isinstance(file_id, str) or not isinstance(sha256, str):
        raise EarlyVoteSyncError("roster version row has no file id or hash")
    return DayVersion(
        version=_as_int(row.get("version")),
        file_id=file_id,
        sha256=sha256,
        row_count=_as_int(row.get("row_count")),
        reject_count=_as_int(row.get("reject_count")),
    )


def visible_counts(rows: Sequence[Mapping[str, object]]) -> tuple[int, int]:
    if not rows:
        raise EarlyVoteSyncError("roster count query returned nothing")
    return _as_int(rows[0].get("row_count")), _as_int(rows[0].get("reject_count"))
