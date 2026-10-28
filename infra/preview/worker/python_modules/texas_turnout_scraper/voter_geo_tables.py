"""Per-voter geography tables in D1: committed DDL and the SQL the build and the route run.

Imported by ``early_vote_sync`` for the schema and by the Worker's geo route, so it must not
import sqlmodel, sqlalchemy or duckdb. The DDL is generated from ``voter_geo_models`` and pinned by
a drift test. Rows are keyed by VUID: nothing here logs or raises with row content. See
docs/adr/015-voter-geography.md.
"""

from __future__ import annotations

# Part of the build signature, so a change to how rows are derived rebuilds them.
VOTER_GEO_VERSION = 1
VUID_WIDTH = 10
FIPS_WIDTH = 3

# Bits of voter_geo.flags: what the VUID's live roster rows say beyond the first listing.
FLAG_MULTI_PRECINCT = 1
FLAG_MULTI_COUNTY = 2
FLAGS_ALL = FLAG_MULTI_PRECINCT | FLAG_MULTI_COUNTY

# Bits of voter_geo.split_flag, from geo_precinct.split_cd / split_sd / split_hd.
SPLIT_CD = 1
SPLIT_SD = 2
SPLIT_HD = 4
SPLITS_ALL = SPLIT_CD | SPLIT_SD | SPLIT_HD

VOTER_GEO_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS voter_geo (vuid TEXT NOT NULL, county_fips TEXT, pctkey TEXT, "
    "cd TEXT, sd TEXT, hd TEXT, split_flag INTEGER, flags INTEGER NOT NULL, "
    "src_hash INTEGER NOT NULL, matched_at TEXT NOT NULL, PRIMARY KEY (vuid)) WITHOUT ROWID"
)
VOTER_GEO_META_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS voter_geo_meta (name TEXT NOT NULL, value TEXT NOT NULL, "
    "PRIMARY KEY (name))"
)
VOTER_GEO_SCHEMA_CREATES: tuple[str, ...] = (VOTER_GEO_TABLE_SQL, VOTER_GEO_META_TABLE_SQL)

VOTER_GEO_PRESENT_SQL = (
    "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name IN "
    "('voter_geo', 'voter_geo_meta')"
)
VOTER_GEO_TABLE_COUNT = 2

# voter_geo columns in the order the upsert payload carries them.
VOTER_GEO_COLUMNS: tuple[str, ...] = (
    "vuid",
    "county_fips",
    "pctkey",
    "cd",
    "sd",
    "hd",
    "split_flag",
    "flags",
    "src_hash",
    "matched_at",
)
_INTEGER_COLUMNS = frozenset({"split_flag", "flags", "src_hash"})
# Every integer count of one build; ``signature`` is the only other meta row.
META_COUNT_NAMES: tuple[str, ...] = (
    "live_rows",
    "vuids",
    "vuid_normalized_changed",
    "matched_rows",
    "matched_vuids",
    "unmatched_alias_vuids",
    "no_alias_vuids",
    "unknown_county_vuids",
    "null_cd_vuids",
    "null_sd_vuids",
    "null_hd_vuids",
    "split_vuids",
    "multi_precinct_vuids",
    "multi_county_vuids",
)
META_NAMES: tuple[str, ...] = ("signature", *META_COUNT_NAMES)

# Rows per read: stored hashes are read in keyset pages of this size.
PAGE_ROWS = 10_000


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


# Each upsert or delete takes one JSON array of row arrays (of VUIDs for the delete).
VOTER_GEO_UPSERT_SQL = _upsert("voter_geo", VOTER_GEO_COLUMNS, "vuid")
VOTER_GEO_DELETE_SQL = "DELETE FROM voter_geo WHERE vuid IN (SELECT value FROM json_each(?))"
VOTER_GEO_META_UPSERT_SQL = _upsert("voter_geo_meta", ("name", "value"), "name")
VOTER_GEO_META_SELECT_SQL = "SELECT name, value FROM voter_geo_meta"
VOTER_GEO_SIGNATURE_SQL = "SELECT value FROM voter_geo_meta WHERE name = 'signature'"
# src_hash as text: D1's JSON results cannot carry every signed 64-bit integer exactly.
# Parameter: the last VUID already read ('' to start).
VOTER_GEO_STORED_HASHES_SQL = (
    "SELECT vuid, CAST(src_hash AS TEXT) AS src_hash FROM voter_geo "
    f"WHERE vuid > ? ORDER BY vuid LIMIT {PAGE_ROWS}"
)

# Live rows of one day and one row_hash range in key order, so within a day the lowest
# (row_hash, occ) comes first. Parameters: day, low, high.
VOTER_GEO_LIVE_ROWS_SQL = (
    "SELECT id_voter, county, precinct FROM early_vote_roster_row "
    "WHERE day = ? AND row_hash BETWEEN ? AND ? AND removed_in IS NULL ORDER BY row_hash, occ"
)
# One row per state precinct: ~10k rows, read once. The split columns are booleans.
PRECINCT_DISTRICTS_SQL = "SELECT pctkey, cd, sd, hd, split_cd, split_sd, split_hd FROM geo_precinct"

# The route serves a day's live VUIDs in this many row-hash ranges, one per page.
VOTER_GEO_PAGES = 64
# One page as one row: the count and one JSON text cell, so D1 result conversion stays
# constant-size and the Worker never parses it. Parameters: day, low, high. A VUID listed on
# the day twice under different row hashes can fall in two pages; within a page it is distinct.
VOTER_GEO_PAGE_SQL = (
    "SELECT COUNT(*) AS n, json_group_array(json_object("
    "'vuid', vuid, 'county_fips', county_fips, 'pctkey', pctkey, 'cd', cd, 'sd', sd, 'hd', hd, "
    "'district_source', CASE WHEN pctkey IS NOT NULL THEN 'precinct_alias' END, "
    "'split_flag', split_flag, 'flags', flags, 'lat', NULL, 'lon', NULL, "
    "'geocode_source', CASE WHEN has_row THEN 'none' END, "
    "'geocode_quality', CASE WHEN pctkey IS NOT NULL THEN 'stated_precinct' "
    "WHEN has_row THEN 'not_located' END, "
    "'address_points_vintage', NULL, 'matched_at', matched_at)) AS voters FROM ("
    "SELECT DISTINCT r.id_voter AS vuid, g.county_fips AS county_fips, g.pctkey AS pctkey, "
    "g.cd AS cd, g.sd AS sd, g.hd AS hd, g.split_flag AS split_flag, g.flags AS flags, "
    "g.vuid IS NOT NULL AS has_row, g.matched_at AS matched_at "
    "FROM early_vote_roster_row r LEFT JOIN voter_geo g ON g.vuid = r.id_voter "
    "WHERE r.day = ? AND r.row_hash BETWEEN ? AND ? AND r.removed_in IS NULL "
    "ORDER BY r.id_voter)"
)
