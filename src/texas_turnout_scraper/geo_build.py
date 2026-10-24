"""Offline build of the Texas geography JSON from the state's map layers.

Needs the ``geo`` extra (shapely, pyproj, pyshp). Nothing the loader, ``early_vote_sync`` or the
Worker imports may import this module. Run it when a plan or the precinct layer changes and
commit the output; ``geo_load`` loads that file into D1. See docs/adr/010-geography-tables.md.

Every place gets a point from ``representative_point()`` of its shape in EPSG:3083, so the point
lies inside the shape (a centroid can fall outside a concave district). Each precinct takes, per
plan, the district it overlaps most by area in EPSG:3083.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

try:
    import numpy as np
    import pyproj
    import shapefile
    import shapely
    from shapely.geometry import shape
    from shapely.geometry.base import BaseGeometry
except ImportError as error:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "texas_turnout_scraper.geo_build needs the geo extra: uv sync --extra geo"
    ) from error

logger = logging.getLogger(__name__)

AREA_EPSG = 3083  # NAD83 / Texas Centric Albers Equal Area: all area math happens here.
POINT_EPSG = 4326
POINT_DECIMALS = 6
SHARE_DECIMALS = 4
SPLIT_BELOW = 0.99
# Overlap areas this close to the largest count as a tie, which goes to the lower district.
TIE_TOLERANCE = 1e-9
METHOD = (
    "points: shapely representative_point() of each (multi)polygon in EPSG:3083, transformed to "
    "EPSG:4326 and rounded to 6 decimals; districts: per plan, the district with the largest "
    "intersection area with the precinct in EPSG:3083 (ties to the lower number), share rounded "
    "to 4 decimals, split when share < 0.99, null when nothing overlaps"
)

EXPECTED_COUNTS: Mapping[str, int] = {"county": 254, "cd": 38, "sd": 31, "hd": 150}
TEXAS_STATEFP = "48"
DISTRICT_FOLDERS: Mapping[str, str] = {
    "cd": "tiger/congressional",
    "sd": "tiger/state_senate",
    "hd": "tiger/state_house",
}
DISTRICT_LABELS: Mapping[str, str] = {
    "cd": "Congressional District",
    "sd": "State Senate District",
    "hd": "State House District",
}
COUNTY_FOLDER = "tiger/county"
PRECINCT_FOLDER = "tx_election/precincts"
CURRENT_PRECINCTS = f"{PRECINCT_FOLDER}/current.json"
# Each pipeline dataset's setup writes sha256 per file, relative to its own folder.
CHECKSUM_FILES = ("tiger/.checksums.json", "tx_election/.checksums.json")
REQUIRED_PARTS = (".shp", ".shx", ".dbf", ".prj")
OPTIONAL_PARTS = (".cpg",)

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_SUBMODULE = "vendor/voterfile-audit-pipeline"
DEFAULT_REFERENCE_DIR = REPO_ROOT / PIPELINE_SUBMODULE / "data" / "reference"
DEFAULT_OUT = Path("data/geo/tx_geo.json")


class GeoBuildError(RuntimeError):
    """A layer is missing, ambiguous, or not what the build expects. Nothing was written."""


@dataclass(frozen=True)
class Area:
    kind: str
    code: str
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class PrecinctDistricts:
    pctkey: str
    county_fips: str
    prec: str
    cd: str | None
    cd_share: float | None
    split_cd: bool | None
    sd: str | None
    sd_share: float | None
    split_sd: bool | None
    hd: str | None
    hd_share: float | None
    split_hd: bool | None


@dataclass(frozen=True)
class GeoData:
    provenance: dict[str, Any]
    areas: tuple[Area, ...]
    precincts: tuple[PrecinctDistricts, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provenance": self.provenance,
            "areas": [asdict(area) for area in self.areas],
            "precincts": [asdict(precinct) for precinct in self.precincts],
        }

    def to_json(self) -> bytes:
        """Sorted keys, one record per line, so a rebuild diffs by place."""

        def dump(value: object) -> str:
            return json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
            )

        data = self.to_dict()
        lines = ["{", f'"provenance":{dump(data["provenance"])},']
        for key, last in (("areas", False), ("precincts", True)):
            lines.append(f'"{key}":[')
            lines.append(",\n".join(dump(row) for row in data[key]))
            lines.append("]" if last else "],")
        lines.append("}")
        return ("\n".join(lines) + "\n").encode("ascii")


@dataclass(frozen=True)
class _Feature:
    record: dict[str, Any]
    geom: BaseGeometry


def _layer_parts(reference_dir: Path, shp: Path) -> list[Path]:
    parts = []
    for suffix in REQUIRED_PARTS:
        part = shp.with_suffix(suffix)
        if not part.is_file():
            raise GeoBuildError(f"layer file missing: {part.relative_to(reference_dir)}")
        parts.append(part)
    parts.extend(p for p in (shp.with_suffix(s) for s in OPTIONAL_PARTS) if p.is_file())
    return parts


def _only_shapefile(reference_dir: Path, folder: str) -> Path:
    found = sorted((reference_dir / folder).glob("*.shp"))
    if len(found) != 1:
        names = ", ".join(p.name for p in found) or "none"
        raise GeoBuildError(f"{folder} must hold exactly one .shp, found: {names}")
    return found[0]


def _polygonal(geom: BaseGeometry) -> BaseGeometry:
    """The polygon part of a geometry; ``make_valid`` can add stray lines or points."""
    if not geom.is_valid:
        geom = cast("BaseGeometry", shapely.make_valid(geom))
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    polygons = [
        part for part in getattr(geom, "geoms", ()) if part.geom_type in ("Polygon", "MultiPolygon")
    ]
    return cast("BaseGeometry", shapely.union_all(polygons))


def _read(shp: Path, keep: Mapping[str, str] | None = None) -> list[_Feature]:
    """Records and shapes in file order, shapes projected to EPSG:3083 from the layer's .prj."""
    crs = pyproj.CRS.from_wkt(shp.with_suffix(".prj").read_text())
    to_area = pyproj.Transformer.from_crs(crs, AREA_EPSG, always_xy=True)
    features = []
    reader = shapefile.Reader(str(shp))
    try:
        for item in reader.iterShapeRecords():
            raw_record, raw_shape = item.record, item.shape
            if raw_record is None or raw_shape is None:
                raise GeoBuildError(f"{shp.name} has a feature without a record or shape")
            record = raw_record.as_dict()
            if keep is not None and any(str(record.get(k)) != v for k, v in keep.items()):
                continue
            geom = shape(raw_shape.__geo_interface__)
            projected = shapely.transform(geom, to_area.transform, interleaved=False)
            features.append(_Feature(record, _polygonal(projected)))
    finally:
        reader.close()
    return features


