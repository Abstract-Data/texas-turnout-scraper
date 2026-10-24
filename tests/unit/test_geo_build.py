"""Geography build: points inside their shapes, largest-overlap districts, split flags, provenance.

Synthetic layers are written with pyshp in the same CRSs the real files use: the district and
precinct layers in the plans' Lambert projection, the county layer in geographic NAD83. Shapes
are drawn in EPSG:3083 metres around Austin and projected into each layer's CRS on write.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

shapefile = pytest.importorskip("shapefile")
shapely = pytest.importorskip("shapely")
pyproj = pytest.importorskip("pyproj")

from shapely.geometry import Point, Polygon, shape  # noqa: E402

from texas_turnout_scraper import geo_build  # noqa: E402
from texas_turnout_scraper.geo_build import (  # noqa: E402
    AREA_EPSG,
    Area,
    EXPECTED_COUNTS,
    GeoBuildError,
    GeoData,
    build_geo,
    main,
)

AUSTIN_LON, AUSTIN_LAT = -97.743, 30.267
PLAN_WKT = (
    'PROJCS["NAD_1983_Lambert_Conformal_Conic",GEOGCS["GCS_North_American_1983",'
    'DATUM["D_North_American_1983",SPHEROID["GRS_1980",6378137.0,298.257222101]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Lambert_Conformal_Conic"],PARAMETER["False_Easting",1000000.0],'
    'PARAMETER["False_Northing",1000000.0],PARAMETER["Central_Meridian",-100.0],'
    'PARAMETER["Standard_Parallel_1",27.41666666666667],'
    'PARAMETER["Standard_Parallel_2",34.91666666666666],'
    'PARAMETER["Latitude_Of_Origin",31.16666666666667],UNIT["Meter",1.0]]'
)
COUNTY_WKT = (
    'GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",'
    'SPHEROID["GRS_1980",6378137,298.257222101]],PRIMEM["Greenwich",0],'
    'UNIT["Degree",0.017453292519943295]]'
)
SMALL_COUNTS = {"county": 2, "cd": 2, "sd": 1, "hd": 1}
KM = 1000.0

Ring = Sequence[tuple[float, float]]


def _origin() -> tuple[float, float]:
    to_area = pyproj.Transformer.from_crs(4326, AREA_EPSG, always_xy=True)
    x, y = to_area.transform(AUSTIN_LON, AUSTIN_LAT)
    return float(x), float(y)


def _box(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    """A rectangle in km offsets from Austin, as EPSG:3083 metres."""
    ox, oy = _origin()
    return [
        (ox + x0 * KM, oy + y0 * KM),
        (ox + x0 * KM, oy + y1 * KM),
        (ox + x1 * KM, oy + y1 * KM),
        (ox + x1 * KM, oy + y0 * KM),
        (ox + x0 * KM, oy + y0 * KM),
    ]


def _c_shape() -> list[tuple[float, float]]:
    """A thick C whose centroid falls in its open middle, outside the shape."""
    ox, oy = _origin()
    pts = [(30, 0), (40, 0), (40, 2), (32, 2), (32, 8), (40, 8), (40, 10), (30, 10), (30, 0)]
    return [(ox + x * KM, oy + y * KM) for x, y in pts]


# Rings in EPSG:3083, keyed by what the tests assert about them.
DISTRICTS = {
    "cd": [(1, _box(0, 0, 10, 10)), (2, _box(10, 0, 20, 10))],
    "sd": [(1, _box(0, 0, 30, 10))],
    "hd": [(1, _box(0, 0, 20, 10))],
}
COUNTIES = [("453", "Travis", _box(-5, -5, 22, 12)), ("491", "Williamson", _c_shape())]
# (CNTY, PREC, ring). PCTKEY is str(CNTY) + PREC, as in the state file.
PRECINCTS = [
    (453, "0001", _box(1, 1, 4, 4)),  # inside cd 1: share 1, not split
    (453, "0002", _box(9, 1, 11, 2)),  # half in cd 1, half in cd 2: tie goes to cd 1
    (453, "0003", _box(9.8, 5, 19.8, 6)),  # 98% in cd 2: split
    (453, "0004", _box(9.95, 7, 19.95, 8)),  # 99.5% in cd 2: not split
    (453, "0005", _box(25, 1, 26, 2)),  # outside every cd and hd, inside sd 1
    (453, "0006", _box(9.85, 8.5, 19.85, 9)),  # 98.5% in cd 2: split
]


def _write(
    path: Path, wkt: str, fields: list[tuple[str, str, int]], rows: list[tuple[Any, ...]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    to_layer = pyproj.Transformer.from_crs(AREA_EPSG, pyproj.CRS.from_wkt(wkt), always_xy=True)
    with shapefile.Writer(str(path), shapeType=shapefile.POLYGON) as writer:
        for name, kind, size in fields:
            writer.field(name, kind, size)
        for *record, ring in rows:
            writer.record(*record)
            writer.poly([[to_layer.transform(x, y) for x, y in ring]])
    path.with_suffix(".prj").write_text(wkt)


def write_reference(root: Path) -> Path:
    tiger = root / "tiger"
    for kind, folder, plan in (
        ("cd", "congressional", "PLANC9001"),
        ("sd", "state_senate", "PLANS9002"),
        ("hd", "state_house", "PLANH9003"),
    ):
        rows = [(district, ring) for district, ring in DISTRICTS[kind]]
        _write(tiger / folder / f"{plan}.shp", PLAN_WKT, [("District", "N", 4)], rows)
    counties = [("48", fips, name, ring) for fips, name, ring in COUNTIES]
    counties.append(("06", "001", "Alameda", _box(100, 100, 101, 101)))
    _write(
        tiger / "county" / "tl_2024_us_county.shp",
        COUNTY_WKT,
        [("STATEFP", "C", 2), ("COUNTYFP", "C", 3), ("NAME", "C", 40)],
        counties,
    )
    precincts = root / "tx_election" / "precincts"
    _write(
        precincts / "Precincts26P.shp",
        PLAN_WKT,
        [("CNTY", "N", 3), ("PREC", "C", 6), ("PCTKEY", "C", 10)],
        [(cnty, prec, f"{cnty}{prec}", ring) for cnty, prec, ring in PRECINCTS],
    )
    (precincts / "current.json").write_text(
        json.dumps({"filename": "Precincts26P.shp", "year": 2026, "kind": "P"})
    )
    return root


@pytest.fixture
def reference(tmp_path: Path) -> Path:
    return write_reference(tmp_path / "reference")


def _build(reference: Path) -> GeoData:
    return build_geo(reference, pipeline_commit="a" * 40, expected=SMALL_COUNTS)


def _areas(data: GeoData) -> dict[tuple[str, str], dict[str, Any]]:
    return {(a["kind"], a["code"]): a for a in data.to_dict()["areas"]}


def _precincts(data: GeoData) -> dict[str, dict[str, Any]]:
    return {p["pctkey"]: p for p in data.to_dict()["precincts"]}


def test_every_area_point_lies_inside_its_shape_even_for_a_concave_county(
    reference: Path,
) -> None:
    data = _build(reference)
    to_area = pyproj.Transformer.from_crs(4326, AREA_EPSG, always_xy=True)
    rings: dict[tuple[str, str], Ring] = {
        **{("cd", str(d)): r for d, r in DISTRICTS["cd"]},
        **{("sd", str(d)): r for d, r in DISTRICTS["sd"]},
        **{("hd", str(d)): r for d, r in DISTRICTS["hd"]},
        **{("county", f): r for f, _, r in COUNTIES},
        **{("precinct", f"{c}{p}"): r for c, p, r in PRECINCTS},
    }
    areas = _areas(data)
    assert set(areas) == set(rings)
    for key, ring in rings.items():
        point = Point(to_area.transform(areas[key]["lon"], areas[key]["lat"]))
        assert Polygon(ring).contains(point), key
    c_shape = Polygon(_c_shape())
    assert not c_shape.contains(c_shape.centroid)


def test_points_are_wgs84_degrees_near_austin_rounded_to_six_places(reference: Path) -> None:
    travis = _areas(_build(reference))[("county", "453")]
    assert travis["name"] == "Travis"
    assert 30.2 < travis["lat"] < 30.4
    assert -97.8 < travis["lon"] < -97.5
    for value in (travis["lat"], travis["lon"]):
        assert round(value, 6) == value


def test_austin_round_trips_through_the_area_crs() -> None:
    to_area = pyproj.Transformer.from_crs(4326, AREA_EPSG, always_xy=True)
    back = pyproj.Transformer.from_crs(AREA_EPSG, 4326, always_xy=True)
    x, y = to_area.transform(AUSTIN_LON, AUSTIN_LAT)
    lon, lat = back.transform(x, y)
    assert abs(lon - AUSTIN_LON) < 1e-6
    assert abs(lat - AUSTIN_LAT) < 1e-6
    assert pyproj.CRS.from_epsg(AREA_EPSG).name == "NAD83 / Texas Centric Albers Equal Area"


GOLDEN_PRECINCTS = [
    {
        "pctkey": "4530001",
        "county_fips": "453",
        "prec": "0001",
        "cd": "1",
        "cd_share": 1.0,
        "split_cd": False,
        "sd": "1",
        "sd_share": 1.0,
        "split_sd": False,
        "hd": "1",
        "hd_share": 1.0,
        "split_hd": False,
    },
    {
        "pctkey": "4530002",
        "county_fips": "453",
        "prec": "0002",
        "cd": "1",
        "cd_share": 0.5,
        "split_cd": True,
        "sd": "1",
        "sd_share": 1.0,
        "split_sd": False,
        "hd": "1",
        "hd_share": 1.0,
        "split_hd": False,
    },
    {
        "pctkey": "4530003",
        "county_fips": "453",
        "prec": "0003",
        "cd": "2",
        "cd_share": 0.98,
        "split_cd": True,
        "sd": "1",
        "sd_share": 1.0,
        "split_sd": False,
        "hd": "1",
        "hd_share": 1.0,
        "split_hd": False,
    },
    {
        "pctkey": "4530004",
        "county_fips": "453",
        "prec": "0004",
        "cd": "2",
        "cd_share": 0.995,
        "split_cd": False,
        "sd": "1",
        "sd_share": 1.0,
        "split_sd": False,
        "hd": "1",
        "hd_share": 1.0,
        "split_hd": False,
    },
    {
        "pctkey": "4530005",
        "county_fips": "453",
        "prec": "0005",
        "cd": None,
        "cd_share": None,
        "split_cd": None,
        "sd": "1",
        "sd_share": 1.0,
        "split_sd": False,
        "hd": None,
        "hd_share": None,
        "split_hd": None,
    },
    {
        "pctkey": "4530006",
        "county_fips": "453",
        "prec": "0006",
        "cd": "2",
        "cd_share": 0.985,
        "split_cd": True,
        "sd": "1",
        "sd_share": 1.0,
        "split_sd": False,
        "hd": "1",
        "hd_share": 1.0,
        "split_hd": False,
    },
]


def test_district_assignment_split_flags_tie_break_and_no_overlap_are_pinned(
    reference: Path,
) -> None:
    assert _build(reference).to_dict()["precincts"] == GOLDEN_PRECINCTS


def test_area_codes_are_fips_and_plain_district_numbers(reference: Path) -> None:
    keys = set(_areas(_build(reference)))
    assert {code for kind, code in keys if kind == "county"} == {"453", "491"}
    assert {code for kind, code in keys if kind == "cd"} == {"1", "2"}


def test_codes_and_keys_are_strings_on_the_data_objects(reference: Path) -> None:
    data = _build(reference)
    assert all(isinstance(area.code, str) for area in data.areas)
    for precinct in data.precincts:
        assert isinstance(precinct.pctkey, str)
        assert isinstance(precinct.county_fips, str)
        assert isinstance(precinct.prec, str)
        for district in (precinct.cd, precinct.sd, precinct.hd):
            assert district is None or isinstance(district, str)
    assert data.to_dict()["precincts"] == GOLDEN_PRECINCTS


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_value_never_reaches_the_json(bad: float) -> None:
    data = GeoData(provenance={}, areas=(Area("county", "001", "X", bad, 0.0),), precincts=())
    with pytest.raises(ValueError, match="Out of range float values"):
        data.to_json()


def test_two_builds_are_byte_identical(reference: Path) -> None:
    assert _build(reference).to_json() == _build(reference).to_json()


def test_the_cli_writes_the_same_bytes_as_the_library(
    reference: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(geo_build, "EXPECTED_COUNTS", SMALL_COUNTS)
    out = tmp_path / "geo" / "tx_geo.json"
    argv = ["--reference-dir", str(reference), "--out", str(out), "--pipeline-commit", "a" * 40]
    assert main(argv) == 0
    assert out.read_bytes() == _build(reference).to_json()


def test_provenance_names_sources_hashes_method_and_crs(reference: Path) -> None:
    provenance = _build(reference).to_dict()["provenance"]
    assert provenance["pipeline_commit"] == "a" * 40
    assert provenance["plans"] == {"cd": "PLANC9001", "sd": "PLANS9002", "hd": "PLANH9003"}
    assert provenance["precinct_file"] == "Precincts26P.shp"
    assert (provenance["precinct_year"], provenance["precinct_kind"]) == (2026, "P")
    assert (provenance["area_epsg"], provenance["point_epsg"]) == (3083, 4326)
    assert "representative_point" in provenance["method"]
    assert set(provenance["libraries"]) >= {"shapely", "pyproj", "pyshp", "geos", "proj"}
    shp = "tx_election/precincts/Precincts26P.shp"
    expected = hashlib.sha256((reference / shp).read_bytes()).hexdigest()
    assert provenance["sources"][shp] == expected
    assert "tx_election/precincts/current.json" in provenance["sources"]
    assert not any("tl_2020_48_vtd20" in key for key in provenance["sources"])


def test_a_checksum_file_that_disagrees_stops_the_build(reference: Path) -> None:
    (reference / "tiger" / ".checksums.json").write_text(
        json.dumps({"files": {"congressional/PLANC9001.shp": "0" * 64}})
    )
    with pytest.raises(GeoBuildError, match=r"PLANC9001\.shp"):
        _build(reference)


def test_a_matching_checksum_file_is_accepted(reference: Path) -> None:
    shp = reference / "tiger" / "congressional" / "PLANC9001.shp"
    digest = hashlib.sha256(shp.read_bytes()).hexdigest()
    (reference / "tiger" / ".checksums.json").write_text(
        json.dumps({"files": {"congressional/PLANC9001.shp": digest}})
    )
    assert _build(reference).to_dict()["provenance"]["sources"]["tiger/congressional/PLANC9001.shp"]


@pytest.mark.parametrize("kind", ["county", "cd", "sd", "hd"])
def test_an_unexpected_feature_count_stops_the_build(reference: Path, kind: str) -> None:
    with pytest.raises(GeoBuildError, match=kind):
        build_geo(reference, pipeline_commit=None, expected={**SMALL_COUNTS, kind: 99})


def test_the_default_counts_are_the_enacted_plans() -> None:
    assert dict(EXPECTED_COUNTS) == {"county": 254, "cd": 38, "sd": 31, "hd": 150}


@pytest.mark.parametrize(
    "missing",
    [
        "tiger/state_house",
        "tiger/county",
        "tx_election/precincts/current.json",
        "tx_election/precincts/Precincts26P.dbf",
    ],
)
def test_a_missing_layer_stops_the_build(reference: Path, missing: str) -> None:
    target = reference / missing
    if target.is_dir():
        for child in target.iterdir():
            child.unlink()
        target.rmdir()
    else:
        target.unlink()
    with pytest.raises(GeoBuildError):
        _build(reference)


def test_two_plans_in_one_folder_is_ambiguous(reference: Path) -> None:
    folder = reference / "tiger" / "congressional"
    for path in list(folder.iterdir()):
        path.with_name(path.name.replace("PLANC9001", "PLANC9009")).write_bytes(path.read_bytes())
    with pytest.raises(GeoBuildError, match="congressional"):
        _build(reference)


def test_a_duplicate_precinct_key_stops_the_build(reference: Path) -> None:
    _write(
        reference / "tx_election" / "precincts" / "Precincts26P.shp",
        PLAN_WKT,
        [("CNTY", "N", 3), ("PREC", "C", 6), ("PCTKEY", "C", 10)],
        [(453, "0001", "4530001", PRECINCTS[0][2]), (453, "0001", "4530001", PRECINCTS[1][2])],
    )
    with pytest.raises(GeoBuildError, match="4530001"):
        _build(reference)


_REAL = os.environ.get("TX_REFERENCE_DIR", "")


@pytest.mark.skipif(
    not (_REAL and (Path(_REAL) / "tx_election" / "precincts" / "current.json").is_file()),
    reason="set TX_REFERENCE_DIR to the pipeline's data/reference to run against the real layers",
)
def test_the_real_layers_build_with_every_point_inside_its_polygon() -> None:
    reference = Path(_REAL)
    data = build_geo(reference, pipeline_commit=None)
    out = data.to_dict()
    counts: dict[str, int] = {}
    for area in out["areas"]:
        counts[area["kind"]] = counts.get(area["kind"], 0) + 1
    assert counts == {"county": 254, "cd": 38, "sd": 31, "hd": 150, "precinct": 9614}
    assert len(out["precincts"]) == 9614
    current = json.loads((reference / "tx_election/precincts/current.json").read_text())
    layers = {
        "cd": ("tiger/congressional", "District"),
        "sd": ("tiger/state_senate", "District"),
        "hd": ("tiger/state_house", "District"),
        "county": ("tiger/county", "COUNTYFP"),
        "precinct": ("tx_election/precincts/" + current["filename"], "PCTKEY"),
    }
    points = {(a["kind"], a["code"]): (a["lon"], a["lat"]) for a in out["areas"]}
    for kind, (where, field) in layers.items():
        path = reference / where
        shp = path if path.suffix == ".shp" else next(path.glob("*.shp"))
        crs = pyproj.CRS.from_wkt(shp.with_suffix(".prj").read_text())
        to_layer = pyproj.Transformer.from_crs(4326, crs, always_xy=True)
        with shapefile.Reader(str(shp)) as reader:
            for item in reader.iterShapeRecords():
                record = item.record.as_dict()
                if kind == "county" and record["STATEFP"] != "48":
                    continue
                raw = record[field]
                code = str(int(raw)) if kind in {"cd", "sd", "hd"} else str(raw)
                geom = shapely.make_valid(shape(item.shape.__geo_interface__))
                lon, lat = points[(kind, code)]
                assert geom.covers(Point(to_layer.transform(lon, lat))), (kind, code)
