"""Per-voter geography: place each live roster VUID by precinct alias, diff-only writes."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable
from typing_extensions import override

from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError
from texas_turnout_scraper.early_vote_roster_rows import (
    ROW_TABLE_SQL,
    VERSION_TABLE_SQL,
    day_number,
)
from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES
from texas_turnout_scraper.geo_tables import GEO_SCHEMA_CREATES
from texas_turnout_scraper.voter_geo_build import (
    VoterGeoResult,
    output_lines,
    run_voter_geo_build_on_store,
)
from texas_turnout_scraper.voter_geo_models import VoterGeo, VoterGeoMeta, VoterGeoRow
from texas_turnout_scraper.voter_geo_tables import (
    FLAG_MULTI_COUNTY,
    FLAG_MULTI_PRECINCT,
    SPLIT_CD,
    SPLIT_HD,
    SPLIT_SD,
    VOTER_GEO_COLUMNS,
    VOTER_GEO_META_TABLE_SQL,
    VOTER_GEO_SCHEMA_CREATES,
    VOTER_GEO_TABLE_SQL,
)

ELECTION = "53815"
D1, D2, D3 = "2026-10-20", "2026-10-21", "2026-10-24"
T1 = datetime(2026, 10, 27, 1, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 27, 2, 0, 0, tzinfo=timezone.utc)

# Synthetic geography: (pctkey, county_fips, prec, cd, sd, hd, split_cd, split_sd, split_hd).
PRECINCTS = [
    ("4530101", "453", "0101", "37", "14", "46", 0, 0, 0),
    ("0290007", "029", "0007", "20", "26", "123", 1, 0, 1),
    ("1230007", "123", "0007", "27", "18", "30", 0, 0, 0),
    ("0290050", "029", "0050", None, "26", None, 0, 0, 0),
    ("4530102", "453", "0102", "10", "14", "47", 0, 1, 0),
]
COUNTIES = [("453", "Travis"), ("029", "Bexar"), ("123", "DeWitt")]
# (county, precinct, pctkey, rule): one for each alias rule, plus the two ways to be unmatched.
ALIASES = [
    ("TRAVIS", "0101", "4530101", "exact"),
    ("BEXAR", "7", "0290007", "zfill4"),
    ("DE WITT", "7A", "1230007", "alnum_zfill4"),
    ("BEXAR", "0050", "0290050", "exact"),
    ("TRAVIS", "ZZZ", None, "unmatched"),
    ("ATLANTIS", "1", None, "unmatched"),
]
PAIR_PCTKEY = {(c, p): k for c, p, k, _ in ALIASES}
# What the synthetic geography says each roster pair is: (fips, pctkey, cd, sd, hd, split).
EXPECTED_PLACE: dict[tuple[str, str], tuple[Any, ...]] = {
    ("TRAVIS", "0101"): ("453", "4530101", "37", "14", "46", 0),
    ("BEXAR", "7"): ("029", "0290007", "20", "26", "123", SPLIT_CD | SPLIT_HD),
    ("DE WITT", "7A"): ("123", "1230007", "27", "18", "30", 0),
    ("BEXAR", "0050"): ("029", "0290050", None, "26", None, 0),
    ("TRAVIS", "ZZZ"): ("453", None, None, None, None, None),
    ("ATLANTIS", "1"): (None, None, None, None, None, None),
    ("BEXAR", "99"): ("029", None, None, None, None, None),
}


def _v(n: int) -> str:
    return f"{n:010d}"


class _Store:
    """Roster, geography and (after the schema step) voter_geo tables on sqlite, real DDL."""

    def __init__(self, *, voter_geo_tables: bool = True, geography: bool = True) -> None:
        self.conn = sqlite3.connect(":memory:")
        for sql in (ROW_TABLE_SQL, VERSION_TABLE_SQL):
            self.conn.execute(sql)
        if geography:
            self.load_geography()
        if voter_geo_tables:
            for sql in VOTER_GEO_SCHEMA_CREATES:
                self.conn.execute(sql)
        self.statements: list[tuple[str, list[str]]] = []
        self.schema_calls = 0
        self.next_hash: dict[str, int] = {}

    def load_geography(self) -> None:
        for sql in GEO_SCHEMA_CREATES:
            self.conn.execute(sql)
        self.conn.executemany(
            "INSERT INTO geo_area (kind, code, name, lat, lon) VALUES ('county', ?, ?, 30, -97)",
            COUNTIES,
        )
        self.conn.executemany(
            "INSERT INTO geo_precinct (pctkey, county_fips, prec, cd, sd, hd, split_cd, "
            "split_sd, split_hd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            PRECINCTS,
        )
        self.conn.executemany("INSERT INTO geo_precinct_alias VALUES (?, ?, ?, ?)", ALIASES)
        self.conn.execute("INSERT INTO geo_meta VALUES ('tx_geo', 'geo-sha-1')")

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return "db"

    def _ensure_schema(self, database_id: str) -> None:
        self.schema_calls += 1
        for sql in (*GEO_SCHEMA_CREATES, *VOTER_GEO_SCHEMA_CREATES):
            self.conn.execute(sql)

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        for sql, params in statements:
            self.statements.append((sql, list(params)))
            self.conn.execute(sql, params)

    def rows(self, sql: str, params: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        return self.conn.execute(sql, params).fetchall()

    def add(
        self,
        ev_date: str,
        vuid: str,
        county: str = "TRAVIS",
        precinct: str = "0101",
        *,
        row_hash: int | None = None,
        removed_in: int | None = None,
    ) -> None:
        """One roster row. Without ``row_hash`` rows of a day take increasing hashes."""
        self.conn.execute(
            "INSERT OR IGNORE INTO early_vote_roster_version VALUES (?, ?, 1, 'f', 's', 0, 0)",
            (ELECTION, ev_date),
        )
        if row_hash is None:
            row_hash = self.next_hash.get(ev_date, -(2**62))
            self.next_hash[ev_date] = row_hash + 2**40
        self.conn.execute(
            "INSERT INTO early_vote_roster_row (county, voter_name, id_voter, voting_method, "
            "precinct, day, row_hash, occ, extra, added_in, removed_in) "
            "VALUES (?, 'FAKE PERSON', ?, 'IN-PERSON', ?, ?, ?, 0, '{}', 1, ?)",
            (county, vuid, precinct, day_number(ev_date), row_hash, removed_in),
        )

    def bump(self, ev_date: str) -> None:
        """A new roster version for the day, as a reload that changed the file writes."""
        self.conn.execute(
            "UPDATE early_vote_roster_version SET version = version + 1, sha256 = sha256 || 'x' "
            "WHERE ev_date = ?",
            (ev_date,),
        )

    def geo_row(self, vuid: str) -> dict[str, Any] | None:
        rows = self._query_rows("db", "SELECT * FROM voter_geo WHERE vuid = ?", [vuid])
        return rows[0] if rows else None

    def upserts(self) -> list[list[Any]]:
        return [
            row
            for sql, params in self.statements
            if sql.startswith("INSERT INTO voter_geo (")
            for row in json.loads(params[0])
        ]


EXPECTED_FLAGS = {
    _v(8): FLAG_MULTI_PRECINCT | FLAG_MULTI_COUNTY,
    _v(9): FLAG_MULTI_PRECINCT,
    _v(11): FLAG_MULTI_PRECINCT | FLAG_MULTI_COUNTY,
    _v(12): FLAG_MULTI_PRECINCT | FLAG_MULTI_COUNTY,
}


def _populate(store: _Store) -> None:
    """The synthetic roster; ``EXPECTED_FLAGS`` lists the VUIDs whose listings disagree."""
    store.add(D1, _v(1), "TRAVIS", "0101")
    store.add(D1, _v(2), "BEXAR", "7")
    store.add(D2, _v(3), "DE WITT", "7A")
    store.add(D2, _v(4), "TRAVIS", "ZZZ")
    store.add(D3, _v(5), "ATLANTIS", "1")
    store.add(D3, _v(6), "BEXAR", "99")
    store.add(D2, _v(7), "BEXAR", "0050")
    # Different precinct and county on a later day: first listing wins, both flags set.
    store.add(D2, _v(8), "TRAVIS", "0101")
    store.add(D3, _v(8), "BEXAR", "7")
    # Different precinct, same county.
    store.add(D2, _v(9), "TRAVIS", "0101")
    store.add(D3, _v(9), "TRAVIS", "ZZZ")
    # The same pair twice on one day and again later: no flag.
    store.add(D2, _v(10), "TRAVIS", "0101")
    store.add(D2, _v(10), "TRAVIS", "0101")
    store.add(D3, _v(10), "TRAVIS", "0101")
    # Inserted day 3 first: the earliest day is still the first listing.
    store.add(D3, _v(11), "TRAVIS", "0101")
    store.add(D2, _v(11), "BEXAR", "7")
    # Same day: the lower row hash is first even though it was inserted second.
    store.add(D2, _v(12), "TRAVIS", "0101", row_hash=2**40)
    store.add(D2, _v(12), "BEXAR", "7", row_hash=-(2**40))
    # Removed rows do not place anyone.
    store.add(D1, _v(13), "TRAVIS", "0101", removed_in=1)


FIRST_PAIR = {
    _v(1): ("TRAVIS", "0101"),
    _v(2): ("BEXAR", "7"),
    _v(3): ("DE WITT", "7A"),
    _v(4): ("TRAVIS", "ZZZ"),
    _v(5): ("ATLANTIS", "1"),
    _v(6): ("BEXAR", "99"),
    _v(7): ("BEXAR", "0050"),
    _v(8): ("TRAVIS", "0101"),
    _v(9): ("TRAVIS", "0101"),
    _v(10): ("TRAVIS", "0101"),
    _v(11): ("BEXAR", "7"),
    _v(12): ("BEXAR", "7"),
}
# 17 live rows: voter 8, 9, 11, 12 have two, voter 10 three, the rest one; voter 13's is removed.
LIVE_ROWS = 7 + 2 * 4 + 3
MATCHED_ROWS = 14  # every live row except voters 4 (ZZZ), 5 (ATLANTIS), 6 (BEXAR 99) and v9's ZZZ


def _placed(store: _Store, vuid: str) -> dict[str, Any]:
    row = store.geo_row(vuid)
    assert row is not None, "a live VUID has no row"
    return row


@pytest.fixture
def store() -> _Store:
    s = _Store()
    _populate(s)
    return s


def _run(store: Any, *, dry_run: bool = False, now: datetime = T1, **kwargs: Any) -> VoterGeoResult:
    result = run_voter_geo_build_on_store(store, ELECTION, dry_run=dry_run, now=now, **kwargs)
    assert result is not None
    return result


def test_every_voter_gets_the_place_its_first_listing_names(store: _Store) -> None:
    result = _run(store)
    assert result.status == "wrote"
    for vuid, pair in FIRST_PAIR.items():
        fips, pctkey, cd, sd, hd, split = EXPECTED_PLACE[pair]
        row = store.geo_row(vuid)
        assert row is not None, "a live VUID has no row"
        assert (
            row["county_fips"],
            row["pctkey"],
            row["cd"],
            row["sd"],
            row["hd"],
            row["split_flag"],
        ) == (fips, pctkey, cd, sd, hd, split)
        assert row["flags"] == EXPECTED_FLAGS.get(vuid, 0)
        assert row["matched_at"] == "2026-10-27T01:00:00Z"
    assert store.geo_row(_v(13)) is None
    assert store.rows("SELECT COUNT(*) FROM voter_geo") == [(len(FIRST_PAIR),)]


def test_counts_match_the_synthetic_geography(store: _Store) -> None:
    result = _run(store)
    assert result.live_rows == LIVE_ROWS
    assert result.vuids == len(FIRST_PAIR)
    assert result.vuid_normalized_changed == 0
    # Matched: voters 1, 2, 3, 7, 8, 9, 10, 11, 12. Unmatched alias: 4 and 5. No alias: 6.
    assert (result.matched_vuids, result.unmatched_alias_vuids, result.no_alias_vuids) == (9, 2, 1)
    assert result.unknown_county_vuids == 1
    # Voter 7 is the only matched one with a NULL cd or hd; sd is set for every matched precinct.
    assert (result.null_cd_vuids, result.null_sd_vuids, result.null_hd_vuids) == (1, 0, 1)
    # BEXAR 7 splits cd and hd: voters 2, 11 and 12 and, by first listing, no one else.
    assert result.split_vuids == 3
    assert (result.multi_precinct_vuids, result.multi_county_vuids) == (4, 3)
    assert result.matched_rows == MATCHED_ROWS
    assert result.matched_row_share == pytest.approx(MATCHED_ROWS / LIVE_ROWS)
    assert result.rows_pending == len(FIRST_PAIR)
    assert result.rows_stale == 0
    assert result.queries > 0


def test_stored_matched_rows_agree_with_the_expected_pairs() -> None:
    expected = sum(
        1
        for pairs in [
            [("TRAVIS", "0101")],
            [("BEXAR", "7")],
            [("DE WITT", "7A")],
            [("TRAVIS", "ZZZ")],
            [("ATLANTIS", "1")],
            [("BEXAR", "99")],
            [("BEXAR", "0050")],
            [("TRAVIS", "0101"), ("BEXAR", "7")],
            [("TRAVIS", "0101"), ("TRAVIS", "ZZZ")],
            [("TRAVIS", "0101")] * 3,
            [("TRAVIS", "0101"), ("BEXAR", "7")],
            [("TRAVIS", "0101"), ("BEXAR", "7")],
        ]
        for pair in pairs
        if PAIR_PCTKEY.get(pair) is not None
    )
    assert expected == MATCHED_ROWS


def test_vuids_are_normalized_before_they_key_a_row() -> None:
    store = _Store()
    store.add(D1, "14", "TRAVIS", "0101")
    store.add(D2, _v(14), "TRAVIS", "0101")
    store.add(D1, " 0000000015 ", "TRAVIS", "0101")
    store.add(D1, "00000000016", "TRAVIS", "0101")
    store.add(D1, _v(17), "TRAVIS", "0101")
    result = _run(store)
    assert result.vuid_normalized_changed == 3
    assert result.vuids == 4
    stored = [v for (v,) in store.rows("SELECT vuid FROM voter_geo ORDER BY vuid")]
    assert stored == [_v(14), _v(15), _v(16), _v(17)]
    assert _placed(store, _v(14))["flags"] == 0


def test_a_vuid_that_is_not_ten_digits_fails_validation_without_naming_it() -> None:
    store = _Store()
    store.add(D1, "ABCDEFGHIJ", "TRAVIS", "0101")
    with pytest.raises(EarlyVoteSyncError) as excinfo:
        _run(store)
    message = str(excinfo.value)
    assert message.startswith("1 voter_geo rows failed validation; first: ")
    assert "ABCDEFGHIJ" not in message
    assert store.rows("SELECT COUNT(*) FROM voter_geo") == [(0,)]
    assert store.rows("SELECT COUNT(*) FROM voter_geo_meta") == [(0,)]


def test_meta_is_written_last_and_holds_the_counts(store: _Store) -> None:
    result = _run(store)
    kinds = [sql.split("(")[0].strip() for sql, _ in store.statements]
    assert kinds[-1].startswith("INSERT INTO voter_geo_meta")
    assert not any(k.startswith("INSERT INTO voter_geo_meta") for k in kinds[:-1])
    meta = dict(store.rows("SELECT name, value FROM voter_geo_meta"))
    assert meta["vuids"] == str(result.vuids)
    assert meta["live_rows"] == str(LIVE_ROWS)
    assert len(meta["signature"]) == 64


def test_a_second_apply_sends_no_statement_and_keeps_the_counts(store: _Store) -> None:
    first = _run(store)
    store.statements.clear()
    second = _run(store, now=T2)
    assert store.statements == []
    assert second.status == "unchanged"
    assert (second.vuids, second.live_rows, second.matched_vuids) == (
        first.vuids,
        first.live_rows,
        first.matched_vuids,
    )
    assert second.matched_row_share == pytest.approx(first.matched_row_share)
    assert second.statements == 0


def test_a_changed_roster_day_upserts_only_new_voters_and_deletes_gone_ones(
    store: _Store,
) -> None:
    _run(store)
    store.statements.clear()
    store.add(D3, _v(20), "BEXAR", "7")
    store.conn.execute(
        "UPDATE early_vote_roster_row SET removed_in = 2 WHERE id_voter = ?", (_v(1),)
    )
    store.bump(D3)
    result = _run(store, now=T2)
    assert result.status == "wrote"
    assert [row[0] for row in store.upserts()] == [_v(20)]
    assert (result.rows_pending, result.rows_stale) == (1, 1)
    assert store.geo_row(_v(1)) is None
    assert _placed(store, _v(20))["matched_at"] == "2026-10-27T02:00:00Z"
    assert _placed(store, _v(2))["matched_at"] == "2026-10-27T01:00:00Z"
    assert dict(store.rows("SELECT name, value FROM voter_geo_meta"))["vuids"] == str(
        len(FIRST_PAIR)
    )


def test_a_geography_change_rewrites_only_the_voters_it_moves(store: _Store) -> None:
    _run(store)
    store.statements.clear()
    store.conn.execute(
        "UPDATE geo_precinct_alias SET pctkey = '4530102', rule = 'exact' WHERE precinct = 'ZZZ'"
    )
    result = _run(store, now=T2)
    assert result.status == "wrote"
    assert [row[0] for row in store.upserts()] == [_v(4)]
    moved = store.geo_row(_v(4))
    assert moved is not None
    assert (moved["pctkey"], moved["cd"], moved["split_flag"]) == ("4530102", "10", SPLIT_SD)
    assert moved["matched_at"] == "2026-10-27T02:00:00Z"


def test_a_dry_run_on_a_fresh_database_creates_and_writes_nothing_and_reports() -> None:
    store = _Store(voter_geo_tables=False)
    _populate(store)
    result = _run(store, dry_run=True)
    assert result.status == "dry_run"
    assert store.schema_calls == 0
    assert store.statements == []
    assert store.rows("SELECT name FROM sqlite_master WHERE name LIKE 'voter_geo%'") == []
    assert result.vuids == len(FIRST_PAIR)
    assert result.rows_pending == len(FIRST_PAIR)
    assert result.matched_vuids == 9


def test_a_dry_run_predicts_the_statements_of_the_apply(store: _Store) -> None:
    dry = _run(store, dry_run=True, batch_rows=5)
    assert store.statements == []
    applied = _run(store, batch_rows=5)
    assert dry.statements == applied.statements == len(store.statements)


def test_a_dry_run_after_an_apply_writes_nothing_and_finds_nothing_pending(store: _Store) -> None:
    _run(store)
    sent = len(store.statements)
    result = _run(store, dry_run=True, now=T2)
    assert len(store.statements) == sent
    assert (result.rows_pending, result.rows_stale, result.statements) == (0, 0, 1)
    assert store.schema_calls == 1


def test_a_dry_run_without_loaded_geography_reports_zero_counts() -> None:
    store = _Store(voter_geo_tables=False, geography=False)
    store.add(D1, _v(1))
    result = _run(store, dry_run=True)
    assert result.status == "geo_not_loaded"
    assert (result.live_rows, result.vuids, result.rows_pending) == (0, 0, 0)
    assert store.schema_calls == 0
    assert store.statements == []
    assert output_lines(result)[0] == "status=geo_not_loaded"


def test_an_apply_without_loaded_geography_writes_nothing() -> None:
    store = _Store(voter_geo_tables=False, geography=False)
    store.add(D1, _v(1))
    with pytest.raises(EarlyVoteSyncError, match="geography is not loaded"):
        _run(store)
    assert store.statements == []


def test_no_database_means_nothing_to_do() -> None:
    class _NoDb(_Store):
        @override
        def _database_uuid_if_exists(self, election_id: str) -> str | None:
            return None

    assert run_voter_geo_build_on_store(_NoDb(), ELECTION, dry_run=True) is None


def test_writes_are_bounded_batches() -> None:
    store = _Store()
    for n in range(1, 41):
        store.add(D1, _v(n), "TRAVIS", "0101")
    _run(store, batch_rows=7)
    upserts = [p for s, p in store.statements if s.startswith("INSERT INTO voter_geo (")]
    assert len(upserts) == 6
    assert all(len(json.loads(p[0])) <= 7 for p in upserts)
    assert sum(len(json.loads(p[0])) for p in upserts) == 40
    assert all(len(p[0].encode()) <= 900_000 for _, p in store.statements)


def test_logs_and_output_carry_no_vuids_counties_or_precincts(
    store: _Store, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        result = _run(store)
        _run(store, dry_run=True)
    text = caplog.text + "\n".join(output_lines(result))
    assert text.count("voter_geo build") >= 2
    for secret in (*FIRST_PAIR, "TRAVIS", "BEXAR", "ATLANTIS", "DE WITT", "7A", "ZZZ"):
        assert secret not in text


def test_the_output_has_every_count_line_in_order(store: _Store) -> None:
    keys = [line.split("=")[0] for line in output_lines(_run(store))]
    assert keys == [
        "status",
        "live_rows",
        "vuids",
        "vuid_normalized_changed",
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
        "matched_row_share",
        "rows_pending",
        "rows_stale",
        "statements",
        "queries",
        "seconds",
    ]
    assert re.fullmatch(r"matched_row_share=\d\.\d{4}", output_lines(_run(store))[14])


# --- DDL drift and imports --------------------------------------------------------------------


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


@pytest.mark.parametrize(
    ("model", "constant"),
    [(VoterGeoRow, VOTER_GEO_TABLE_SQL), (VoterGeoMeta, VOTER_GEO_META_TABLE_SQL)],
)
def test_committed_voter_geo_ddl_matches_what_the_models_generate(
    model: Any, constant: str
) -> None:
    generated = str(
        CreateTable(model.__table__, if_not_exists=True).compile(dialect=sqlite.dialect())
    )
    assert _normalize(constant) == _normalize(generated)


def test_voter_geo_columns_match_the_model() -> None:
    columns = VoterGeoRow.__table__.columns.keys()  # ty: ignore[unresolved-attribute]  # reason: SQLModel sets it at class creation
    assert list(VOTER_GEO_COLUMNS) == list(columns)


def test_the_sync_schema_creates_the_voter_geo_tables() -> None:
    for sql in VOTER_GEO_SCHEMA_CREATES:
        assert sql in _SCHEMA_CREATES


_HEAVY = "('sqlmodel', 'sqlalchemy', 'duckdb', 'polars', 'geopandas', 'shapely', 'voterfile_audit_pipeline')"
_BUILD_HEAVY = "('duckdb', 'polars', 'geopandas', 'shapely', 'voterfile_audit_pipeline')"


def _imports_no(module: str, heavy: str) -> None:
    code = (
        f"import sys, {module};bad = [m for m in {heavy} if m in sys.modules];assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize(
    "module",
    ["texas_turnout_scraper.voter_geo_tables", "texas_turnout_scraper.early_vote_sync"],
)
def test_worker_side_modules_import_no_heavy_libraries(module: str) -> None:
    _imports_no(module, _HEAVY)


def test_the_build_imports_no_duckdb_or_geo_libraries() -> None:
    _imports_no("texas_turnout_scraper.voter_geo_build", _BUILD_HEAVY)


def test_model_validators_reject_malformed_rows() -> None:
    base: dict[str, Any] = {
        "vuid": _v(1),
        "county_fips": "453",
        "pctkey": "4530101",
        "cd": "37",
        "sd": None,
        "hd": None,
        "split_flag": 0,
        "flags": 0,
        "src_hash": 1,
        "matched_at": "2026-10-27T01:00:00Z",
    }
    VoterGeo.model_validate(base)
    for bad in (
        {"vuid": "123"},
        {"county_fips": "45"},
        {"split_flag": None},
        {"pctkey": None},
        {"flags": 4},
        {"split_flag": 8},
        {"matched_at": "yesterday"},
    ):
        with pytest.raises(ValidationError):
            VoterGeo.model_validate({**base, **bad})
