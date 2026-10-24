"""Voter attribute tables in D1: committed DDL, seeds and the SQL the voter link runs.

Imported by ``early_vote_sync`` for the schema and later by the Worker, so it must not import
sqlmodel, sqlalchemy or duckdb. The table DDL is generated from ``voter_models`` and pinned by a
drift test. Rows hold voter attributes keyed by VUID: nothing here logs or raises with row
content. See docs/adr/011-voter-attributes.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

SCORE_VERSION = 1
VUID_WIDTH = 10

HISTORY_COLUMNS: tuple[str, ...] = (
    "GEN24",
    "GEN23",
    "GEN22",
    "GEN21",
    "GEN20",
    "GEN19",
    "GEN18",
    "PRI26",
    "PRI24",
    "PRI22",
    "PRI20",
    "PRI18",
    "RO26",
    "RO24",
    "RO22",
    "RO20",
    "RO18",
)
# The only voterfile columns the link reads, in this order.
VOTERFILE_COLUMNS: tuple[str, ...] = ("VUID", "SEX", "DOB", "EDR", *HISTORY_COLUMNS)

GENERAL_CODES = frozenset({"V", "VE", "VA"})
PRIMARY_CODES = frozenset({"D", "R", "DE", "RE", "DA", "RA"})
SEXES = frozenset({"F", "M", "U"})
PRESIDENTIAL_GENERALS = ("GEN24", "GEN20")
MIDTERM_GENERALS = ("GEN22", "GEN18")
OFF_YEAR_GENERALS = ("GEN23", "GEN21", "GEN19")
PRIMARIES_AND_RUNOFFS = tuple(c for c in HISTORY_COLUMNS if not c.startswith("GEN"))

# Bits of early_vote_voter.flags. A bad date is stored as NULL; the voter still loads.
FLAG_DOB_INVALID = 1
FLAG_EDR_INVALID = 2
DOB_MIN_YEAR = 1880
EDR_MIN_YEAR = 1900

# Early-voting day files carry the voting method; the voterfile suffix letter for each.
DATED_MODES = {"IN-PERSON": "E", "MAIL-IN": "A"}


@dataclass(frozen=True)
class CalendarElection:
    key: str
    election_date: str
    early_days: int
    first_early_date: str
    is_current: bool
    civix_ids: tuple[int, ...]


# Elections whose early-voting days the Civix portal lists (verified 2026-10-24). Nothing before
# 2025 is listed, so earlier history is mode-only. date_turnout_id k is "Early Day k".
ELECTION_CALENDAR: tuple[CalendarElection, ...] = (
    CalendarElection("GEN25", "2025-11-04", 12, "2025-10-20", False, (51031,)),
    CalendarElection("PRI26", "2026-03-03", 11, "2026-02-17", False, (53813, 53814)),
    CalendarElection("RO26", "2026-05-26", 5, "2026-05-18", False, (58314, 58315)),
    CalendarElection("GEN26", "2026-11-03", 17, "2026-10-14", True, (53815,)),
)
CALENDAR_BY_KEY = {e.key: e for e in ELECTION_CALENDAR}
# Past elections whose early day each voter voted on is stored (gen25_day, pri26_day, ro26_day).
DATED_KEYS: tuple[str, ...] = ("GEN25", "PRI26", "RO26")

# (min_age, max_age, label, sort). The view joins on BETWEEN, so the ranges must not overlap.
AGE_TIERS: tuple[tuple[int, int, str, int], ...] = (
    (18, 24, "18-24", 1),
    (25, 34, "25-34", 2),
    (35, 54, "35-54", 3),
    (55, 64, "55-64", 4),
    (65, 74, "65-74", 5),
    (75, 84, "75-84", 6),
    (85, 200, "85+", 7),
)

VOTER_TABLE = "early_vote_voter"
COUNT_COLUMNS: tuple[str, ...] = (
    "n_voted",
    "n_early",
    "n_absentee",
    "n_election_day",
    "n_gen_pres",
    "n_gen_mid",
    "n_gen_off",
    "n_primary_r",
    "n_primary_d",
)
# early_vote_voter columns in the order the upsert payload carries them.
VOTER_COLUMNS: tuple[str, ...] = (
    "vuid",
    "sex",
    "dob",
    "edr",
    "flags",
    *(c.lower() for c in HISTORY_COLUMNS),
    "gen25_day",
    "gen25_mode",
    "pri26_day",
    "ro26_day",
    *COUNT_COLUMNS,
    "primary_pattern",
    "general_pattern",
    "typical_mode",
    "early_or_absentee_share",
    "typical_earliness",
    "earliness_n",
    "score_version",
    "src_hash",
)
_INTEGER_COLUMNS = frozenset(
    {"flags", "gen25_day", "pri26_day", "ro26_day", *COUNT_COLUMNS, "earliness_n"}
    | {"score_version", "src_hash"}
)

VOTER_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_voter (vuid VARCHAR NOT NULL, sex VARCHAR NOT NULL, "
    "dob VARCHAR, edr VARCHAR, flags INTEGER NOT NULL, gen24 VARCHAR, gen23 VARCHAR, "
    "gen22 VARCHAR, gen21 VARCHAR, gen20 VARCHAR, gen19 VARCHAR, gen18 VARCHAR, pri26 VARCHAR, "
    "pri24 VARCHAR, pri22 VARCHAR, pri20 VARCHAR, pri18 VARCHAR, ro26 VARCHAR, ro24 VARCHAR, "
    "ro22 VARCHAR, ro20 VARCHAR, ro18 VARCHAR, gen25_day INTEGER, gen25_mode VARCHAR, "
    "pri26_day INTEGER, ro26_day INTEGER, n_voted INTEGER NOT NULL, n_early INTEGER NOT NULL, "
    "n_absentee INTEGER NOT NULL, n_election_day INTEGER NOT NULL, "
    "n_gen_pres INTEGER NOT NULL, n_gen_mid INTEGER NOT NULL, n_gen_off INTEGER NOT NULL, "
    "n_primary_r INTEGER NOT NULL, n_primary_d INTEGER NOT NULL, "
    "primary_pattern VARCHAR NOT NULL, general_pattern VARCHAR NOT NULL, typical_mode VARCHAR, "
    "early_or_absentee_share FLOAT, typical_earliness FLOAT, earliness_n INTEGER NOT NULL, "
    "score_version INTEGER NOT NULL, src_hash INTEGER NOT NULL, PRIMARY KEY (vuid)) "
    "WITHOUT ROWID"
)
CALENDAR_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_election_calendar (election_key VARCHAR NOT NULL, "
    "election_date VARCHAR NOT NULL, early_days INTEGER NOT NULL, "
    "first_early_date VARCHAR NOT NULL, is_current INTEGER NOT NULL, "
    "PRIMARY KEY (election_key))"
)
AGE_TIER_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS age_tier (min_age INTEGER NOT NULL, max_age INTEGER NOT NULL, "
    "label VARCHAR NOT NULL, sort INTEGER NOT NULL, PRIMARY KEY (label))"
)
REJECT_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_voter_reject (vuid VARCHAR NOT NULL, "
    "reason VARCHAR NOT NULL, raw JSON NOT NULL, PRIMARY KEY (vuid)) WITHOUT ROWID"
)
META_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS early_vote_voter_meta (name VARCHAR NOT NULL, "
    "value VARCHAR NOT NULL, PRIMARY KEY (name))"
)


def _age_at(reference: str) -> str:
    """Whole years from ``dob`` to the ISO date ``reference`` (an SQL expression).

    One less than the year difference while the reference month-day is before the birth
    month-day; a Feb 29 birthday has passed on Mar 1 of a common year.
    """
    return (
        f"(CAST(substr({reference}, 1, 4) AS INTEGER) - CAST(substr(dob, 1, 4) AS INTEGER) "
        f"- (substr({reference}, 6, 5) < substr(dob, 6, 5)))"
    )


_TODAY = "date('now')"
_CURRENT_ELECTION_DATE = (
    "(SELECT election_date FROM early_vote_election_calendar WHERE is_current = 1 LIMIT 1)"
)
# date('now') is the UTC date, so age_today turns over at UTC midnight. A view cannot be
# changed by CREATE ... IF NOT EXISTS: a new definition needs DROP VIEW first.
AGE_VIEW_SQL = (
    "CREATE VIEW IF NOT EXISTS early_vote_voter_age AS SELECT a.vuid, a.age_today, "
    "t.label AS age_tier_today, a.age_election_day, e.label AS age_tier_election_day FROM ("
    "SELECT vuid, CASE WHEN dob IS NULL THEN NULL ELSE "
    f"{_age_at(_TODAY)} END AS age_today, "
    f"CASE WHEN dob IS NULL THEN NULL ELSE {_age_at(_CURRENT_ELECTION_DATE)} END "
    "AS age_election_day FROM early_vote_voter) a "
    "LEFT JOIN age_tier t ON a.age_today BETWEEN t.min_age AND t.max_age "
    "LEFT JOIN age_tier e ON a.age_election_day BETWEEN e.min_age AND e.max_age"
)
AGE_VIEW_COLUMNS = (
    "vuid",
    "age_today",
    "age_tier_today",
    "age_election_day",
    "age_tier_election_day",
)

VOTER_SCHEMA_CREATES: tuple[str, ...] = (
    VOTER_TABLE_SQL,
    CALENDAR_TABLE_SQL,
    AGE_TIER_TABLE_SQL,
    REJECT_TABLE_SQL,
    META_TABLE_SQL,
    AGE_VIEW_SQL,
)

VOTER_TABLES_PRESENT_SQL = (
    "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name IN "
    "('early_vote_voter', 'early_vote_election_calendar', 'age_tier', "
    "'early_vote_voter_reject', 'early_vote_voter_meta')"
)
VOTER_TABLE_COUNT = 5


def _at(index: int, column: str) -> str:
    value = f"json_extract(value, '$[{index}]')"
    return f"CAST({value} AS INTEGER)" if column in _INTEGER_COLUMNS else value


def _upsert(table: str, columns: tuple[str, ...], key: str) -> str:
    # WHERE true is SQLite's rule for INSERT ... SELECT with an upsert clause.
    picks = ", ".join(_at(index, column) for index, column in enumerate(columns))
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns if c != key)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) SELECT {picks} FROM json_each(?) "
        f"WHERE true ON CONFLICT({key}) DO UPDATE SET {updates}"
    )


CALENDAR_COLUMNS = ("election_key", "election_date", "early_days", "first_early_date", "is_current")
AGE_TIER_COLUMNS = ("min_age", "max_age", "label", "sort")
REJECT_COLUMNS = ("vuid", "reason", "raw")

# Each upsert takes one JSON array of row arrays, in the column order of its column tuple.
VOTER_UPSERT_SQL = _upsert(VOTER_TABLE, VOTER_COLUMNS, "vuid")
CALENDAR_UPSERT_SQL = (
    f"INSERT INTO early_vote_election_calendar ({', '.join(CALENDAR_COLUMNS)}) "
    "SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), "
    "CAST(json_extract(value, '$[2]') AS INTEGER), json_extract(value, '$[3]'), "
    "CAST(json_extract(value, '$[4]') AS INTEGER) FROM json_each(?) WHERE true "
    "ON CONFLICT(election_key) DO UPDATE SET election_date = excluded.election_date, "
    "early_days = excluded.early_days, first_early_date = excluded.first_early_date, "
    "is_current = excluded.is_current"
)
AGE_TIER_UPSERT_SQL = (
    "INSERT INTO age_tier (min_age, max_age, label, sort) SELECT "
    "CAST(json_extract(value, '$[0]') AS INTEGER), CAST(json_extract(value, '$[1]') AS INTEGER), "
    "json_extract(value, '$[2]'), CAST(json_extract(value, '$[3]') AS INTEGER) "
    "FROM json_each(?) WHERE true ON CONFLICT(label) DO UPDATE SET min_age = excluded.min_age, "
    "max_age = excluded.max_age, sort = excluded.sort"
)
# raw travels as a nested JSON object; json() stores it as JSON text.
REJECT_UPSERT_SQL = (
    "INSERT INTO early_vote_voter_reject (vuid, reason, raw) SELECT json_extract(value, '$[0]'), "
    "json_extract(value, '$[1]'), json(json_extract(value, '$[2]')) FROM json_each(?) "
    "WHERE true ON CONFLICT(vuid) DO UPDATE SET reason = excluded.reason, raw = excluded.raw"
)
REJECT_DELETE_SQL = (
    "DELETE FROM early_vote_voter_reject WHERE vuid IN (SELECT value FROM json_each(?))"
)
META_SELECT_SQL = "SELECT name, value FROM early_vote_voter_meta"
META_UPSERT_SQL = (
    "INSERT INTO early_vote_voter_meta (name, value) SELECT json_extract(value, '$[0]'), "
    "json_extract(value, '$[1]') FROM json_each(?) WHERE true "
    "ON CONFLICT(name) DO UPDATE SET value = excluded.value"
)


def seed_statements() -> tuple[tuple[str, list[str]], ...]:
    """Upserts that bring the calendar and the age tiers to the committed constants."""
    calendar = [
        [e.key, e.election_date, e.early_days, e.first_early_date, int(e.is_current)]
        for e in ELECTION_CALENDAR
    ]
    tiers = [list(tier) for tier in AGE_TIERS]
    return (
        (CALENDAR_UPSERT_SQL, [json.dumps(calendar, separators=(",", ":"))]),
        (AGE_TIER_UPSERT_SQL, [json.dumps(tiers, separators=(",", ":"))]),
    )


# src_hash as text: D1's JSON results cannot carry every signed 64-bit integer exactly.
# Parameters: lowest and highest VUID of one slice of the target set.
VOTER_HASHES_SQL = (
    "SELECT vuid, CAST(src_hash AS TEXT) AS src_hash FROM early_vote_voter "
    "WHERE vuid BETWEEN ? AND ?"
)
REJECTS_SQL = "SELECT vuid, reason, raw FROM early_vote_voter_reject WHERE vuid BETWEEN ? AND ?"
# Live VUIDs of one day and one row_hash range. Parameters: day, low, high.
TARGET_VUIDS_SQL = (
    "SELECT DISTINCT id_voter FROM early_vote_roster_row "
    "WHERE day = ? AND row_hash BETWEEN ? AND ? AND removed_in IS NULL"
)
