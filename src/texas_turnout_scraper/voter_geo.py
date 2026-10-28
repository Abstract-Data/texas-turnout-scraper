"""Place voters at their residential address point and the districts under it (ADR-015, Stage 2).

Runs locally, never in CI: the voterfile addresses are PII and stay on the owner's machine.
Loader-only (``loader`` extra for sqlmodel, ``voterfile`` extra for duckdb). The geometry steps
need the voterfile-audit-pipeline, which needs Python 3.11 or later, so run it in an ephemeral
environment (no project extra):

    uv run --no-project --python 3.12 --with ".[loader,voterfile]" \\
        --with ./vendor/voterfile-audit-pipeline python -m texas_turnout_scraper.voter_geo \\
        --election-id 53815 --dry-run

polars, geopandas, duckdb and the pipeline are imported inside the functions that need them.
Steps:

1. Read the election's live roster VUIDs from D1 (``voter_link.target_vuids``).
2. Read only those VUIDs, and only the address and stated-district columns, from the voterfile
   with DuckDB. No name, birth date or history column is read.
3. Normalize with the pipeline's ingest steps (the public ``ingest`` insists on name columns, so
   its steps are called one by one, in its order). That recovers the pre-directional the vendor
   file keeps only inside ``RADDRESS`` (pipeline ADR-0023).
4. Geocode with ``geocode_frame_offline`` against the TxGIO address-point parquet. Matching is
   exact; the Census/Nominatim geocoder is never imported (see ``_block_census_geocoder``).
5. Assign CD, SD and HD from the plan layers under each matched point.
6. Compare each row's ``src_hash`` with D1, upsert only new or changed rows and delete rows whose
   VUID is no longer a target or no longer geocodes, in bounded ``json_each`` batches. Meta is
   written last, so an interrupted apply is redone in full by the next run.

A dry run does steps 1 to 6 without the schema step and without a write. Logs and output carry
counts only, never VUIDs, addresses or coordinates.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import types
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import ValidationError

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    EarlyVoteSyncError,
    _cloudflare_config_from_env,
)
from texas_turnout_scraper.voter_geo_build import (
    VoterGeoStore,
    _Buffer,
    _count,
    _CountingStore,
    _dump,
    _reason,
    _Sender,
    _stamp,
    _text,
)
from texas_turnout_scraper.voter_geo_models import VoterGeoPoint, VoterGeoPointMeta
from texas_turnout_scraper.voter_geo_tables import (
    COORD_DECIMALS,
    GEOCODE_QUALITY_POINT,
    GEOCODE_SOURCE_POINT,
    PAGE_ROWS,
    POINT_META_NAMES,
    VOTER_GEO_BASE_PRESENT_SQL,
    VOTER_GEO_BASE_TABLE_COUNT,
    VOTER_GEO_PLACES_SQL,
    VOTER_GEO_POINT_COLUMNS,
    VOTER_GEO_POINT_DELETE_SQL,
    VOTER_GEO_POINT_META_SELECT_SQL,
    VOTER_GEO_POINT_META_UPSERT_SQL,
    VOTER_GEO_POINT_PRESENT_SQL,
    VOTER_GEO_POINT_STORED_HASHES_SQL,
    VOTER_GEO_POINT_TABLE_COUNT,
    VOTER_GEO_POINT_UPSERT_SQL,
    VOTER_GEO_POINT_VERSION,
)
from texas_turnout_scraper.voter_link import read_voterfile, target_vuids
from texas_turnout_scraper.voter_scores import src_hash
from texas_turnout_scraper.voterfile import normalize_vuid

logger = logging.getLogger(__name__)

VOTER_GEO_POINT_ROWS_PER_STATEMENT = 2000
# Voterfile rows become one polars frame per chunk, so Python holds only a chunk of row tuples.
FRAME_CHUNK_ROWS = 200_000
VUID_WIDTH = 10

# Only these voterfile columns are read: the address, the stated districts and the key. The real
# file has no RSTPRE; the directional survives only inside RADDRESS.
GEO_VOTERFILE_COLUMNS: tuple[str, ...] = (
    "VUID",
    "COUNTY",
    "RADDRESS",
    "RHNUM",
    "RDESIG",
    "RSTNAME",
    "RSTTYPE",
    "RSTSFX",
    "RUNUM",
    "RUTYPE",
    "RCITY",
    "RZIP",
    "CDPLANC2333",
    "SD2022",
    "HD2022",
)
# Pipeline column names the later steps need once the ingest steps have renamed the columns.
FRAME_COLUMNS: tuple[str, ...] = (
    "vuid",
    "county_code",
    "house_number",
    "street_pre_dir",
    "street_name",
    "street_type",
    "street_post_dir",
    "cd",
    "sd",
    "hd",
)
# Shapefile registry key, district column, and the ADR-010 plan the layer must be.
LAYERS: tuple[tuple[str, str, str], ...] = (
    ("cd", "congressional", "PLANC2333"),
    ("sd", "state_senate", "PLANS2168"),
    ("hd", "state_house", "PLANH2316"),
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_REV = re.compile(r"[0-9a-f]{40}")
_AGREEMENT_BUCKETS = {
    "AGREES": "agree",
    "DISAGREES": "disagree",
    "UNDERIVABLE": "underivable",
    "STATED_NOT_RECORDED": "stated_missing",
    "ASSIGNED_NO_STATED": "stated_missing",
}
BUCKETS = ("agree", "disagree", "underivable", "stated_missing")
_CENSUS_GEOCODER = "voterfile_audit_pipeline.stages.geography.geocoding"

PointStatus = Literal["dry_run", "wrote", "unchanged"]


class PointFix(NamedTuple):
    """One geocoded VUID: the point (6 decimals) and the districts derived under it."""

    lat: float
    lon: float
    cd: str | None
    sd: str | None
    hd: str | None


@dataclass(frozen=True)
class Located:
    """What the pipeline made of the target voters: a fix per geocoded VUID, and its counts.

    ``districts`` maps ``cd``, ``sd`` and ``hd`` to counts of ``agree``, ``disagree``,
    ``underivable`` and ``stated_missing``.
    """

    fixes: Mapping[str, PointFix]
    voterfile_rows: int = 0
    ingest_dropped: int = 0
    counties_without_points: int = 0
    districts: Mapping[str, Mapping[str, int]] = field(default_factory=dict)

    @property
    def geocoded(self) -> int:
        return len(self.fixes)

    @property
    def not_geocoded(self) -> int:
        return self.voterfile_rows - self.ingest_dropped - len(self.fixes)


@dataclass(frozen=True)
class Inputs:
    """What one run reads from disk besides D1: provenance for the meta rows."""

    vintage: str
    plans: Mapping[str, str]
    pipeline_rev: str
    voterfile_name: str
    voterfile_size: int


@dataclass(frozen=True)
class PointSync:
    """What differs from D1, and the signature of the table the run leaves behind."""

    rows_pending: int
    rows_stale: int
    statements: int
    signature: str


@dataclass(frozen=True)
class VoterGeoPointResult:
    """Counts of one run. ``rows_pending`` and ``rows_stale`` are what differs from D1."""

    status: PointStatus
    targets: int = 0
    voterfile_rows: int = 0
    ingest_dropped: int = 0
    geocoded: int = 0
    not_geocoded: int = 0
    counties_without_points: int = 0
    stated_precinct: int = 0
    not_located: int = 0
    districts: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    disagree_with_precinct: Mapping[str, int] = field(default_factory=dict)
    rows_pending: int = 0
    rows_stale: int = 0
    statements: int = 0
    seconds: float = 0.0


# --- Pure decisions over plain records -------------------------------------------------------


def coordinate(value: float) -> float:
    return round(value, COORD_DECIMALS)


def point_hash(vuid: str, fix: PointFix, vintage: str) -> int:
    """src_hash over every stored column except ``matched_at``."""
    return src_hash(
        [
            VOTER_GEO_POINT_VERSION,
            vuid,
            f"{coordinate(fix.lat):.{COORD_DECIMALS}f}",
            f"{coordinate(fix.lon):.{COORD_DECIMALS}f}",
            fix.cd,
            fix.sd,
            fix.hd,
            GEOCODE_SOURCE_POINT,
            GEOCODE_QUALITY_POINT,
            vintage,
        ]
    )


def point_record(
    vuid: str, fix: PointFix, vintage: str, hashed: int, matched_at: str
) -> VoterGeoPoint:
    """The row for one VUID. Raises ``EarlyVoteSyncError`` naming fields only."""
    fields_ = {
        "vuid": vuid,
        "lat": coordinate(fix.lat),
        "lon": coordinate(fix.lon),
        "cd": fix.cd,
        "sd": fix.sd,
        "hd": fix.hd,
        "geocode_source": GEOCODE_SOURCE_POINT,
        "geocode_quality": GEOCODE_QUALITY_POINT,
        "address_points_vintage": vintage,
        "src_hash": hashed,
        "matched_at": matched_at,
    }
    try:
        return VoterGeoPoint.model_validate(fields_)
    except ValidationError as error:
        raise EarlyVoteSyncError(
            f"voter_geo_point row failed validation: {_reason(error)}"
        ) from None


def fold_agreements(summary: Mapping[str, Mapping[str, int]]) -> dict[str, dict[str, int]]:
    """Per layer, the pipeline's five agreement outcomes as agree, disagree, underivable and
    stated_missing (a stated district that is blank or absent)."""
    folded: dict[str, dict[str, int]] = {}
    for layer, counts in summary.items():
        bucketed = dict.fromkeys(BUCKETS, 0)
        for outcome, n in counts.items():
            bucketed[_AGREEMENT_BUCKETS[outcome]] += n
        folded[layer] = bucketed
    return folded


def district_key(value: object) -> str:
    """District codes compare without leading zeros, as the pipeline and ADR-010 write them."""
    return str(value).strip().lstrip("0").upper()


def _stored_hashes(store: VoterGeoStore, database_id: str) -> dict[str, int]:
    hashes: dict[str, int] = {}
    after = ""
    while True:
        page = store._query_rows(database_id, VOTER_GEO_POINT_STORED_HASHES_SQL, [after])  # noqa: SLF001
        for row in page:
            hashes[_text(row.get("vuid"))] = int(_text(row.get("src_hash")))
        if len(page) < PAGE_ROWS:
            return hashes
        after = _text(page[-1].get("vuid"))


def _stored_meta(store: VoterGeoStore, database_id: str) -> dict[str, str]:
    return {
        _text(row.get("name")): _text(row.get("value"))
        for row in store._query_rows(database_id, VOTER_GEO_POINT_META_SELECT_SQL, [])  # noqa: SLF001
    }


def point_meta(located: Located, inputs: Inputs, targets: int, signature: str) -> dict[str, str]:
    counts = {
        "targets": targets,
        "voterfile_rows": located.voterfile_rows,
        "ingest_dropped": located.ingest_dropped,
        "geocoded": located.geocoded,
        "counties_without_points": located.counties_without_points,
        "districts": located.districts,
    }
    meta = {
        "signature": signature,
        "address_points_vintage": inputs.vintage,
        "plans": _dump(dict(inputs.plans)),
        "pipeline_rev": inputs.pipeline_rev,
        "voterfile": _dump({"name": inputs.voterfile_name, "size": inputs.voterfile_size}),
        "counts": _dump(counts),
    }
    if tuple(meta) != POINT_META_NAMES:
        raise EarlyVoteSyncError("voter_geo_point meta names drifted from the committed list")
    return meta


def sync_points(  # noqa: PLR0913
    store: VoterGeoStore,
    database_id: str,
    located: Located,
    inputs: Inputs,
    *,
    targets: int,
    dry_run: bool,
    tables_present: bool = True,
    batch_rows: int = VOTER_GEO_POINT_ROWS_PER_STATEMENT,
    matched_at: str,
) -> PointSync:
    """Bring ``voter_geo_point`` to the located fixes; see the module."""
    hashes = _stored_hashes(store, database_id) if tables_present else {}
    if not dry_run and hashes and not located.fixes:
        raise EarlyVoteSyncError("no target voter geocoded; refusing to delete every stored point")
    sender = _Sender(store, database_id, dry_run=dry_run, batch_rows=batch_rows)
    upserts = _Buffer(sender, VOTER_GEO_POINT_UPSERT_SQL)
    digest = hashlib.sha256(_dump([VOTER_GEO_POINT_VERSION, inputs.vintage]).encode())
    failed, first_failure = 0, ""
    for vuid in sorted(located.fixes):
        fix = located.fixes[vuid]
        hashed = point_hash(vuid, fix, inputs.vintage)
        digest.update(f"{vuid}:{hashed}\n".encode())
        if hashes.pop(vuid, None) == hashed:
            continue
        try:
            record = point_record(vuid, fix, inputs.vintage, hashed, matched_at)
        except EarlyVoteSyncError as error:
            failed += 1
            first_failure = first_failure or str(error)
            continue
        upserts.add([getattr(record, column) for column in VOTER_GEO_POINT_COLUMNS])
    if failed:
        raise EarlyVoteSyncError(
            f"{failed} voter_geo_point rows failed validation; first: {first_failure}"
        )
    upserts.finish()
    stale = sorted(hashes)
    sender.send(VOTER_GEO_POINT_DELETE_SQL, stale)
    signature = digest.hexdigest()
    meta = point_meta(located, inputs, targets, signature)
    for name, value in meta.items():
        VoterGeoPointMeta.model_validate({"name": name, "value": value})
    stored = _stored_meta(store, database_id) if tables_present else {}
    if any(stored.get(name) != value for name, value in meta.items()):
        sender.send(VOTER_GEO_POINT_META_UPSERT_SQL, sorted([n, v] for n, v in meta.items()))
    return PointSync(upserts.count, len(stale), sender.statements, signature)


def compare_with_precinct(
    store: VoterGeoStore,
    database_id: str,
    targets: Iterable[str],
    fixes: Mapping[str, PointFix],
) -> tuple[int, dict[str, int]]:
    """One keyset pass over ``voter_geo``, keeping counts only.

    Returns the targets without a point whose stored row has a precinct, and per layer the
    points whose district differs from the precinct's where both exist.
    """
    wanted = targets if isinstance(targets, (set, frozenset)) else set(targets)
    stated = 0
    disagree = {layer: 0 for layer, _, _ in LAYERS}
    after = ""
    while True:
        page = store._query_rows(database_id, VOTER_GEO_PLACES_SQL, [after])  # noqa: SLF001
        for row in page:
            vuid = _text(row.get("vuid"))
            if vuid not in wanted:
                continue
            fix = fixes.get(vuid)
            if fix is None:
                stated += row.get("pctkey") is not None
                continue
            for layer in disagree:
                point, precinct = getattr(fix, layer), row.get(layer)
                if point is not None and precinct is not None:
                    disagree[layer] += district_key(point) != district_key(precinct)
        if len(page) < PAGE_ROWS:
            return stated, disagree
        after = _text(page[-1].get("vuid"))


# --- Local inputs ----------------------------------------------------------------------------


def read_vintage(points_dir: Path) -> str:
    """``acquisition_date`` of the address-point manifest, next to the parquet folder or in it."""
    for manifest in (points_dir.parent / "manifest.json", points_dir / "manifest.json"):
        if not manifest.is_file():
            continue
        try:
            value = json.loads(manifest.read_text()).get("acquisition_date")
        except (json.JSONDecodeError, AttributeError):
            raise EarlyVoteSyncError("address-point manifest is not a JSON object") from None
        if not isinstance(value, str) or _DATE.fullmatch(value) is None:
            raise EarlyVoteSyncError("address-point manifest has no acquisition_date")
        return value
    raise EarlyVoteSyncError("no address-point manifest.json beside or in the points directory")


def check_plans(shapefile_dir: Path) -> dict[str, str]:
    """Each layer folder holds exactly one .shp, and it is the ADR-010 plan. Returns the names."""
    plans: dict[str, str] = {}
    for _stated, layer, plan in LAYERS:
        shapes = sorted((shapefile_dir / layer).glob("*.shp"))
        if len(shapes) != 1 or plan not in shapes[0].stem.upper():
            raise EarlyVoteSyncError(f"{layer} layer is not exactly one {plan} shapefile")
        plans[layer] = shapes[0].stem
    return plans


def pipeline_revision(pipeline_dir: Path) -> str:
    """The pinned pipeline commit: the checkout's HEAD, else the superproject's gitlink."""
    attempts = (
        ["git", "-C", str(pipeline_dir), "rev-parse", "HEAD"],
        ["git", "-C", str(pipeline_dir.parent), "ls-tree", "HEAD", pipeline_dir.name],
    )
    for args in attempts:
        try:
            out = subprocess.run(
                args, capture_output=True, text=True, check=True, timeout=30
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        match = _REV.search(out)
        if match:
            return match.group(0)
    logger.warning("voter_geo pipeline revision is unknown")
    return "unknown"


def read_inputs(
    voterfile: Path, points_dir: Path, shapefile_dir: Path, pipeline_dir: Path
) -> Inputs:
    """Check every local input before anything is read from or written to D1."""
    if not voterfile.is_file():
        raise EarlyVoteSyncError("voterfile not found")
    if not any(points_dir.glob("*.parquet")):
        raise EarlyVoteSyncError("no address-point parquet files in the points directory")
    return Inputs(
        vintage=read_vintage(points_dir),
        plans=check_plans(shapefile_dir),
        pipeline_rev=pipeline_revision(pipeline_dir),
        voterfile_name=voterfile.name,
        voterfile_size=voterfile.stat().st_size,
    )


# --- The pipeline adapter: the only code that touches polars, geopandas and the pipeline ------


def _load(module: str) -> Any:
    """A module of the ephemeral environment (polars, geopandas, the pipeline), imported late."""
    return importlib.import_module(module)


@contextmanager
def _step(name: str) -> Generator[None, None, None]:
    try:
        yield
    except EarlyVoteSyncError:
        raise
    except Exception as error:  # noqa: BLE001  # reason: library errors can quote a cell
        raise EarlyVoteSyncError(f"voter_geo {name} failed: {type(error).__name__}") from None


def _block_census_geocoder() -> None:
    """Keep the pipeline's Census/Nominatim geocoder from ever loading.

    The pipeline's ``stages.geography`` package imports ``.geocoding`` when it is first imported,
    and the offline geocoder and the district assignment live inside that package. A tripwire
    module takes the name first, so the real module never runs and a call raises.
    """
    existing = sys.modules.get(_CENSUS_GEOCODER)
    if existing is not None:
        if getattr(existing, "TRIPWIRE", False):
            return
        raise EarlyVoteSyncError("the Census geocoder module is already loaded")

    def geocode_batch(*_args: object, **_kwargs: object) -> None:
        raise EarlyVoteSyncError("the Census geocoder is blocked: addresses stay local")

    tripwire = types.ModuleType(_CENSUS_GEOCODER)
    setattr(tripwire, "TRIPWIRE", True)  # noqa: B010  # reason: attributes of a stub module
    setattr(tripwire, "geocode_batch", geocode_batch)  # noqa: B010
    sys.modules[_CENSUS_GEOCODER] = tripwire


def _chunks(rows: Iterable[Mapping[str, str | None]]) -> Iterator[list[tuple[str | None, ...]]]:
    pending: list[tuple[str | None, ...]] = []
    for row in rows:
        pending.append(tuple(row[column] for column in GEO_VOTERFILE_COLUMNS))
        if len(pending) >= FRAME_CHUNK_ROWS:
            yield pending
            pending = []
    if pending:
        yield pending


def _normalized_frame(pl: Any, rows: Iterable[Mapping[str, str | None]]) -> Any:
    """The selected rows as the pipeline's ingest would leave them, without its name columns.

    ``ingest`` requires ``vuid``, ``name_last`` and ``name_first`` and this run must not read
    names, so the steps it takes after reading are called in its order: the null spellings,
    column aliases, strip and upper-case, the pre-directional recovery from ``RADDRESS`` and the
    address assembly. ``_derive_cntyvtd`` is skipped (it needs ``PCT``, which is not read) and
    no status is read, so no registration is suppressed.
    """
    ingest = _load("voterfile_audit_pipeline.stages.ingest")
    schema = dict.fromkeys(GEO_VOTERFILE_COLUMNS, pl.String)
    parts = [pl.DataFrame(chunk, schema=schema, orient="row") for chunk in _chunks(rows)]
    raw = pl.concat(parts) if parts else pl.DataFrame(schema=schema)
    lazy = raw.lazy().with_columns(
        [
            pl.when(pl.col(column).is_in(ingest.NULL_VALUES))
            .then(None)
            .otherwise(pl.col(column))
            .alias(column)
            for column in GEO_VOTERFILE_COLUMNS
        ]
    )
    lazy = ingest.normalize_voter_columns(lazy)
    lazy = lazy.with_columns(pl.col(pl.String).str.strip_chars().str.to_uppercase())
    lazy = ingest._recover_pre_directional(lazy)  # noqa: SLF001
    lazy = ingest._combine_address_component_columns(lazy)  # noqa: SLF001
    lazy = ingest._combine_address_unit_columns(lazy)  # noqa: SLF001
    frame = lazy.collect()
    missing = [name for name in FRAME_COLUMNS if name not in frame.columns]
    if missing:
        raise EarlyVoteSyncError(f"normalized frame lacks columns: {','.join(missing)}")
    return frame


def _fixes(placed: Any, assigned: Any) -> dict[str, PointFix]:
    """Fixes from the matched rows and the districts derived for them, row for row."""
    if assigned.height != placed.height:
        raise EarlyVoteSyncError("district assignment lost rows")
    empty = [None] * placed.height
    derived = {
        layer: assigned[f"derived_{layer}"].to_list()
        if f"derived_{layer}" in assigned.columns
        else empty
        for layer, _, _ in LAYERS
    }
    fixes: dict[str, PointFix] = {}
    for index, (vuid, lat, lon) in enumerate(
        zip(placed["vuid"], placed["lat"], placed["lon"], strict=True)
    ):
        key = vuid if len(vuid) == VUID_WIDTH and vuid.isdigit() else normalize_vuid(vuid)
        fixes[key] = PointFix(
            coordinate(lat),
            coordinate(lon),
            derived["cd"][index],
            derived["sd"][index],
            derived["hd"][index],
        )
    return fixes


def _locate(voterfile: Path, targets: set[str], points_dir: Path, shapefile_dir: Path) -> Located:
    gpd = _load("geopandas")
    pl = _load("polars")
    _block_census_geocoder()
    package = "voterfile_audit_pipeline"
    registry_type = _load(f"{package}.reference.tiger").ShapefileRegistry
    districts_module = _load(f"{package}.stages.geography.district_assignment")
    geocoder = _load(f"{package}.stages.geography.offline_geocode")
    configure_logging = _load(f"{package}.utils.logging").configure_logging

    # Pipeline logs go to stderr at WARNING: stdout stays the count lines.
    configure_logging("WARNING")
    with _step("voterfile read"):
        frame = _normalized_frame(pl, read_voterfile(voterfile, targets, GEO_VOTERFILE_COLUMNS))
    with _step("dedup"):
        rows_read = frame.height
        frame = frame.with_columns(pl.col("vuid").str.zfill(VUID_WIDTH)).unique(
            subset=["vuid"], keep="last", maintain_order=True
        )
        dropped = rows_read - frame.height
    with _step("geocode"):
        geocoded, _matched_by_county = geocoder.geocode_frame_offline(frame, points_dir)
        counties = geocoded["county_fips"].unique().to_list() if "county_fips" in geocoded else []
        without = sum(
            1 for fips in counties if fips is None or not (points_dir / f"{fips}.parquet").exists()
        )
        placed = geocoded.filter(pl.col("geocoded")).select("vuid", "lon", "lat", "cd", "sd", "hd")
    with _step("district assignment"):
        registry = registry_type(shapefile_dir).load_all()
        boundaries = {layer: registry.get(layer) for _, layer, _ in LAYERS}
        if any(shapes is None for shapes in boundaries.values()):
            raise EarlyVoteSyncError("a plan layer failed to load")
        pdf = placed.to_pandas()
        points = gpd.GeoDataFrame(
            pdf, geometry=gpd.points_from_xy(pdf["lon"], pdf["lat"]), crs="EPSG:4326"
        )
        assigned = districts_module.assign_districts(points, boundaries)
        districts = fold_agreements(districts_module.summarize_districts(assigned))
        fixes = _fixes(placed, assigned)
    return Located(fixes, rows_read, dropped, without, districts)


def pipeline_locator(
    voterfile: Path, points_dir: Path, shapefile_dir: Path
) -> Callable[[set[str]], Located]:
    def locate(targets: set[str]) -> Located:
        return _locate(voterfile, targets, points_dir, shapefile_dir)

    return locate


# --- The run ----------------------------------------------------------------------------------


def run_voter_geo_on_store(
    store: VoterGeoStore,
    election_id: str,
    locate: Callable[[set[str]], Located],
    inputs: Inputs,
    *,
    dry_run: bool,
    batch_rows: int = VOTER_GEO_POINT_ROWS_PER_STATEMENT,
    now: datetime | None = None,
) -> VoterGeoPointResult | None:
    """The whole run for one election. Without an election database there is nothing to do."""
    started = time.monotonic()
    election_id = require_election_id(election_id)
    counted = _CountingStore(store)
    database_id = counted._database_uuid_if_exists(election_id)  # noqa: SLF001
    if database_id is None:
        logger.info("voter_geo point election_id=%s has no D1 database", election_id)
        return None
    if dry_run:
        base = _count(counted, database_id, VOTER_GEO_BASE_PRESENT_SQL)
        points = _count(counted, database_id, VOTER_GEO_POINT_PRESENT_SQL)
        base_present = base == VOTER_GEO_BASE_TABLE_COUNT
        points_present = points == VOTER_GEO_POINT_TABLE_COUNT
        if not points_present:
            logger.info("voter_geo point dry run: point tables absent, nothing is created")
    else:
        counted._ensure_schema(database_id)  # noqa: SLF001
        base_present = points_present = True
    targets = target_vuids(counted, database_id, election_id)
    located = locate(targets)
    sync = sync_points(
        counted,
        database_id,
        located,
        inputs,
        targets=len(targets),
        dry_run=dry_run,
        tables_present=points_present,
        batch_rows=batch_rows,
        matched_at=_stamp(now or datetime.now(timezone.utc)),
    )
    stated, disagree = (
        compare_with_precinct(counted, database_id, targets, located.fixes)
        if base_present
        else (0, dict.fromkeys(("cd", "sd", "hd"), 0))
    )
    status: PointStatus = "dry_run" if dry_run else ("wrote" if sync.statements else "unchanged")
    result = VoterGeoPointResult(
        status,
        targets=len(targets),
        voterfile_rows=located.voterfile_rows,
        ingest_dropped=located.ingest_dropped,
        geocoded=located.geocoded,
        not_geocoded=located.not_geocoded,
        counties_without_points=located.counties_without_points,
        stated_precinct=stated,
        not_located=len(targets) - located.geocoded - stated,
        districts=located.districts,
        disagree_with_precinct=disagree,
        rows_pending=sync.rows_pending,
        rows_stale=sync.rows_stale,
        statements=sync.statements,
    )
    result = replace(result, seconds=time.monotonic() - started)
    _log(election_id, result)
    return result


def output_lines(result: VoterGeoPointResult) -> list[str]:
    """``key=value`` lines of counts only."""
    lines = [
        f"status={result.status}",
        f"targets={result.targets}",
        f"voterfile_rows={result.voterfile_rows}",
        f"ingest_dropped={result.ingest_dropped}",
        f"geocoded={result.geocoded}",
        f"not_geocoded={result.not_geocoded}",
        f"counties_without_points={result.counties_without_points}",
        f"stated_precinct={result.stated_precinct}",
        f"not_located={result.not_located}",
    ]
    for layer, _, _ in LAYERS:
        counts = result.districts.get(layer, {})
        lines += [f"{layer}_{bucket}={counts.get(bucket, 0)}" for bucket in BUCKETS]
    lines += [
        f"disagree_with_precinct_{layer}={result.disagree_with_precinct.get(layer, 0)}"
        for layer, _, _ in LAYERS
    ]
    lines += [
        f"rows_pending={result.rows_pending}",
        f"rows_stale={result.rows_stale}",
        f"statements={result.statements}",
        f"seconds={result.seconds:.1f}",
    ]
    return lines


def _log(election_id: str, result: VoterGeoPointResult) -> None:
    logger.info("voter_geo point election_id=%s %s", election_id, " ".join(output_lines(result)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Place target voters at their address point.")
    parser.add_argument("--election-id", required=True)
    parser.add_argument("--voterfile", type=Path, default=os.environ.get("TX_VOTERFILE"))
    parser.add_argument("--points-dir", type=Path, default=os.environ.get("TX_ADDRESS_POINTS_DIR"))
    parser.add_argument("--shapefile-dir", type=Path, default=os.environ.get("TX_SHAPEFILE_DIR"))
    parser.add_argument(
        "--pipeline-dir", type=Path, default=Path("vendor/voterfile-audit-pipeline")
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run == args.apply:
        parser.error("pass exactly one of --dry-run or --apply")
    for name, value in (
        ("--voterfile or TX_VOTERFILE", args.voterfile),
        ("--points-dir or TX_ADDRESS_POINTS_DIR", args.points_dir),
        ("--shapefile-dir or TX_SHAPEFILE_DIR", args.shapefile_dir),
    ):
        if value is None:
            parser.error(f"pass {name}")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    inputs = read_inputs(
        Path(args.voterfile), Path(args.points_dir), Path(args.shapefile_dir), args.pipeline_dir
    )
    config = _cloudflare_config_from_env()
    if config is None:
        raise EarlyVoteSyncError(
            "voter_geo requires CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, "
            "and CLOUDFLARE_KV_NAMESPACE_ID"
        )
    account_id, api_token, namespace_id = config
    store = CloudflareEarlyVoteStore(
        account_id=account_id, api_token=api_token, kv_namespace_id=namespace_id
    )
    locate = pipeline_locator(Path(args.voterfile), Path(args.points_dir), Path(args.shapefile_dir))
    try:
        result = run_voter_geo_on_store(
            store, args.election_id, locate, inputs, dry_run=args.dry_run
        )
    finally:
        store.close()
    if result is None:
        sys.stdout.write("status=no_database\n")
        return 0
    sys.stdout.write("\n".join(output_lines(result)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
