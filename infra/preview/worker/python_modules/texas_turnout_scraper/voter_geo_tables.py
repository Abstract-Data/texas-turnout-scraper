"""Per-voter geography tables in D1: committed DDL and the SQL the build and the route run.

Imported by ``early_vote_sync`` for the schema and by the Worker's geo route, so it must not
import sqlmodel, sqlalchemy or duckdb. The DDL is generated from ``voter_geo_models`` and pinned by
a drift test. Rows are keyed by VUID: nothing here logs or raises with row content. See
docs/adr/015-voter-geography.md.
"""

from __future__ import annotations

# Part of the build signature, so a change to how rows are derived rebuilds them.
VOTER_GEO_VERSION = 1
# The same for voter_geo_point, which only the local voter_geo module writes (Stage 2).
VOTER_GEO_POINT_VERSION = 1
GEOCODE_SOURCE_POINT = "txgio_address_point"
GEOCODE_QUALITY_POINT = "address_point"
# A residential point is stored to 6 decimals (about 0.1 m), like the ADR-010 area points.
COORD_DECIMALS = 6
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
# Stage 2: matched residential points only, one writer (the local voter_geo module). The CHECKs
# are the ADR-015 §8 invariant, since CREATE TABLE IF NOT EXISTS cannot add one later.
VOTER_GEO_POINT_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS voter_geo_point (vuid TEXT NOT NULL, lat FLOAT, lon FLOAT, "
    "cd TEXT, sd TEXT, hd TEXT, geocode_source TEXT NOT NULL, geocode_quality TEXT NOT NULL, "
    "address_points_vintage TEXT NOT NULL, src_hash INTEGER NOT NULL, matched_at TEXT NOT NULL, "
    "PRIMARY KEY (vuid), "
    "CHECK ((lat IS NOT NULL AND lon IS NOT NULL) = (geocode_quality = 'address_point')), "
    "CHECK (geocode_quality = 'address_point'), "
    "CHECK (geocode_source = 'txgio_address_point')) WITHOUT ROWID"
)
VOTER_GEO_POINT_META_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS voter_geo_point_meta (name TEXT NOT NULL, value TEXT NOT NULL, "
    "PRIMARY KEY (name))"
)
VOTER_GEO_SCHEMA_CREATES: tuple[str, ...] = (
    VOTER_GEO_TABLE_SQL,
    VOTER_GEO_META_TABLE_SQL,
    VOTER_GEO_POINT_TABLE_SQL,
    VOTER_GEO_POINT_META_TABLE_SQL,
)


