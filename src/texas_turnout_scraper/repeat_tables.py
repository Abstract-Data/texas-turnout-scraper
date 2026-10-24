"""Repeat-VUID tables in D1: committed DDL and the SQL the repeat build runs.

Imported by ``early_vote_sync`` for the schema and later by the Worker, so it must not import
sqlmodel or sqlalchemy. The DDL is generated from ``repeat_models`` and pinned by a drift test.
Rows are keyed by VUID: nothing here logs or raises with row content. See
docs/adr/012-repeat-vuids.md.
"""

from __future__ import annotations

# Part of the build signature, so a change to how rows are derived rebuilds them.
REPEAT_VERSION = 1
VUID_WIDTH = 10

# Bits of early_vote_vuid_repeat.flags.
FLAG_SAME_DAY = 1
FLAG_CROSS_DAY = 2
FLAG_CROSS_METHOD = 4
FLAG_REMOVED = 8
FLAGS_ALL = FLAG_SAME_DAY | FLAG_CROSS_DAY | FLAG_CROSS_METHOD | FLAG_REMOVED

REPEAT_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_vuid_repeat (vuid TEXT NOT NULL, "
    "n_counted INTEGER NOT NULL, n_removed INTEGER NOT NULL, first_day TEXT, "
    "counted_days JSON NOT NULL, appearances JSON NOT NULL, removed_days JSON NOT NULL, "
    "flags INTEGER NOT NULL, src_hash INTEGER NOT NULL, PRIMARY KEY (vuid)) WITHOUT ROWID"
)
EXCESS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_repeat_excess (ev_date TEXT NOT NULL, "
    "county TEXT NOT NULL, voting_method TEXT NOT NULL, n INTEGER NOT NULL, "
    "PRIMARY KEY (ev_date, county, voting_method))"
)
META_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_repeat_meta (name TEXT NOT NULL, "
    "value TEXT NOT NULL, PRIMARY KEY (name))"
)
REPEAT_SCHEMA_CREATES: tuple[str, ...] = (REPEAT_TABLE_SQL, EXCESS_TABLE_SQL, META_TABLE_SQL)

REPEAT_TABLES_PRESENT_SQL = (
    "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name IN "
    "('early_vote_vuid_repeat', 'early_vote_repeat_excess', 'early_vote_repeat_meta')"
)
REPEAT_TABLE_COUNT = 3

# early_vote_vuid_repeat columns in the order the upsert payload carries them.
REPEAT_COLUMNS: tuple[str, ...] = (
    "vuid",
    "n_counted",
    "n_removed",
    "first_day",
    "counted_days",
    "appearances",
    "removed_days",
    "flags",
    "src_hash",
)
_INTEGER_COLUMNS = frozenset({"n_counted", "n_removed", "flags", "src_hash"})
_JSON_COLUMNS = frozenset({"counted_days", "appearances", "removed_days"})
EXCESS_COLUMNS: tuple[str, ...] = ("ev_date", "county", "voting_method", "n")
META_NAMES: tuple[str, ...] = (
    "signature",
    "raw_rows",
    "distinct_voters",
    "repeat_voters",
    "excess_rows",
    "removed_voters",
)

# Rows per read: stored hashes and removal events are read in keyset pages of this size.
PAGE_ROWS = 10_000


def _at(index: int, column: str) -> str:
    value = f"json_extract(value, '$[{index}]')"
    if column in _INTEGER_COLUMNS:
        return f"CAST({value} AS INTEGER)"
    # A nested array travels as JSON; json() stores it as minified JSON text.
    return f"json({value})" if column in _JSON_COLUMNS else value


def _upsert(table: str, columns: tuple[str, ...], keys: tuple[str, ...]) -> str:
    # WHERE true is SQLite's rule for INSERT ... SELECT with an upsert clause.
    picks = ", ".join(_at(index, column) for index, column in enumerate(columns))
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c not in keys)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) SELECT {picks} FROM json_each(?) "
        f"WHERE true ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}"
    )


# Each upsert or delete takes one JSON array of row arrays, in its column order.
REPEAT_UPSERT_SQL = _upsert("early_vote_vuid_repeat", REPEAT_COLUMNS, ("vuid",))
REPEAT_DELETE_SQL = (
    "DELETE FROM early_vote_vuid_repeat WHERE vuid IN (SELECT value FROM json_each(?))"
)
EXCESS_UPSERT_SQL = _upsert(
    "early_vote_repeat_excess", EXCESS_COLUMNS, ("ev_date", "county", "voting_method")
)
EXCESS_DELETE_SQL = (
    "DELETE FROM early_vote_repeat_excess WHERE (ev_date, county, voting_method) IN "
    "(SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), "
    "json_extract(value, '$[2]') FROM json_each(?))"
)
META_UPSERT_SQL = _upsert("early_vote_repeat_meta", ("name", "value"), ("name",))

META_SELECT_SQL = "SELECT name, value FROM early_vote_repeat_meta"
# At most one row per (day, county, method): a few thousand rows.
EXCESS_SELECT_SQL = "SELECT ev_date, county, voting_method, n FROM early_vote_repeat_excess"
# src_hash as text: D1's JSON results cannot carry every signed 64-bit integer exactly.
# Parameter: the last VUID already read ('' to start).
STORED_HASHES_SQL = (
    "SELECT vuid, CAST(src_hash AS TEXT) AS src_hash FROM early_vote_vuid_repeat "
    f"WHERE vuid > ? ORDER BY vuid LIMIT {PAGE_ROWS}"
)

# The build signature's inputs. Parameter: source_election_id.
ROSTER_VERSIONS_SQL = (
    "SELECT ev_date, version, sha256, row_count, reject_count FROM early_vote_roster_version "
    "WHERE source_election_id = ? ORDER BY ev_date"
)
CHANGE_LOG_SQL = (
    "SELECT COUNT(*) AS n, MAX(recorded_at) AS last_recorded FROM early_vote_day_change "
    "WHERE source_election_id = ?"
)
# Live rows of one day and one row_hash range in key order, so within a day the lowest
# (row_hash, occ) comes first. Parameters: day, low, high.
LIVE_ROWS_SQL = (
    "SELECT id_voter, voting_method, county FROM early_vote_roster_row "
    "WHERE day = ? AND row_hash BETWEEN ? AND ? AND removed_in IS NULL ORDER BY row_hash, occ"
)
# The change log's days, one primary-key seek each. Parameters: source_election_id, the last
# day already read ('' to start).
NEXT_CHANGE_DAY_SQL = (
    "SELECT MIN(ev_date) AS ev_date FROM early_vote_day_change "
    "WHERE source_election_id = ? AND ev_date > ?"
)
# One page of a day's removal events. Parameters: source_election_id, ev_date, and the last
# (new_sha256, id_voter) already read ('', '' to start).
REMOVALS_SQL = (
    "SELECT new_sha256, id_voter FROM early_vote_day_change "
    "WHERE source_election_id = ? AND ev_date = ? AND change_kind = 'removed' "
    "AND (new_sha256, id_voter) > (?, ?) ORDER BY new_sha256, id_voter "
    f"LIMIT {PAGE_ROWS}"
)
