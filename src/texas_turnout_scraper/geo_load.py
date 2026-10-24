"""Load the committed geography JSON into an election's D1 database and alias roster precincts.

Loader-only (``loader`` extra): imports ``geo_models`` (sqlmodel), never the geo build libraries.
Both steps are idempotent. ``load_geo`` skips when ``geo_meta`` already holds the file's sha256;
otherwise it upserts every row, deletes rows the file no longer has, and writes ``geo_meta``
last, so a crash anywhere before that is redone in full on the next run. ``build_aliases``
resolves each live roster ``(county, precinct)`` pair against the loaded precincts and writes
only the alias rows that changed. Logs carry counts, never county or precinct values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_roster_rows import day_number, key_ranges
from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    EarlyVoteSyncError,
    _cloudflare_config_from_env,
)
from texas_turnout_scraper.geo_models import GeoArea, GeoPrecinct
from texas_turnout_scraper.geo_tables import (
    ALIAS_DELETE_SQL,
    ALIAS_UPSERT_SQL,
    ALIASES_SQL,
    AREA_DELETE_SQL,
    AREA_KEYS_SQL,
    AREA_UPSERT_SQL,
    COUNTY_NAMES_SQL,
    GEO_ROWS_PER_STATEMENT,
    GEO_TABLE_COUNT,
    GEO_TABLES_PRESENT_SQL,
    META_NAME,
    META_SELECT_SQL,
    META_UPSERT_SQL,
    PAIR_COUNTS_SQL,
    PRECINCT_COLUMNS,
    PRECINCT_DELETE_SQL,
    PRECINCT_KEYS_SQL,
    PRECINCT_LOOKUP_SQL,
    PRECINCT_UPSERT_SQL,
    ROSTER_DAYS_SQL,
)

logger = logging.getLogger(__name__)

DEFAULT_JSON = Path("data/geo/tx_geo.json")
# Each pair-count query covers one day and 1/ALIAS_KEY_RANGES of the row-hash space.
ALIAS_KEY_RANGES = 4

LoadStatus = Literal["dry_run", "wrote", "unchanged"]
Pair = tuple[str, str]
Alias = tuple[str | None, str]


class GeoStore(Protocol):
    def _query_rows(
        self, database_id: str, sql: str, params: list[str]
    ) -> list[dict[str, object]]: ...

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None: ...


class GeoLoadStore(GeoStore, Protocol):
    def _database_uuid_if_exists(self, election_id: str) -> str | None: ...

    def _ensure_schema(self, database_id: str) -> None: ...


@dataclass(frozen=True)
class GeoLoadResult:
    status: LoadStatus
    areas: int = 0
    precincts: int = 0
    removed_areas: int = 0
    removed_precincts: int = 0


@dataclass(frozen=True)
class AliasResult:
    pairs: int
    matched_pairs: int
    unmatched_pairs: int
    rows: int
    matched_rows: int
    written: int
    removed: int

    @property
    def matched_row_share(self) -> float:
        return self.matched_rows / self.rows if self.rows else 0.0


def county_key(name: str) -> str:
    """A county name compared by its letters only: ``DE WITT`` and ``DeWitt`` are one county."""
    return "".join(ch for ch in name.upper() if ch.isascii() and ch.isalpha())


def _candidates(precinct: str) -> Iterator[tuple[str, str]]:
    yield "exact", precinct
    yield "zfill4", precinct.strip().zfill(4)
    yield "alnum_zfill4", "".join(ch for ch in precinct if ch.isascii() and ch.isalnum()).zfill(4)


def resolve_precinct(fips: str | None, precinct: str, keys: Mapping[Pair, str]) -> Alias:
    """The state precinct for a roster precinct, by the first rule that finds one in its county."""
    if fips is not None and precinct.strip():
        for rule, prec in _candidates(precinct):
            pctkey = keys.get((fips, prec))
            if pctkey is not None:
                return pctkey, rule
    return None, "unmatched"


def _chunks(items: Sequence[object], size: int) -> Iterator[Sequence[object]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _dump(rows: Sequence[object]) -> str:
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def _write_batches(
    store: GeoStore, database_id: str, sql: str, rows: Sequence[object], batch_rows: int
) -> None:
    """One bounded statement per request, each carrying at most ``batch_rows`` rows."""
    for chunk in _chunks(rows, batch_rows):
        store._statements(database_id, [(sql, [_dump(chunk)])])  # noqa: SLF001


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _precinct_row(precinct: GeoPrecinct) -> list[object]:
    row: list[object] = []
    for column in PRECINCT_COLUMNS:
        value = getattr(precinct, column)
        row.append(int(value) if isinstance(value, bool) else value)
    return row


def load_geo(
    store: GeoStore,
    database_id: str,
    json_path: Path,
    *,
    dry_run: bool = False,
    batch_rows: int = GEO_ROWS_PER_STATEMENT,
) -> GeoLoadResult:
    """Bring the geography tables to ``json_path``; skipped when it is already loaded."""
    body = Path(json_path).read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    loaded = store._query_rows(database_id, META_SELECT_SQL, [META_NAME])  # noqa: SLF001
    if loaded and loaded[0].get("sha256") == digest:
        logger.info("geo load unchanged sha256=%s", digest)
        return GeoLoadResult("unchanged")
    data = json.loads(body)
    areas = [GeoArea.model_validate(row) for row in data["areas"]]
    precincts = [GeoPrecinct.model_validate(row) for row in data["precincts"]]
    area_keys = {(a.kind, a.code) for a in areas}
    precinct_keys = {p.pctkey for p in precincts}
    stale_areas = sorted(
        key
        for row in store._query_rows(database_id, AREA_KEYS_SQL, [])  # noqa: SLF001
        if (key := (_text(row.get("kind")), _text(row.get("code")))) not in area_keys
    )
    stale_precincts = sorted(
        key
        for row in store._query_rows(database_id, PRECINCT_KEYS_SQL, [])  # noqa: SLF001
        if (key := _text(row.get("pctkey"))) not in precinct_keys
    )
    result = GeoLoadResult(
        "dry_run" if dry_run else "wrote",
        len(areas),
        len(precincts),
        len(stale_areas),
        len(stale_precincts),
    )
    if not dry_run:
        area_rows = [[a.kind, a.code, a.name, a.lat, a.lon] for a in areas]
        _write_batches(store, database_id, AREA_UPSERT_SQL, area_rows, batch_rows)
        precinct_rows = [_precinct_row(p) for p in precincts]
        _write_batches(store, database_id, PRECINCT_UPSERT_SQL, precinct_rows, batch_rows)
        _write_batches(
            store, database_id, AREA_DELETE_SQL, [list(k) for k in stale_areas], batch_rows
        )
        _write_batches(
            store, database_id, PRECINCT_DELETE_SQL, [[k] for k in stale_precincts], batch_rows
        )
        store._statements(database_id, [(META_UPSERT_SQL, [META_NAME, digest])])  # noqa: SLF001
    logger.info(
        "geo load status=%s sha256=%s areas=%s precincts=%s removed_areas=%s removed_precincts=%s",
        result.status,
        digest,
        result.areas,
        result.precincts,
        result.removed_areas,
        result.removed_precincts,
    )
    return result


def _pair_counts(store: GeoStore, database_id: str, election_id: str) -> dict[Pair, int]:
    """Live roster rows per ``(county, precinct)``, read one day and one key range at a time."""
    counts: dict[Pair, int] = {}
    days = store._query_rows(database_id, ROSTER_DAYS_SQL, [election_id])  # noqa: SLF001
    for day_row in days:
        day = str(day_number(_text(day_row.get("ev_date"))))
        for low, high in key_ranges(ALIAS_KEY_RANGES):
            for row in store._query_rows(  # noqa: SLF001
                database_id, PAIR_COUNTS_SQL, [day, low, high]
            ):
                pair = (_text(row.get("county")), _text(row.get("precinct")))
                counts[pair] = counts.get(pair, 0) + int(_text(row.get("n")) or 0)
    return counts


def _rows(rows: Iterable[Mapping[str, object]], *columns: str) -> Iterator[tuple[str, ...]]:
    for row in rows:
        yield tuple(_text(row.get(column)) for column in columns)


@dataclass(frozen=True)
class GeoCatalog:
    """County names and precinct keys read from the geography JSON instead of the tables."""

    fips_by_name: dict[str, str]
    keys: dict[tuple[str, str], str]


def catalog_from_json(data: Mapping[str, Any]) -> GeoCatalog:
    return GeoCatalog(
        fips_by_name={
            county_key(str(a["name"])): str(a["code"])
            for a in data["areas"]
            if a["kind"] == "county"
        },
        keys={(str(p["county_fips"]), str(p["prec"])): str(p["pctkey"]) for p in data["precincts"]},
    )


def _schema_present(store: GeoStore, database_id: str) -> bool:
    rows = store._query_rows(database_id, GEO_TABLES_PRESENT_SQL, [])  # noqa: SLF001
    return bool(rows) and int(_text(rows[0].get("n")) or 0) == GEO_TABLE_COUNT


def build_aliases(
    store: GeoStore,
    database_id: str,
    election_id: str,
    *,
    dry_run: bool = False,
    batch_rows: int = GEO_ROWS_PER_STATEMENT,
    catalog: GeoCatalog | None = None,
) -> AliasResult:
    """Alias every live roster pair to a loaded precinct, or ``unmatched``. Never guesses.

    ``catalog`` stands in for the geography tables, which a dry run on a fresh database does not
    have; then there are no stored aliases either.
    """
    fips_by_name = (
        catalog.fips_by_name
        if catalog is not None
        else {
            county_key(name): code
            for code, name in _rows(
                store._query_rows(database_id, COUNTY_NAMES_SQL, []),  # noqa: SLF001
                "code",
                "name",
            )
        }
    )
    keys = (
        catalog.keys
        if catalog is not None
        else {
            (fips, prec): pctkey
            for pctkey, fips, prec in _rows(
                store._query_rows(database_id, PRECINCT_LOOKUP_SQL, []),  # noqa: SLF001
                "pctkey",
                "county_fips",
                "prec",
            )
        }
    )
    counts = _pair_counts(store, database_id, election_id)
    wanted = {
        pair: resolve_precinct(fips_by_name.get(county_key(pair[0])), pair[1], keys)
        for pair in counts
    }
    existing: dict[Pair, Alias] = {}
    stored = [] if catalog is not None else store._query_rows(database_id, ALIASES_SQL, [])  # noqa: SLF001
    for row in stored:
        pctkey = row.get("pctkey")
        existing[(_text(row.get("county")), _text(row.get("precinct")))] = (
            None if pctkey is None else str(pctkey),
            _text(row.get("rule")),
        )
    changed = sorted(pair for pair, alias in wanted.items() if existing.get(pair) != alias)
    vanished = sorted(pair for pair in existing if pair not in wanted)
    if not dry_run:
        rows = [[county, precinct, *wanted[(county, precinct)]] for county, precinct in changed]
        _write_batches(store, database_id, ALIAS_UPSERT_SQL, rows, batch_rows)
        _write_batches(
            store, database_id, ALIAS_DELETE_SQL, [list(p) for p in vanished], batch_rows
        )
    matched = [pair for pair, (pctkey, _) in wanted.items() if pctkey is not None]
    result = AliasResult(
        pairs=len(wanted),
        matched_pairs=len(matched),
        unmatched_pairs=len(wanted) - len(matched),
        rows=sum(counts.values()),
        matched_rows=sum(counts[pair] for pair in matched),
        written=0 if dry_run else len(changed),
        removed=0 if dry_run else len(vanished),
    )
    by_rule: dict[str, int] = {}
    for _, rule in wanted.values():
        by_rule[rule] = by_rule.get(rule, 0) + 1
    logger.info(
        "geo aliases election_id=%s dry_run=%s pairs=%s matched_pairs=%s unmatched_pairs=%s "
        "rows=%s matched_rows=%s matched_row_share=%.4f rules=%s written=%s removed=%s",
        election_id,
        dry_run,
        result.pairs,
        result.matched_pairs,
        result.unmatched_pairs,
        result.rows,
        result.matched_rows,
        result.matched_row_share,
        json.dumps(by_rule, sort_keys=True),
        len(changed),
        len(vanished),
    )
    return result


def run_geo_load_on_store(
    store: GeoLoadStore, election_id: str, json_path: Path, *, dry_run: bool
) -> tuple[GeoLoadResult, AliasResult | None]:
    """Load geography, then aliases. Without an election database there is nothing to do."""
    election_id = require_election_id(election_id)
    database_id = store._database_uuid_if_exists(election_id)  # noqa: SLF001
    if database_id is None:
        logger.info("geo load election_id=%s has no D1 database", election_id)
        return GeoLoadResult("unchanged"), None
    if not dry_run:
        store._ensure_schema(database_id)  # noqa: SLF001
    elif not _schema_present(store, database_id):
        data = json.loads(Path(json_path).read_bytes())
        logger.info("geo load dry run: geography tables absent, nothing is created")
        loaded = GeoLoadResult("dry_run", len(data["areas"]), len(data["precincts"]))
        aliases = build_aliases(
            store, database_id, election_id, dry_run=True, catalog=catalog_from_json(data)
        )
        return loaded, aliases
    loaded = load_geo(store, database_id, json_path, dry_run=dry_run)
    return loaded, build_aliases(store, database_id, election_id, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load geography and precinct aliases into D1.")
    parser.add_argument("--election-id", required=True)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run == args.apply:
        parser.error("pass exactly one of --dry-run or --apply")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = _cloudflare_config_from_env()
    if config is None:
        raise EarlyVoteSyncError(
            "geo load requires CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, "
            "and CLOUDFLARE_KV_NAMESPACE_ID"
        )
    account_id, api_token, namespace_id = config
    store = CloudflareEarlyVoteStore(
        account_id=account_id, api_token=api_token, kv_namespace_id=namespace_id
    )
    try:
        loaded, aliases = run_geo_load_on_store(
            store, args.election_id, args.json, dry_run=args.dry_run
        )
    finally:
        store.close()
    lines = [
        f"geo_status={loaded.status}",
        f"areas={loaded.areas}",
        f"precincts={loaded.precincts}",
        f"removed_areas={loaded.removed_areas}",
        f"removed_precincts={loaded.removed_precincts}",
    ]
    if aliases is not None:
        lines += [
            f"alias_pairs={aliases.pairs}",
            f"alias_matched_pairs={aliases.matched_pairs}",
            f"alias_unmatched_pairs={aliases.unmatched_pairs}",
            f"alias_matched_row_share={aliases.matched_row_share:.4f}",
            f"alias_written={aliases.written}",
            f"alias_removed={aliases.removed}",
        ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