def _present_sql(names: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{name}'" for name in names)
    return f"SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name IN ({quoted})"


# The Stage 1 build checks only its own tables, so it still works before the point tables exist.
VOTER_GEO_BASE_TABLES = ("voter_geo", "voter_geo_meta")
VOTER_GEO_POINT_TABLES = ("voter_geo_point", "voter_geo_point_meta")
VOTER_GEO_BASE_PRESENT_SQL = _present_sql(VOTER_GEO_BASE_TABLES)
VOTER_GEO_BASE_TABLE_COUNT = len(VOTER_GEO_BASE_TABLES)
VOTER_GEO_POINT_PRESENT_SQL = _present_sql(VOTER_GEO_POINT_TABLES)
VOTER_GEO_POINT_TABLE_COUNT = len(VOTER_GEO_POINT_TABLES)
# The route joins all of them.
VOTER_GEO_PRESENT_SQL = _present_sql((*VOTER_GEO_BASE_TABLES, *VOTER_GEO_POINT_TABLES))
VOTER_GEO_TABLE_COUNT = len(VOTER_GEO_BASE_TABLES) + len(VOTER_GEO_POINT_TABLES)

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

# voter_geo_point columns in the order the upsert payload carries them.
VOTER_GEO_POINT_COLUMNS: tuple[str, ...] = (
    "vuid",
    "lat",
    "lon",
    "cd",
    "sd",
    "hd",
    "geocode_source",
    "geocode_quality",
    "address_points_vintage",
    "src_hash",
    "matched_at",
)
# Every row of voter_geo_point_meta; ``signature`` changes exactly when a point row does, so the
# route's ETag can name it.
POINT_META_NAMES: tuple[str, ...] = (
    "signature",
    "address_points_vintage",
    "plans",
    "pipeline_rev",
    "voterfile",
    "counts",
)

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
# One query for both signatures: the route needs them for its ETag and pays one D1 round trip.
# point_signature is NULL until the first point apply.
VOTER_GEO_SIGNATURE_SQL = (
    "SELECT (SELECT value FROM voter_geo_meta WHERE name = 'signature') AS signature, "
    "(SELECT value FROM voter_geo_point_meta WHERE name = 'signature') AS point_signature"
)
# src_hash as text: D1's JSON results cannot carry every signed 64-bit integer exactly.
# Parameter: the last VUID already read ('' to start).
VOTER_GEO_STORED_HASHES_SQL = (
    "SELECT vuid, CAST(src_hash AS TEXT) AS src_hash FROM voter_geo "
    f"WHERE vuid > ? ORDER BY vuid LIMIT {PAGE_ROWS}"
)

VOTER_GEO_POINT_UPSERT_SQL = _upsert("voter_geo_point", VOTER_GEO_POINT_COLUMNS, "vuid")
VOTER_GEO_POINT_DELETE_SQL = (
    "DELETE FROM voter_geo_point WHERE vuid IN (SELECT value FROM json_each(?))"
)
VOTER_GEO_POINT_META_UPSERT_SQL = _upsert("voter_geo_point_meta", ("name", "value"), "name")
VOTER_GEO_POINT_META_SELECT_SQL = "SELECT name, value FROM voter_geo_point_meta"
VOTER_GEO_POINT_STORED_HASHES_SQL = (
    "SELECT vuid, CAST(src_hash AS TEXT) AS src_hash FROM voter_geo_point "
    f"WHERE vuid > ? ORDER BY vuid LIMIT {PAGE_ROWS}"
)
# What Stage 1 says about each VUID, for the counts of voters without a point.
# Parameter: the last VUID already read ('' to start).
VOTER_GEO_PLACES_SQL = (
    f"SELECT vuid, pctkey, cd, sd, hd FROM voter_geo WHERE vuid > ? ORDER BY vuid LIMIT {PAGE_ROWS}"
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
    "'district_source', CASE WHEN has_point THEN 'address_point' "
    "WHEN pctkey IS NOT NULL THEN 'precinct_alias' END, "
    "'split_flag', split_flag, 'flags', flags, 'lat', lat, 'lon', lon, "
    "'geocode_source', CASE WHEN has_point THEN geocode_source "
    "WHEN has_row THEN 'none' END, "
    "'geocode_quality', CASE WHEN has_point THEN geocode_quality "
    "WHEN pctkey IS NOT NULL THEN 'stated_precinct' WHEN has_row THEN 'not_located' END, "
    "'address_points_vintage', address_points_vintage, 'matched_at', matched_at)) AS voters FROM ("
    "SELECT DISTINCT r.id_voter AS vuid, g.county_fips AS county_fips, g.pctkey AS pctkey, "
    "CASE WHEN p.vuid IS NOT NULL THEN p.cd ELSE g.cd END AS cd, "
    "CASE WHEN p.vuid IS NOT NULL THEN p.sd ELSE g.sd END AS sd, "
    "CASE WHEN p.vuid IS NOT NULL THEN p.hd ELSE g.hd END AS hd, "
    "g.split_flag AS split_flag, g.flags AS flags, g.vuid IS NOT NULL AS has_row, "
    "p.vuid IS NOT NULL AS has_point, p.lat AS lat, p.lon AS lon, "
    "p.geocode_source AS geocode_source, p.geocode_quality AS geocode_quality, "
    "p.address_points_vintage AS address_points_vintage, "
    # SQLite's two-argument max() is NULL when either side is, so each NULL is handled here.
    "CASE WHEN p.vuid IS NULL THEN g.matched_at "
    "WHEN g.matched_at IS NULL OR p.matched_at > g.matched_at THEN p.matched_at "
    "ELSE g.matched_at END AS matched_at "
    "FROM early_vote_roster_row r LEFT JOIN voter_geo g ON g.vuid = r.id_voter "
    "LEFT JOIN voter_geo_point p ON p.vuid = r.id_voter "
    "WHERE r.day = ? AND r.row_hash BETWEEN ? AND ? AND r.removed_in IS NULL "
    "ORDER BY r.id_voter)"
)
