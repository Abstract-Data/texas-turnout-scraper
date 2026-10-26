"""Geography tables in D1: committed DDL and the SQL the loader runs.

Imported by ``early_vote_sync`` for the schema and later by the Worker, so it must not import
sqlmodel, sqlalchemy or the geo build libraries. The DDL is generated from ``geo_models`` and
pinned by a drift test. See docs/adr/010-geography-tables.md.
"""

from __future__ import annotations

from collections.abc import Iterable

AREA_KINDS = frozenset({"county", "precinct", "cd", "sd", "hd"})
# Tried in this order; the first that finds a state precinct in the same county wins.
ALIAS_RULES = ("exact", "zfill4", "alnum_zfill4", "unmatched")
META_NAME = "tx_geo"
GEO_ROWS_PER_STATEMENT = 1000

# The box columns are nullable: a table created before they existed gains them by ALTER, which
# cannot add a NOT NULL column without a default, and its rows keep NULL until the next load.
BBOX_COLUMNS = ("min_lat", "min_lon", "max_lat", "max_lon")
AREA_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS geo_area (kind VARCHAR NOT NULL, code VARCHAR NOT NULL, "
    "name VARCHAR NOT NULL, lat FLOAT NOT NULL, lon FLOAT NOT NULL, min_lat FLOAT, "
    "min_lon FLOAT, max_lat FLOAT, max_lon FLOAT, PRIMARY KEY (kind, code))"
)
AREA_COLUMNS_SQL = "SELECT name FROM pragma_table_info('geo_area')"
PRECINCT_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS geo_precinct (pctkey VARCHAR NOT NULL, "
    "county_fips VARCHAR NOT NULL, prec VARCHAR NOT NULL, cd VARCHAR, sd VARCHAR, hd VARCHAR, "
    "cd_share FLOAT, sd_share FLOAT, hd_share FLOAT, split_cd BOOLEAN, split_sd BOOLEAN, "
    "split_hd BOOLEAN, PRIMARY KEY (pctkey))"
)
ALIAS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS geo_precinct_alias (county VARCHAR NOT NULL, "
    "precinct VARCHAR NOT NULL, pctkey VARCHAR, rule VARCHAR NOT NULL, "
    "PRIMARY KEY (county, precinct))"
)
META_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS geo_meta (name VARCHAR NOT NULL, sha256 VARCHAR NOT NULL, "
    "PRIMARY KEY (name))"
)
GEO_SCHEMA_CREATES: tuple[str, ...] = (
    AREA_TABLE_SQL,
    PRECINCT_TABLE_SQL,
    ALIAS_TABLE_SQL,
    META_TABLE_SQL,
)


def geo_migration_statements(area_columns: Iterable[str]) -> list[str]:
    """ALTERs that bring an older ``geo_area`` to the current columns; none once it is current.

    ``area_columns`` is the result of ``AREA_COLUMNS_SQL``. A missing table has no columns, so
    nothing is altered (its CREATE has the full shape).
    """
    present = set(area_columns)
    if not present:
        return []
    return [
        f"ALTER TABLE geo_area ADD COLUMN {column} FLOAT"
        for column in BBOX_COLUMNS
        if column not in present
    ]


GEO_TABLES_PRESENT_SQL = (
    "SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name IN "
    "('geo_area', 'geo_precinct', 'geo_precinct_alias', 'geo_meta')"
)
GEO_TABLE_COUNT = 4
META_SELECT_SQL = "SELECT sha256 FROM geo_meta WHERE name = ?"
META_UPSERT_SQL = (
    "INSERT INTO geo_meta (name, sha256) VALUES (?, ?) "
    "ON CONFLICT(name) DO UPDATE SET sha256 = excluded.sha256"
)
AREA_KEYS_SQL = "SELECT kind, code FROM geo_area"
PRECINCT_KEYS_SQL = "SELECT pctkey FROM geo_precinct"
PRECINCT_LOOKUP_SQL = "SELECT pctkey, county_fips, prec FROM geo_precinct"
COUNTY_NAMES_SQL = "SELECT code, name FROM geo_area WHERE kind = 'county'"
ALIASES_SQL = "SELECT county, precinct, pctkey, rule FROM geo_precinct_alias"


def _at(index: int) -> str:
    return f"json_extract(value, '$[{index}]')"


def _columns(count: int) -> str:
    return ", ".join(_at(index) for index in range(count))


# Each upsert takes one JSON array of row arrays, in the column order written below.
AREA_COLUMNS = ("kind", "code", "name", "lat", "lon", *BBOX_COLUMNS)
AREA_UPSERT_SQL = (
    f"INSERT INTO geo_area ({', '.join(AREA_COLUMNS)}) "
    f"SELECT {_columns(len(AREA_COLUMNS))} FROM json_each(?) WHERE true "
    "ON CONFLICT(kind, code) DO UPDATE SET "
    + ", ".join(f"{name} = excluded.{name}" for name in AREA_COLUMNS[2:])
)
PRECINCT_COLUMNS = (
    "pctkey",
    "county_fips",
    "prec",
    "cd",
    "sd",
    "hd",
    "cd_share",
    "sd_share",
    "hd_share",
    "split_cd",
    "split_sd",
    "split_hd",
)
PRECINCT_UPSERT_SQL = (
    f"INSERT INTO geo_precinct ({', '.join(PRECINCT_COLUMNS)}) "
    f"SELECT {_columns(len(PRECINCT_COLUMNS))} FROM json_each(?) WHERE true "
    "ON CONFLICT(pctkey) DO UPDATE SET "
    + ", ".join(f"{name} = excluded.{name}" for name in PRECINCT_COLUMNS[1:])
)
ALIAS_UPSERT_SQL = (
    f"INSERT INTO geo_precinct_alias (county, precinct, pctkey, rule) SELECT {_columns(4)} "
    "FROM json_each(?) WHERE true ON CONFLICT(county, precinct) DO UPDATE SET "
    "pctkey = excluded.pctkey, rule = excluded.rule"
)
AREA_DELETE_SQL = (
    f"DELETE FROM geo_area WHERE (kind, code) IN (SELECT {_columns(2)} FROM json_each(?))"
)
PRECINCT_DELETE_SQL = (
    f"DELETE FROM geo_precinct WHERE pctkey IN (SELECT {_at(0)} FROM json_each(?))"
)
ALIAS_DELETE_SQL = (
    "DELETE FROM geo_precinct_alias WHERE (county, precinct) IN "
    f"(SELECT {_columns(2)} FROM json_each(?))"
)

ROSTER_DAYS_SQL = (
    "SELECT ev_date FROM early_vote_roster_version WHERE source_election_id = ? ORDER BY ev_date"
)
# Live rows of one day and one row_hash range, counted per (county, precinct). Parameters:
# day, low, high. Bounded by the primary key so no statement scans the whole table.
PAIR_COUNTS_SQL = (
    "SELECT county, precinct, COUNT(*) AS n FROM early_vote_roster_row "
    "WHERE day = ? AND row_hash BETWEEN ? AND ? AND removed_in IS NULL GROUP BY county, precinct"
)