def _point(geom: BaseGeometry, to_lonlat: pyproj.Transformer) -> tuple[float, float]:
    point = geom.representative_point()
    lon, lat = to_lonlat.transform(point.x, point.y)
    return round(float(lat), POINT_DECIMALS), round(float(lon), POINT_DECIMALS)


def _district_code(record: Mapping[str, Any]) -> str:
    return str(int(record["District"]))


def _unique(kind: str, codes: Sequence[str]) -> None:
    seen: set[str] = set()
    for code in codes:
        if code in seen:
            raise GeoBuildError(f"{kind} layer repeats code {code}")
        seen.add(code)


def _check_count(kind: str, features: Sequence[_Feature], expected: Mapping[str, int]) -> None:
    if len(features) != expected[kind]:
        raise GeoBuildError(f"{kind} layer has {len(features)} features, expected {expected[kind]}")


def _best_districts(
    precincts: Sequence[BaseGeometry], districts: Sequence[tuple[str, BaseGeometry]]
) -> list[tuple[str, float] | None]:
    """Per precinct, the district with the largest overlap and its share of the precinct."""
    pgeoms = np.array(precincts, dtype=object)
    dgeoms = np.array([geom for _, geom in districts], dtype=object)
    tree = shapely.STRtree(dgeoms)
    pi, di = tree.query(pgeoms, predicate="intersects")
    overlaps = shapely.area(shapely.intersection(pgeoms[pi], dgeoms[di]))
    candidates: dict[int, list[tuple[float, int]]] = {}
    for p, d, area in zip(pi.tolist(), di.tolist(), overlaps.tolist(), strict=True):
        if area > 0:
            candidates.setdefault(p, []).append((area, d))
    totals = shapely.area(pgeoms).tolist()
    best: list[tuple[str, float] | None] = []
    for index, total in enumerate(totals):
        found = candidates.get(index)
        if found is None or total <= 0:
            best.append(None)
            continue
        top = max(area for area, _ in found)
        tied = [districts[d][0] for area, d in found if area >= top * (1 - TIE_TOLERANCE)]
        best.append((min(tied, key=int), min(top / total, 1.0)))
    return best


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _recorded_checksums(reference_dir: Path) -> dict[str, str]:
    recorded: dict[str, str] = {}
    for name in CHECKSUM_FILES:
        path = reference_dir / name
        if path.is_file():
            files = json.loads(path.read_text()).get("files", {})
            base = str(Path(name).parent)
            recorded.update({f"{base}/{rel}": digest for rel, digest in files.items()})
    return recorded


