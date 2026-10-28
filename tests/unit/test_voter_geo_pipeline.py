"""Stage 2 end to end on a synthetic county, through the real pipeline (skips where it is absent).

Needs Python 3.11 or later, polars, geopandas and the voterfile-audit-pipeline, so it runs in the
ephemeral environment and skips in CI:

    uv run --no-project --python 3.12 --with ".[loader,voterfile]" \\
        --with ./vendor/voterfile-audit-pipeline --with pytest --with hypothesis --with respx \\
        python -m pytest tests/unit/test_voter_geo_pipeline.py -q

Everything here is invented: made-up VUIDs, names, streets and square districts.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("voterfile_audit_pipeline")
pl = pytest.importorskip("polars")
gpd = pytest.importorskip("geopandas")
shapely_geometry = pytest.importorskip("shapely.geometry")

from tests.unit.test_voter_geo_build import D1, ELECTION, _Store, _v  # noqa: E402
from texas_turnout_scraper import voter_geo  # noqa: E402
from texas_turnout_scraper.voter_geo import (  # noqa: E402
    GEO_VOTERFILE_COLUMNS,
    output_lines,
    pipeline_locator,
    read_inputs,
    run_voter_geo_on_store,
)
from texas_turnout_scraper.voter_geo_build import run_voter_geo_build_on_store  # noqa: E402
from texas_turnout_scraper.voter_link import read_voterfile  # noqa: E402
from texas_turnout_scraper.voter_tables import HISTORY_COLUMNS  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 28, 1, 0, 0, tzinfo=timezone.utc)
# The real voterfile header: no RSTPRE, so a directional lives only inside RADDRESS.
HEADER = [
    "COUNTY", "PCT", "CDPLANC2333", "SD2022", "HD2022", "VUID", "LNAME", "FNAME", "MNAME",
    "FORMERNAME", "SFX", "SEX", "DOB", "EDR", "STATUS", "HISPANIC", "RADDRESS", "RHNUM", "RDESIG",
    "RSTNAME", "RSTTYPE", "RSTSFX", "RUNUM", "RUTYPE", "RCITY", "RZIP", "MADR1", "MCITY", "MST",
    "MZIP", *HISTORY_COLUMNS,
]  # fmt: skip
TRAVIS_FIPS = "48453"
# Square districts over lon -98..-97, lat 30..31: (name, west, south, east, north).
CD_SQUARES = [(7, -98.0, 30.0, -97.5, 31.0), (8, -97.5, 30.0, -97.0, 31.0)]
SD_SQUARES = [(14, -98.0, 30.0, -97.0, 30.5), (15, -98.0, 30.5, -97.0, 31.0)]
HD_SQUARES = [
    (101, -98.0, 30.0, -97.5, 30.5),
    (102, -98.0, 30.5, -97.5, 31.0),
    (103, -97.5, 30.0, -97.0, 30.5),
    (104, -97.5, 30.5, -97.0, 31.0),
]
# (vuid, county, address line, house number, street, type, stated cd, sd, hd, point lon, lat)
VOTERS = [
    (_v(1), "TRAVIS", "100 MAIN ST", "100", "MAIN", "ST", "7", "14", "101", -97.75, 30.25),
    (_v(2), "TRAVIS", "200 OAK DR", "200", "OAK", "DR", "7", "15", "104", -97.25, 30.75),
    # The directional is only in RADDRESS; the stated cd is blank.
    (_v(3), "TRAVIS", "300 N ELM LN", "300", "ELM", "LN", "", "14", "103", -97.25, 30.25),
    # No such address point.
    (_v(4), "TRAVIS", "999 NOWHERE RD", "999", "NOWHERE", "RD", "7", "14", "101", None, None),
    # A county with no address-point file.
    (_v(5), "BEXAR", "10 CEDAR ST", "10", "CEDAR", "ST", "7", "14", "101", None, None),
]
# Where each synthetic voter must land: cd, sd, hd.
EXPECTED = {_v(1): ("7", "14", "101"), _v(2): ("8", "15", "104"), _v(3): ("8", "14", "103")}


@dataclass(frozen=True)
class Synthetic:
    voterfile: Path
    points_dir: Path
    shapefile_dir: Path


def _row(**cells: str) -> dict[str, str]:
    row = dict.fromkeys(HEADER, "")
    row.update(cells)
    return row


def _voterfile_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for vuid, county, line, number, street, kind, cd, sd, hd, _lon, _lat in VOTERS:
        rows.append(
            _row(
                VUID=vuid, COUNTY=county, RADDRESS=line, RHNUM=number, RSTNAME=street,
                RSTTYPE=kind, CDPLANC2333=cd, SD2022=sd, HD2022=hd, LNAME=f"FAKELAST{vuid[-1]}",
                FNAME="FAKEFIRST", RCITY="FAKETOWN", RZIP="78700", STATUS="V",
            )
        )  # fmt: skip
    # The same VUID twice: the pipeline's dedup keeps the last row.
    rows.append(dict(rows[0]))
    # A voter nobody asked for must never be read.
    rows.append(_row(VUID="1000000099", COUNTY="TRAVIS", RADDRESS="500 MAIN ST", RHNUM="500"))
    return rows


def _write_squares(
    root: Path, layer: str, plan: str, squares: list[tuple[int, float, float, float, float]]
) -> None:
    folder = root / layer
    folder.mkdir(parents=True)
    frame = gpd.GeoDataFrame(
        {"District": [s[0] for s in squares]},
        geometry=[shapely_geometry.box(*s[1:]) for s in squares],
        crs="EPSG:4326",
    )
    frame.to_file(folder / f"{plan}.shp")


def build_synthetic(root: Path) -> Synthetic:
    """A voterfile with the real header, TxGIO-shaped parquet for one county and three plans."""
    voterfile = root / "voterfile.csv"
    with voterfile.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(_voterfile_rows())
    points_dir = root / "tx_address_points" / "parquet"
    points_dir.mkdir(parents=True)
    (root / "tx_address_points" / "manifest.json").write_text(
        json.dumps({"acquisition_date": "2026-03-18"})
    )
    located = [v for v in VOTERS if v[9] is not None]
    pl.DataFrame(
        {
            "Add_Number": [v[3] for v in located] + ["77"],
            "St_PreDir": ["N" if v[2].split()[1] == "N" else None for v in located] + [None],
            "St_PreTyp": [None] * (len(located) + 1),
            "St_Name": [v[4] for v in located] + ["ELSEWHERE"],
            "St_PosTyp": [v[5] for v in located] + ["ST"],
            "St_PosDir": [None] * (len(located) + 1),
            "lon": [v[9] for v in located] + [-97.9],
            "lat": [v[10] for v in located] + [30.9],
            "zip5": ["78700"] * (len(located) + 1),
        },
        schema={
            "Add_Number": pl.String, "St_PreDir": pl.String, "St_PreTyp": pl.String,
            "St_Name": pl.String, "St_PosTyp": pl.String, "St_PosDir": pl.String,
            "lon": pl.Float64, "lat": pl.Float64, "zip5": pl.String,
        },
    ).write_parquet(points_dir / f"{TRAVIS_FIPS}.parquet")  # fmt: skip
    shapefile_dir = root / "shapefiles"
    _write_squares(shapefile_dir, "congressional", "PLANC2333", CD_SQUARES)
    _write_squares(shapefile_dir, "state_senate", "PLANS2168", SD_SQUARES)
    _write_squares(shapefile_dir, "state_house", "PLANH2316", HD_SQUARES)
    return Synthetic(voterfile, points_dir, shapefile_dir)


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> Synthetic:
    return build_synthetic(tmp_path_factory.mktemp("synthetic"))


def _store() -> _Store:
    store = _Store()
    store.add(D1, _v(1), "TRAVIS", "0101")
    store.add(D1, _v(2), "TRAVIS", "0101")
    store.add(D1, _v(3), "BEXAR", "7")
    store.add(D1, _v(4), "TRAVIS", "ZZZ")
    store.add(D1, _v(5), "BEXAR", "7")
    run_voter_geo_build_on_store(store, ELECTION, dry_run=False)
    store.statements.clear()
    return store


def _run(store: _Store, synthetic: Synthetic, *, dry_run: bool = False):
    inputs = read_inputs(
        synthetic.voterfile,
        synthetic.points_dir,
        synthetic.shapefile_dir,
        ROOT / "vendor" / "voterfile-audit-pipeline",
    )
    locate = pipeline_locator(synthetic.voterfile, synthetic.points_dir, synthetic.shapefile_dir)
    result = run_voter_geo_on_store(store, ELECTION, locate, inputs, dry_run=dry_run, now=NOW)
    assert result is not None
    return result


def test_the_directional_in_raddress_is_recovered_and_the_columns_are_the_defaults(
    synthetic: Synthetic,
) -> None:
    rows = read_voterfile(synthetic.voterfile, {_v(1), _v(3)}, GEO_VOTERFILE_COLUMNS)
    frame = voter_geo._normalized_frame(pl, rows)
    print("frame columns:", sorted(frame.columns))
    for name in voter_geo.FRAME_COLUMNS:
        assert name in frame.columns
    by_vuid = {r["vuid"]: r for r in frame.iter_rows(named=True)}
    assert by_vuid[_v(3)]["street_pre_dir"] == "N"
    assert by_vuid[_v(1)]["street_pre_dir"] is None
    assert by_vuid[_v(3)]["cd"] is None  # a blank stated district reads as null


def test_each_synthetic_voter_lands_in_the_expected_district(synthetic: Synthetic) -> None:
    store = _store()
    result = _run(store, synthetic)
    rows = {r[0]: r for r in store.rows("SELECT vuid, lat, lon, cd, sd, hd FROM voter_geo_point")}
    assert set(rows) == set(EXPECTED)
    for vuid, (cd, sd, hd) in EXPECTED.items():
        assert rows[vuid][3:] == (cd, sd, hd)
    # The directional voter matched, so its point is the invented one, to 6 decimals.
    assert (rows[_v(3)][1], rows[_v(3)][2]) == (30.25, -97.25)
    assert _v(4) not in rows and _v(5) not in rows  # unknown address, county without a file
    assert "1000000099" not in rows
    assert result.status == "wrote"


def test_the_counts(synthetic: Synthetic) -> None:
    result = _run(_store(), synthetic, dry_run=True)
    assert result.status == "dry_run"
    assert result.targets == 5
    assert (
        result.voterfile_rows == 6
    )  # five voters plus the repeated one; the uninvited is not read
    assert result.ingest_dropped == 1
    assert (result.geocoded, result.not_geocoded) == (3, 2)
    assert result.counties_without_points == 1
    # Voter 5 has a precinct and no point; voter 4's roster precinct has no alias.
    assert (result.stated_precinct, result.not_located) == (1, 1)
    assert result.districts["cd"] == {
        "agree": 1,
        "disagree": 1,
        "underivable": 0,
        "stated_missing": 1,
    }
    assert result.districts["sd"] == {
        "agree": 3,
        "disagree": 0,
        "underivable": 0,
        "stated_missing": 0,
    }
    assert result.districts["hd"] == {
        "agree": 3,
        "disagree": 0,
        "underivable": 0,
        "stated_missing": 0,
    }
    # The invented precincts say cd 37/20, sd 14/26, hd 46/123.
    assert result.disagree_with_precinct == {"cd": 3, "sd": 2, "hd": 3}
    assert result.rows_pending == 3 and result.rows_stale == 0


def test_a_second_apply_writes_nothing_and_output_is_counts_only(
    synthetic: Synthetic, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store()
    first = _run(store, synthetic)
    sent = len(store.statements)
    second = _run(store, synthetic)
    assert len(store.statements) == sent
    assert second.status == "unchanged" and second.statements == 0
    text = "\n".join(output_lines(first)) + capsys.readouterr().out
    for secret in ("FAKELAST", "FAKEFIRST", "MAIN", "ELM", "OAK", "1000000", "30.25", "-97.25"):
        assert secret not in text


def test_the_census_geocoder_module_was_never_loaded(synthetic: Synthetic) -> None:
    _run(_store(), synthetic, dry_run=True)
    stub = sys.modules["voterfile_audit_pipeline.stages.geography.geocoding"]
    assert stub.TRIPWIRE is True
    assert not hasattr(stub, "_CENSUS_CIRCUIT")


def test_a_layer_that_is_not_the_adr_010_plan_stops_the_run_before_any_read(
    synthetic: Synthetic, tmp_path: Path
) -> None:
    shapefile_dir = tmp_path / "shapefiles"
    _write_squares(shapefile_dir, "congressional", "PLANC2193", CD_SQUARES)
    _write_squares(shapefile_dir, "state_senate", "PLANS2168", SD_SQUARES)
    _write_squares(shapefile_dir, "state_house", "PLANH2316", HD_SQUARES)
    with pytest.raises(voter_geo.EarlyVoteSyncError, match="PLANC2333"):
        read_inputs(
            synthetic.voterfile,
            synthetic.points_dir,
            shapefile_dir,
            ROOT / "vendor" / "voterfile-audit-pipeline",
        )
