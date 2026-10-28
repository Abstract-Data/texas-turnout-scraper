"""Stage 2 voter geography: residential points, diff-only writes, the table CHECKs (no pipeline)."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable
from typing_extensions import override

from tests.unit.test_voter_geo_build import D1, ELECTION, _populate, _Store, _v
from texas_turnout_scraper import voter_geo
from texas_turnout_scraper.early_vote_errors import EarlyVoteSyncError
from texas_turnout_scraper.early_vote_sync import _SCHEMA_CREATES
from texas_turnout_scraper.voter_geo import (
    Inputs,
    Located,
    PointFix,
    VoterGeoPointResult,
    check_plans,
    district_key,
    fold_agreements,
    output_lines,
    point_hash,
    point_record,
    read_vintage,
    run_voter_geo_on_store,
)
from texas_turnout_scraper.voter_geo_build import run_voter_geo_build_on_store
from texas_turnout_scraper.voter_geo_models import VoterGeoPointMeta, VoterGeoPointRow
from texas_turnout_scraper.voter_geo_tables import (
    VOTER_GEO_POINT_COLUMNS,
    VOTER_GEO_POINT_META_TABLE_SQL,
    VOTER_GEO_POINT_TABLE_SQL,
    VOTER_GEO_SCHEMA_CREATES,
    VOTER_GEO_TABLE_COUNT,
)

T1 = datetime(2026, 10, 28, 1, 0, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 10, 28, 2, 0, 0, tzinfo=timezone.utc)
VINTAGE = "2026-03-18"
INPUTS = Inputs(
    vintage=VINTAGE,
    plans={"congressional": "PLANC2333", "state_senate": "PLANS2168", "state_house": "PLANH2316"},
    pipeline_rev="0" * 40,
    voterfile_name="synthetic.csv",
    voterfile_size=123,
)
HEAVY = "('polars', 'geopandas', 'duckdb', 'shapely', 'voterfile_audit_pipeline')"

# Synthetic fixes: voter 1 agrees with its precinct on cd and hd and not on sd; voter 2 has no hd
# under the point; voter 4 is geocoded though its roster precinct has no alias.
FIX_1 = PointFix(30.100001, -97.200002, "37", "15", "46")
FIX_2 = PointFix(29.500003, -98.400004, "20", "26", None)
FIX_4 = PointFix(30.700005, -97.600006, "10", "14", "47")


def _located(
    fixes: dict[str, PointFix],
    *,
    counties_without_points: int = 0,
    districts: Mapping[str, Mapping[str, int]] | None = None,
) -> Located:
    return Located(
        fixes,
        voterfile_rows=len(fixes),
        counties_without_points=counties_without_points,
        districts=districts or {},
    )


def _fixes() -> dict[str, PointFix]:
    return {_v(1): FIX_1, _v(2): FIX_2, _v(4): FIX_4}


def _stage_two(
    store: Any,
    located: Located,
    *,
    dry_run: bool = False,
    now: datetime = T1,
    **kwargs: Any,
) -> VoterGeoPointResult:
    result = run_voter_geo_on_store(
        store, ELECTION, lambda targets: located, INPUTS, dry_run=dry_run, now=now, **kwargs
    )
    assert result is not None
    return result


@pytest.fixture
def store() -> _Store:
    s = _Store()
    _populate(s)
    # Stage 1 has already placed every voter by precinct.
    assert run_voter_geo_build_on_store(s, ELECTION, dry_run=False) is not None
    s.statements.clear()
    return s


def _point_rows(store: _Store) -> dict[str, tuple[Any, ...]]:
    rows = store.rows("SELECT * FROM voter_geo_point ORDER BY vuid")
    return {row[0]: row for row in rows}


def _point_statements(store: _Store) -> list[tuple[str, list[str]]]:
    return [(sql, params) for sql, params in store.statements if "voter_geo_point" in sql]


# --- Table, DDL, CHECKs -----------------------------------------------------------------------


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


@pytest.mark.parametrize(
    ("model", "constant"),
    [
        (VoterGeoPointRow, VOTER_GEO_POINT_TABLE_SQL),
        (VoterGeoPointMeta, VOTER_GEO_POINT_META_TABLE_SQL),
    ],
)
def test_committed_point_ddl_matches_what_the_models_generate(model: Any, constant: str) -> None:
    generated = str(
        CreateTable(model.__table__, if_not_exists=True).compile(dialect=sqlite.dialect())
    )
    assert _normalize(constant) == _normalize(generated)


def test_point_columns_match_the_model() -> None:
    columns = VoterGeoPointRow.__table__.columns.keys()  # ty: ignore[unresolved-attribute]  # reason: SQLModel sets it at class creation
    assert list(VOTER_GEO_POINT_COLUMNS) == list(columns)


def test_the_sync_schema_creates_all_four_tables() -> None:
    assert len(VOTER_GEO_SCHEMA_CREATES) == VOTER_GEO_TABLE_COUNT == 4
    for sql in VOTER_GEO_SCHEMA_CREATES:
        assert sql in _SCHEMA_CREATES


def _insert(conn: sqlite3.Connection, **cells: Any) -> None:
    row: dict[str, Any] = {
        "vuid": _v(1),
        "lat": 30.0,
        "lon": -97.0,
        "cd": "1",
        "sd": "2",
        "hd": "3",
        "geocode_source": "txgio_address_point",
        "geocode_quality": "address_point",
        "address_points_vintage": VINTAGE,
        "src_hash": 1,
        "matched_at": "2026-10-28T01:00:00Z",
    }
    row.update(cells)
    names = ", ".join(row)
    conn.execute(
        f"INSERT INTO voter_geo_point ({names}) VALUES ({', '.join('?' * len(row))})",
        [*row.values()],
    )


def test_sqlite_accepts_a_whole_point_and_rejects_each_row_that_breaks_a_check() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(VOTER_GEO_POINT_TABLE_SQL)
    _insert(conn)
    assert conn.execute("SELECT COUNT(*) FROM voter_geo_point").fetchone() == (1,)
    violations: dict[str, dict[str, Any]] = {
        # lat without lon breaks only the iff CHECK
        "lat_without_lon": {"vuid": _v(2), "lon": None},
        "lon_without_lat": {"vuid": _v(3), "lat": None},
        "address_point_without_point": {"vuid": _v(4), "lat": None, "lon": None},
        # a different quality breaks the quality CHECK (and the iff CHECK while a point is set)
        "other_quality_no_point": {
            "vuid": _v(5),
            "lat": None,
            "lon": None,
            "geocode_quality": "stated_precinct",
        },
        "other_quality_with_point": {"vuid": _v(6), "geocode_quality": "not_located"},
        # a different source breaks only the source CHECK
        "other_source": {"vuid": _v(7), "geocode_source": "census"},
    }
    for name, cells in violations.items():
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert(conn, **cells)
        assert name
    assert conn.execute("SELECT COUNT(*) FROM voter_geo_point").fetchone() == (1,)


def test_the_point_table_has_no_rowid_and_keys_on_the_vuid() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(VOTER_GEO_POINT_TABLE_SQL)
    _insert(conn)
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE|PRIMARY"):
        _insert(conn)
    with pytest.raises(sqlite3.OperationalError, match="rowid"):
        conn.execute("SELECT rowid FROM voter_geo_point")


# --- Pure row building ------------------------------------------------------------------------


def test_the_hash_covers_every_stored_column_but_matched_at() -> None:
    base = point_hash(_v(1), FIX_1, VINTAGE)
    assert base == point_hash(_v(1), FIX_1, VINTAGE)
    changed = [
        point_hash(_v(2), FIX_1, VINTAGE),
        point_hash(_v(1), FIX_1._replace(lat=FIX_1.lat + 0.00001), VINTAGE),
        point_hash(_v(1), FIX_1._replace(lon=FIX_1.lon + 0.00001), VINTAGE),
        point_hash(_v(1), FIX_1._replace(cd="38"), VINTAGE),
        point_hash(_v(1), FIX_1._replace(sd=None), VINTAGE),
        point_hash(_v(1), FIX_1._replace(hd="47"), VINTAGE),
        point_hash(_v(1), FIX_1, "2026-06-01"),
    ]
    assert len({base, *changed}) == len(changed) + 1
    # Sub-6-decimal noise does not change a row.
    assert base == point_hash(_v(1), FIX_1._replace(lat=FIX_1.lat + 1e-9), VINTAGE)


def test_a_record_rounds_to_six_decimals_and_always_has_a_point() -> None:
    record = point_record(
        _v(1),
        PointFix(30.12345678, -97.87654321, None, "2", None),
        VINTAGE,
        5,
        "2026-10-28T01:00:00Z",
    )
    assert (record.lat, record.lon) == (30.123457, -97.876543)
    assert (record.geocode_source, record.geocode_quality) == (
        "txgio_address_point",
        "address_point",
    )
    assert record.cd is None and record.sd == "2" and record.hd is None


@pytest.mark.parametrize(
    "bad",
    [
        {"vuid": "123"},
        {"lat": 95.0},
        {"lat": float("nan")},
        {"lon": -190.0},
    ],
)
def test_a_malformed_row_fails_naming_fields_not_values(bad: dict[str, Any]) -> None:
    fields = {"vuid": _v(1), "lat": 30.0, "lon": -97.0}
    fields.update(bad)
    fix = PointFix(fields["lat"], fields["lon"], "1", "2", "3")
    with pytest.raises(EarlyVoteSyncError, match="failed validation") as caught:
        point_record(fields["vuid"], fix, VINTAGE, 1, "2026-10-28T01:00:00Z")
    message = str(caught.value)
    assert fields["vuid"] not in message and "30.0" not in message and "95.0" not in message


def test_agreement_outcomes_fold_into_four_buckets() -> None:
    folded = fold_agreements(
        {
            "cd": {
                "AGREES": 5,
                "DISAGREES": 2,
                "UNDERIVABLE": 1,
                "STATED_NOT_RECORDED": 3,
                "ASSIGNED_NO_STATED": 4,
            }
        }
    )
    assert folded == {"cd": {"agree": 5, "disagree": 2, "underivable": 1, "stated_missing": 7}}


def test_district_codes_compare_without_leading_zeros() -> None:
    assert district_key("037") == district_key("37") == district_key(" 37 ") == "37"
    assert district_key("7") != district_key("17")


# --- The run against a store ------------------------------------------------------------------


def test_an_apply_writes_one_row_per_geocoded_voter_and_counts_the_rest(store: _Store) -> None:
    result = _stage_two(store, _located(_fixes(), counties_without_points=2))
    assert result.status == "wrote"
    rows = _point_rows(store)
    assert set(rows) == {_v(1), _v(2), _v(4)}
    lat, lon = rows[_v(1)][1], rows[_v(1)][2]
    assert (lat, lon) == (30.100001, -97.200002)
    assert rows[_v(2)][3:6] == ("20", "26", None)
    for row in rows.values():
        assert row[6:9] == ("txgio_address_point", "address_point", VINTAGE)
        assert row[10] == "2026-10-28T01:00:00Z"
    # Twelve live roster voters. Three have a point; seven of the other nine have a precinct.
    assert (result.targets, result.geocoded) == (12, 3)
    assert (result.stated_precinct, result.not_located) == (7, 2)
    assert result.counties_without_points == 2
    assert (result.rows_pending, result.rows_stale) == (3, 0)
    # Voter 1's point sits in a different state senate district than its precinct; voter 4's
    # roster precinct has no alias, so nothing to compare.
    assert result.disagree_with_precinct == {"cd": 0, "sd": 1, "hd": 0}
    meta = dict(store.rows("SELECT name, value FROM voter_geo_point_meta"))
    assert tuple(meta) == tuple(sorted(meta))
    assert set(meta) == {
        "signature",
        "address_points_vintage",
        "plans",
        "pipeline_rev",
        "voterfile",
        "counts",
    }
    assert meta["address_points_vintage"] == VINTAGE
    assert json.loads(meta["voterfile"]) == {"name": "synthetic.csv", "size": 123}
    assert json.loads(meta["counts"])["geocoded"] == 3


def test_every_stored_point_satisfies_the_invariant(store: _Store) -> None:
    _stage_two(store, _located(_fixes()))
    bad = store.rows(
        "SELECT COUNT(*) FROM voter_geo_point WHERE lat IS NULL OR lon IS NULL "
        "OR geocode_quality <> 'address_point' OR geocode_source <> 'txgio_address_point'"
    )
    assert bad == [(0,)]


def test_meta_is_written_last(store: _Store) -> None:
    _stage_two(store, _located(_fixes()))
    kinds = [sql.split(" (")[0] for sql, _ in _point_statements(store)]
    assert kinds[-1] == "INSERT INTO voter_geo_point_meta"
    assert "INSERT INTO voter_geo_point_meta" not in kinds[:-1]


def test_a_second_apply_writes_nothing(store: _Store) -> None:
    first = _stage_two(store, _located(_fixes()))
    sent = len(store.statements)
    signature = dict(store.rows("SELECT name, value FROM voter_geo_point_meta"))["signature"]
    second = _stage_two(store, _located(_fixes()), now=T2)
    assert len(store.statements) == sent
    assert second.status == "unchanged"
    assert (second.rows_pending, second.rows_stale, second.statements) == (0, 0, 0)
    assert second.geocoded == first.geocoded
    assert (
        dict(store.rows("SELECT name, value FROM voter_geo_point_meta"))["signature"] == signature
    )
    # matched_at is stamped only on rows a run writes.
    assert {row[10] for row in _point_rows(store).values()} == {"2026-10-28T01:00:00Z"}


def test_a_moved_point_rewrites_only_that_row_and_changes_the_signature(store: _Store) -> None:
    _stage_two(store, _located(_fixes()))
    signature = dict(store.rows("SELECT name, value FROM voter_geo_point_meta"))["signature"]
    store.statements.clear()
    moved = {**_fixes(), _v(2): FIX_2._replace(cd="21")}
    result = _stage_two(store, _located(moved), now=T2)
    assert result.status == "wrote" and result.rows_pending == 1
    upserts = [
        row
        for sql, params in store.statements
        if sql.startswith("INSERT INTO voter_geo_point (")
        for row in json.loads(params[0])
    ]
    assert [row[0] for row in upserts] == [_v(2)]
    rows = _point_rows(store)
    assert rows[_v(2)][3] == "21" and rows[_v(2)][10] == "2026-10-28T02:00:00Z"
    assert rows[_v(1)][10] == "2026-10-28T01:00:00Z"
    assert (
        dict(store.rows("SELECT name, value FROM voter_geo_point_meta"))["signature"] != signature
    )


def test_a_new_vintage_rewrites_every_row(store: _Store) -> None:
    _stage_two(store, _located(_fixes()))
    store.statements.clear()
    newer = replace(INPUTS, vintage="2026-06-01")
    result = run_voter_geo_on_store(
        store, ELECTION, lambda targets: _located(_fixes()), newer, dry_run=False, now=T2
    )
    assert result is not None and result.rows_pending == 3
    assert {row[8] for row in _point_rows(store).values()} == {"2026-06-01"}


def test_voters_who_stop_geocoding_or_stop_being_targets_are_deleted(store: _Store) -> None:
    _stage_two(store, _located(_fixes()))
    store.statements.clear()
    # Voter 4 no longer geocodes; voter 2 leaves the roster.
    store.conn.execute(
        "UPDATE early_vote_roster_row SET removed_in = 2 WHERE id_voter = ?", (_v(2),)
    )
    result = _stage_two(store, _located({_v(1): FIX_1}), now=T2)
    assert result.rows_stale == 2 and result.rows_pending == 0
    assert set(_point_rows(store)) == {_v(1)}
    deletes = [p for s, p in store.statements if s.startswith("DELETE FROM voter_geo_point ")]
    assert sorted(json.loads(deletes[0][0])) == [_v(2), _v(4)]


def test_an_apply_that_geocodes_nobody_refuses_to_wipe_the_table(store: _Store) -> None:
    _stage_two(store, _located(_fixes()))
    store.statements.clear()
    with pytest.raises(EarlyVoteSyncError, match="refusing to delete"):
        _stage_two(store, _located({}), now=T2)
    assert store.statements == []
    assert len(_point_rows(store)) == 3


def test_a_dry_run_on_a_database_without_point_tables_creates_and_writes_nothing() -> None:
    s = _Store()
    _populate(s)
    run_voter_geo_build_on_store(s, ELECTION, dry_run=False)
    for table in ("voter_geo_point", "voter_geo_point_meta"):
        s.conn.execute(f"DROP TABLE {table}")
    schema_calls, sent = s.schema_calls, len(s.statements)
    result = _stage_two(s, _located(_fixes()), dry_run=True)
    assert result.status == "dry_run"
    assert s.schema_calls == schema_calls and len(s.statements) == sent
    assert s.rows("SELECT name FROM sqlite_master WHERE name LIKE 'voter_geo_point%'") == []
    assert (result.rows_pending, result.stated_precinct, result.not_located) == (3, 7, 2)


def test_a_dry_run_predicts_the_apply_and_never_writes(store: _Store) -> None:
    dry = _stage_two(store, _located(_fixes()), dry_run=True, batch_rows=2)
    assert store.statements == []
    assert store.schema_calls == 1  # only the Stage 1 apply of the fixture
    applied = _stage_two(store, _located(_fixes()), batch_rows=2)
    assert dry.statements == applied.statements == len(store.statements)
    assert dry.rows_pending == applied.rows_pending


def test_a_dry_run_after_an_apply_finds_nothing_pending(store: _Store) -> None:
    _stage_two(store, _located(_fixes()))
    sent = len(store.statements)
    result = _stage_two(store, _located(_fixes()), dry_run=True, now=T2)
    assert len(store.statements) == sent
    assert (result.rows_pending, result.rows_stale, result.statements) == (0, 0, 0)


def test_stage_1_still_diffs_when_the_point_tables_do_not_exist_yet() -> None:
    s = _Store()
    _populate(s)
    first = run_voter_geo_build_on_store(s, ELECTION, dry_run=False)
    assert first is not None and first.rows_pending > 0
    for table in ("voter_geo_point", "voter_geo_point_meta"):
        s.conn.execute(f"DROP TABLE {table}")
    again = run_voter_geo_build_on_store(s, ELECTION, dry_run=True)
    assert again is not None and again.rows_pending == 0


def test_writes_are_bounded_batches() -> None:
    s = _Store()
    fixes = {}
    for n in range(1, 41):
        s.add(D1, _v(n), "TRAVIS", "0101")
        fixes[_v(n)] = PointFix(30.0 + n / 1000, -97.0 - n / 1000, "1", "2", "3")
    _stage_two(s, _located(fixes), batch_rows=7)
    upserts = [p for sql, p in s.statements if sql.startswith("INSERT INTO voter_geo_point (")]
    assert len(upserts) == 6
    assert all(len(json.loads(p[0])) <= 7 for p in upserts)
    assert sum(len(json.loads(p[0])) for p in upserts) == 40
    assert all(len(p[0].encode()) <= 900_000 for _, p in s.statements)


def test_no_database_means_nothing_to_do() -> None:
    class _NoDb(_Store):
        @override
        def _database_uuid_if_exists(self, election_id: str) -> str | None:
            return None

    called = []
    result = run_voter_geo_on_store(
        _NoDb(), ELECTION, lambda t: called.append(t) or _located({}), INPUTS, dry_run=True
    )
    assert result is None and called == []


def test_logs_output_and_errors_carry_no_vuids_or_coordinates(
    store: _Store, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        result = _stage_two(store, _located(_fixes()))
        _stage_two(store, _located(_fixes()), dry_run=True, now=T2)
    text = caplog.text + "\n".join(output_lines(result))
    assert text.count("voter_geo point") >= 2
    for secret in (*_fixes(), "30.100001", "-97.200002", "29.5", "TRAVIS", "BEXAR"):
        assert secret not in text


def test_the_output_has_every_count_line_in_order(store: _Store) -> None:
    located = _located(
        _fixes(),
        districts={layer: dict.fromkeys(voter_geo.BUCKETS, 1) for layer in ("cd", "sd", "hd")},
    )
    lines = output_lines(_stage_two(store, located))
    keys = [line.split("=")[0] for line in lines]
    assert keys == [
        "status",
        "targets",
        "voterfile_rows",
        "ingest_dropped",
        "geocoded",
        "not_geocoded",
        "counties_without_points",
        "stated_precinct",
        "not_located",
        *(f"{layer}_{bucket}" for layer in ("cd", "sd", "hd") for bucket in voter_geo.BUCKETS),
        "disagree_with_precinct_cd",
        "disagree_with_precinct_sd",
        "disagree_with_precinct_hd",
        "rows_pending",
        "rows_stale",
        "statements",
        "seconds",
    ]
    assert all(re.fullmatch(r"[a-z_]+=[a-z0-9_.]+", line) for line in lines)


# --- Local inputs -----------------------------------------------------------------------------


def test_the_vintage_is_the_manifests_acquisition_date(tmp_path: Path) -> None:
    root = tmp_path / "tx_address_points"
    (root / "parquet").mkdir(parents=True)
    with pytest.raises(EarlyVoteSyncError, match="no address-point manifest"):
        read_vintage(root / "parquet")
    (root / "manifest.json").write_text(json.dumps({"acquisition_date": "2026-03-18"}))
    assert read_vintage(root / "parquet") == "2026-03-18"
    assert read_vintage(root) == "2026-03-18"
    (root / "manifest.json").write_text(json.dumps({"acquisition_date": "March"}))
    with pytest.raises(EarlyVoteSyncError, match="acquisition_date"):
        read_vintage(root)
    (root / "manifest.json").write_text("[]")
    with pytest.raises(EarlyVoteSyncError, match="JSON object"):
        read_vintage(root)


def _plans(root: Path, names: dict[str, str]) -> None:
    for layer, stem in names.items():
        (root / layer).mkdir(parents=True, exist_ok=True)
        (root / layer / f"{stem}.shp").write_bytes(b"")


def test_the_plan_gate_wants_exactly_the_adr_010_plans(tmp_path: Path) -> None:
    good = {"congressional": "PLANC2333", "state_senate": "PLANS2168", "state_house": "PLANH2316"}
    _plans(tmp_path, good)
    assert check_plans(tmp_path) == good
    _plans(tmp_path, {"state_house": "PLANH2100"})
    with pytest.raises(EarlyVoteSyncError, match="PLANH2316"):
        check_plans(tmp_path)
    (tmp_path / "state_house" / "PLANH2100.shp").unlink()
    assert check_plans(tmp_path) == good
    (tmp_path / "state_house" / "PLANH2316b.shp").write_bytes(b"")
    with pytest.raises(EarlyVoteSyncError, match="exactly one"):
        check_plans(tmp_path)


def test_the_census_geocoder_is_replaced_by_a_tripwire(monkeypatch: pytest.MonkeyPatch) -> None:
    name = "voterfile_audit_pipeline.stages.geography.geocoding"
    monkeypatch.delitem(sys.modules, name, raising=False)
    voter_geo._block_census_geocoder()
    stub = sys.modules[name]
    assert stub.TRIPWIRE is True
    with pytest.raises(EarlyVoteSyncError, match="blocked"):
        stub.geocode_batch(object())
    voter_geo._block_census_geocoder()  # idempotent
    monkeypatch.setitem(sys.modules, name, type(sys)("real"))
    with pytest.raises(EarlyVoteSyncError, match="already loaded"):
        voter_geo._block_census_geocoder()


# --- Imports ----------------------------------------------------------------------------------


def _imports_no(module: str, heavy: str) -> None:
    code = (
        f"import sys, {module};bad = [m for m in {heavy} if m in sys.modules];assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_voter_geo_imports_no_geometry_libraries_duckdb_or_the_pipeline() -> None:
    _imports_no("texas_turnout_scraper.voter_geo", HEAVY)


@pytest.mark.parametrize(
    "module", ["texas_turnout_scraper.voter_geo_tables", "texas_turnout_scraper.early_vote_sync"]
)
def test_worker_side_modules_still_load_no_sqlmodel(module: str) -> None:
    _imports_no(module, "('sqlmodel', 'sqlalchemy', 'duckdb', 'polars', 'geopandas', 'shapely')")