def _source_hashes(reference_dir: Path, paths: Sequence[Path]) -> dict[str, str]:
    """sha256 of every source file, checked against the pipeline's checksum files when listed."""
    recorded = _recorded_checksums(reference_dir)
    hashes = {}
    for path in sorted(paths):
        rel = path.relative_to(reference_dir).as_posix()
        digest = _sha256(path)
        if rel in recorded and recorded[rel] != digest:
            raise GeoBuildError(f"{rel} does not match its recorded sha256")
        hashes[rel] = digest
    return hashes


def _libraries() -> dict[str, str]:
    return {
        "geos": shapely.geos_version_string,
        "proj": pyproj.proj_version_str,
        "pyproj": pyproj.__version__,
        "pyshp": shapefile.__version__,
        "shapely": shapely.__version__,
    }


def _current_precincts(reference_dir: Path) -> tuple[Path, dict[str, Any]]:
    path = reference_dir / CURRENT_PRECINCTS
    if not path.is_file():
        raise GeoBuildError(f"precinct pointer missing: {CURRENT_PRECINCTS}")
    current = json.loads(path.read_text())
    filename = current.get("filename")
    if not isinstance(filename, str) or not filename.endswith(".shp"):
        raise GeoBuildError(f"{CURRENT_PRECINCTS} does not name a .shp file")
    return reference_dir / PRECINCT_FOLDER / filename, current


def _iter_district_layers(
    reference_dir: Path, expected: Mapping[str, int]
) -> Iterator[tuple[str, Path, list[_Feature]]]:
    for kind, folder in DISTRICT_FOLDERS.items():
        shp = _only_shapefile(reference_dir, folder)
        _layer_parts(reference_dir, shp)
        features = _read(shp)
        _check_count(kind, features, expected)
        yield kind, shp, features


def build_geo(
    reference_dir: Path,
    *,
    pipeline_commit: str | None = None,
    expected: Mapping[str, int] = EXPECTED_COUNTS,
) -> GeoData:
    """Areas with interior points and per-precinct districts from ``reference_dir``."""
    reference_dir = Path(reference_dir)
    to_lonlat = pyproj.Transformer.from_crs(AREA_EPSG, POINT_EPSG, always_xy=True)
    sources: list[Path] = []
    areas: list[Area] = []

    precinct_shp, current = _current_precincts(reference_dir)
    sources.append(reference_dir / CURRENT_PRECINCTS)
    sources.extend(_layer_parts(reference_dir, precinct_shp))
    county_shp = _only_shapefile(reference_dir, COUNTY_FOLDER)
    sources.extend(_layer_parts(reference_dir, county_shp))

    plans: dict[str, str] = {}
    districts: dict[str, list[tuple[str, BaseGeometry]]] = {}
    for kind, shp, features in _iter_district_layers(reference_dir, expected):
        sources.extend(_layer_parts(reference_dir, shp))
        plans[kind] = shp.stem
        codes = [_district_code(f.record) for f in features]
        _unique(kind, codes)
        districts[kind] = [(code, f.geom) for code, f in zip(codes, features, strict=True)]
        for code, feature in zip(codes, features, strict=True):
            lat, lon = _point(feature.geom, to_lonlat)
            areas.append(Area(kind, code, f"{DISTRICT_LABELS[kind]} {code}", lat, lon))

    counties = _read(county_shp, keep={"STATEFP": TEXAS_STATEFP})
    _check_count("county", counties, expected)
    _unique("county", [str(f.record["COUNTYFP"]) for f in counties])
    for feature in counties:
        lat, lon = _point(feature.geom, to_lonlat)
        areas.append(
            Area("county", str(feature.record["COUNTYFP"]), str(feature.record["NAME"]), lat, lon)
        )

    precinct_features = _read(precinct_shp)
    if not precinct_features:
        raise GeoBuildError(f"{precinct_shp.name} has no precincts")
    pctkeys = [str(f.record["PCTKEY"]) for f in precinct_features]
    _unique("precinct", pctkeys)
    geoms = [f.geom for f in precinct_features]
    best = {kind: _best_districts(geoms, districts[kind]) for kind in DISTRICT_FOLDERS}
    precincts: list[PrecinctDistricts] = []
    for index, (pctkey, feature) in enumerate(zip(pctkeys, precinct_features, strict=True)):
        prec = str(feature.record["PREC"])
        lat, lon = _point(feature.geom, to_lonlat)
        areas.append(Area("precinct", pctkey, prec, lat, lon))
        assigned: dict[str, Any] = {}
        for kind in DISTRICT_FOLDERS:
            hit = best[kind][index]
            share = None if hit is None else round(hit[1], SHARE_DECIMALS)
            assigned[kind] = None if hit is None else hit[0]
            assigned[f"{kind}_share"] = share
            assigned[f"split_{kind}"] = None if share is None else share < SPLIT_BELOW
        precincts.append(
            PrecinctDistricts(
                pctkey=pctkey,
                county_fips=f"{int(feature.record['CNTY']):03d}",
                prec=prec,
                **assigned,
            )
        )

    provenance = {
        "area_epsg": AREA_EPSG,
        "county_file": county_shp.name,
        "libraries": _libraries(),
        "method": METHOD,
        "pipeline_commit": pipeline_commit,
        "plans": plans,
        "point_epsg": POINT_EPSG,
        "precinct_file": precinct_shp.name,
        "precinct_kind": current.get("kind"),
        "precinct_year": current.get("year"),
        "sources": _source_hashes(reference_dir, sources),
    }
    return GeoData(
        provenance=provenance,
        areas=tuple(sorted(areas, key=lambda a: (a.kind, a.code))),
        precincts=tuple(sorted(precincts, key=lambda p: p.pctkey)),
    )


def pipeline_gitlink(repo_root: Path = REPO_ROOT) -> str | None:
    """The pipeline submodule commit recorded in this repo's HEAD, or None outside a checkout."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"HEAD:{PIPELINE_SUBMODULE}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def _default_reference_dir() -> Path:
    env = os.environ.get("TX_REFERENCE_DIR", "").strip()
    return Path(env) if env else DEFAULT_REFERENCE_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Texas geography JSON.")
    parser.add_argument("--reference-dir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--pipeline-commit", default=None, help="defaults to the submodule gitlink in HEAD"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    reference_dir = args.reference_dir or _default_reference_dir()
    started = time.monotonic()
    data = build_geo(
        reference_dir,
        pipeline_commit=args.pipeline_commit or pipeline_gitlink(),
        expected=EXPECTED_COUNTS,
    )
    body = data.to_json()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    partial = args.out.with_suffix(args.out.suffix + ".partial")
    partial.write_bytes(body)
    partial.replace(args.out)
    counts: dict[str, int] = {}
    for area in data.areas:
        counts[area.kind] = counts.get(area.kind, 0) + 1
    split = {
        k: sum(1 for p in data.precincts if getattr(p, f"split_{k}")) for k in DISTRICT_FOLDERS
    }
    none = {k: sum(1 for p in data.precincts if getattr(p, k) is None) for k in DISTRICT_FOLDERS}
    logger.info(
        "geo build wrote %s bytes=%s sha256=%s seconds=%.1f areas=%s split=%s no_overlap=%s",
        args.out,
        len(body),
        hashlib.sha256(body).hexdigest(),
        time.monotonic() - started,
        json.dumps(counts, sort_keys=True),
        json.dumps(split, sort_keys=True),
        json.dumps(none, sort_keys=True),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
