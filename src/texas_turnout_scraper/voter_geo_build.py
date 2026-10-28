"""Place every live roster VUID in a county, a state precinct and its districts, in D1.

Loader-only (``loader`` extra): imports ``voter_geo_models`` (sqlmodel). Run from the owner's
machine; it is not in the scheduled sync (ADR-015). Steps:

1. Read the geography ADR-010 loaded (county names and the roster-pair aliases) and the build
   signature (the roster version rows, the ``tx_geo`` hash and a hash of the alias rows). On an
   apply whose stored signature matches, stop: nothing else is read or sent.
2. Read the live roster rows one day (ascending) and one row-hash range (in key order) at a time.
   A voter's first appearance is the earliest day, then the lowest ``(row_hash, occ)`` (ADR-012);
   later listings only set ``flags`` when they name another precinct or county.
3. Derive one row per VUID, compare its ``src_hash`` with D1, upsert only new or changed rows and
   delete rows whose VUID is no longer live, in bounded ``json_each`` batches. Meta is written
   last, so an interrupted build is redone in full by the next run.

A dry run does steps 1 to 3 without the early stop, compares against D1 when the tables exist,
creates nothing and writes nothing. Logs and output carry counts only, never VUIDs, counties or
precincts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Literal, NamedTuple, Protocol

from pydantic import ValidationError

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_roster_rows import day_number, key_ranges
from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    EarlyVoteSyncError,
    _cloudflare_config_from_env,
)
from texas_turnout_scraper.geo_load import county_key
from texas_turnout_scraper.geo_tables import (
    ALIASES_SQL,
    COUNTY_NAMES_SQL,
    GEO_TABLE_COUNT,
    GEO_TABLES_PRESENT_SQL,
    META_NAME,
    ROSTER_DAYS_SQL,
)
from texas_turnout_scraper.geo_tables import META_SELECT_SQL as GEO_META_SELECT_SQL
from texas_turnout_scraper.repeat_tables import ROSTER_VERSIONS_SQL
from texas_turnout_scraper.voter_geo_models import VoterGeo, VoterGeoMeta
from texas_turnout_scraper.voter_geo_tables import (
    FLAG_MULTI_COUNTY,
    FLAG_MULTI_PRECINCT,
    META_COUNT_NAMES,
    PAGE_ROWS,
    PRECINCT_DISTRICTS_SQL,
    SPLIT_CD,
    SPLIT_HD,
    SPLIT_SD,
    VOTER_GEO_COLUMNS,
    VOTER_GEO_DELETE_SQL,
    VOTER_GEO_LIVE_ROWS_SQL,
    VOTER_GEO_META_SELECT_SQL,
    VOTER_GEO_META_UPSERT_SQL,
    VOTER_GEO_PRESENT_SQL,
    VOTER_GEO_STORED_HASHES_SQL,
    VOTER_GEO_TABLE_COUNT,
    VOTER_GEO_UPSERT_SQL,
    VOTER_GEO_VERSION,
    VUID_WIDTH,
)
from texas_turnout_scraper.voter_link import payload_batches
from texas_turnout_scraper.voter_scores import src_hash
from texas_turnout_scraper.voterfile import normalize_vuid

logger = logging.getLogger(__name__)

VOTER_GEO_ROWS_PER_STATEMENT = 2000
# Each roster read covers one day and 1/VOTER_GEO_KEY_RANGES of the row-hash space.
VOTER_GEO_KEY_RANGES = 16

BuildStatus = Literal["dry_run", "wrote", "unchanged", "geo_not_loaded"]
Pair = tuple[str, str]
_FLAG_SHIFT = 2  # a voter's state is pair_id << 2 | flags (two flag bits)


class VoterGeoStore(Protocol):
    def _query_rows(
        self, database_id: str, sql: str, params: list[str]
    ) -> list[dict[str, object]]: ...

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None: ...

    def _database_uuid_if_exists(self, election_id: str) -> str | None: ...

    def _ensure_schema(self, database_id: str) -> None: ...


class _CountingStore:
    """Forwards to the store and counts the read queries the build issues."""

    def __init__(self, inner: VoterGeoStore) -> None:
        self._inner = inner
        self.queries = 0

    def _query_rows(self, database_id: str, sql: str, params: list[str]) -> list[dict[str, object]]:
        self.queries += 1
        return self._inner._query_rows(database_id, sql, params)  # noqa: SLF001

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None:
        self._inner._statements(database_id, statements)  # noqa: SLF001

    def _database_uuid_if_exists(self, election_id: str) -> str | None:
        return self._inner._database_uuid_if_exists(election_id)  # noqa: SLF001

    def _ensure_schema(self, database_id: str) -> None:
        self._inner._ensure_schema(database_id)  # noqa: SLF001


@dataclass(frozen=True)
class VoterGeoResult:
    """Counts of one build. ``rows_pending`` and ``rows_stale`` are what differs from D1."""

    status: BuildStatus
    live_rows: int = 0
    vuids: int = 0
    vuid_normalized_changed: int = 0
    matched_rows: int = 0
    matched_vuids: int = 0
    unmatched_alias_vuids: int = 0
    no_alias_vuids: int = 0
    unknown_county_vuids: int = 0
    null_cd_vuids: int = 0
    null_sd_vuids: int = 0
    null_hd_vuids: int = 0
    split_vuids: int = 0
    multi_precinct_vuids: int = 0
    multi_county_vuids: int = 0
    rows_pending: int = 0
    rows_stale: int = 0
    statements: int = 0
    queries: int = 0
    seconds: float = 0.0

    @property
    def matched_row_share(self) -> float:
        """Live rows whose roster pair has a state precinct; ADR-010 measured 99.3%."""
        return self.matched_rows / self.live_rows if self.live_rows else 0.0

    @property
    def rows_written(self) -> int:
        return self.rows_pending if self.status == "wrote" else 0

    @property
    def rows_deleted(self) -> int:
        return self.rows_stale if self.status == "wrote" else 0


@dataclass(frozen=True)
class Geography:
    """What ADR-010 loaded: county codes by name, districts by state precinct, aliases by pair."""

    county_fips: Mapping[str, str]
    districts: Mapping[str, tuple[str | None, str | None, str | None, int]]
    aliases: Mapping[Pair, str | None]


class Place(NamedTuple):
    """What one roster ``(county, precinct)`` pair resolves to."""

    county_fips: str | None
    pctkey: str | None
    cd: str | None
    sd: str | None
    hd: str | None
    split_flag: int | None
    alias: Literal["matched", "unmatched_alias", "no_alias"]


@dataclass(frozen=True)
class RosterScan:
    """First listing of every live VUID with the flags later listings add, and pair row counts."""

    live_rows: int
    normalized_changed: int
    voters: dict[str, int]
    pairs: list[Pair]
    pair_rows: list[int]


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


def _truthy(value: object) -> bool:
    return value is not None and str(value).lower() in {"1", "true"}


def _dump(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count(store: VoterGeoStore, database_id: str, sql: str) -> int:
    rows = store._query_rows(database_id, sql, [])  # noqa: SLF001
    return int(_text(rows[0].get("n")) or 0) if rows else 0


def read_aliases(store: VoterGeoStore, database_id: str) -> dict[Pair, str | None]:
    """The stored alias of every roster pair; ``None`` when the alias is ``unmatched``."""
    return {
        (_text(row.get("county")), _text(row.get("precinct"))): _optional(row.get("pctkey"))
        for row in store._query_rows(database_id, ALIASES_SQL, [])  # noqa: SLF001
    }


def read_geography(
    store: VoterGeoStore, database_id: str, aliases: Mapping[Pair, str | None]
) -> Geography:
    counties: dict[str, str] = {}
    for row in store._query_rows(database_id, COUNTY_NAMES_SQL, []):  # noqa: SLF001
        counties.setdefault(county_key(_text(row.get("name"))), _text(row.get("code")))
    districts: dict[str, tuple[str | None, str | None, str | None, int]] = {}
    for row in store._query_rows(database_id, PRECINCT_DISTRICTS_SQL, []):  # noqa: SLF001
        split = (
            (SPLIT_CD if _truthy(row.get("split_cd")) else 0)
            | (SPLIT_SD if _truthy(row.get("split_sd")) else 0)
            | (SPLIT_HD if _truthy(row.get("split_hd")) else 0)
        )
        districts[_text(row.get("pctkey"))] = (
            _optional(row.get("cd")),
            _optional(row.get("sd")),
            _optional(row.get("hd")),
            split,
        )
    return Geography(counties, districts, aliases)


def place_of(pair: Pair, geography: Geography) -> Place:
    """County from the name's letters, precinct from the stored alias of the exact pair."""
    fips = geography.county_fips.get(county_key(pair[0]))
    if pair not in geography.aliases:
        return Place(fips, None, None, None, None, None, "no_alias")
    pctkey = geography.aliases[pair]
    if pctkey is None:
        return Place(fips, None, None, None, None, None, "unmatched_alias")
    cd, sd, hd, split = geography.districts.get(pctkey, (None, None, None, 0))
    return Place(fips, pctkey, cd, sd, hd, split, "matched")


def build_signature(
    store: VoterGeoStore,
    database_id: str,
    election_id: str,
    aliases: Mapping[Pair, str | None],
) -> str:
    """sha256 of the version, the roster versions, the ``tx_geo`` hash and the alias rows."""
    versions = sorted(
        tuple(
            _text(row.get(c)) for c in ("ev_date", "version", "sha256", "row_count", "reject_count")
        )
        for row in store._query_rows(database_id, ROSTER_VERSIONS_SQL, [election_id])  # noqa: SLF001
    )
    geo_rows = store._query_rows(database_id, GEO_META_SELECT_SQL, [META_NAME])  # noqa: SLF001
    geo_sha = _text(geo_rows[0].get("sha256")) if geo_rows else ""
    alias_hash = hashlib.sha256(
        _dump(
            sorted([county, precinct, pctkey] for (county, precinct), pctkey in aliases.items())
        ).encode()
    ).hexdigest()
    payload = [VOTER_GEO_VERSION, versions, geo_sha, alias_hash]
    return hashlib.sha256(_dump(payload).encode()).hexdigest()


def _vuid_of(raw: str) -> str:
    # Ten ASCII digits are already normal; the fast path skips the per-character join.
    if len(raw) == VUID_WIDTH and raw.isascii() and raw.isdigit():
        return raw
    return normalize_vuid(raw)


def scan_roster(
    store: VoterGeoStore,
    database_id: str,
    election_id: str,
    *,
    ranges: int = VOTER_GEO_KEY_RANGES,
) -> RosterScan:
    """One pass over the live rows in first-appearance order: day ascending, then key order."""
    pair_ids: dict[Pair, int] = {}
    pairs: list[Pair] = []
    pair_rows: list[int] = []
    voters: dict[str, int] = {}
    live = changed = 0
    for day_row in store._query_rows(database_id, ROSTER_DAYS_SQL, [election_id]):  # noqa: SLF001
        day = str(day_number(_text(day_row.get("ev_date"))))
        for low, high in key_ranges(ranges):
            rows = store._query_rows(database_id, VOTER_GEO_LIVE_ROWS_SQL, [day, low, high])  # noqa: SLF001
            for row in rows:
                live += 1
                raw = _text(row.get("id_voter"))
                vuid = _vuid_of(raw)
                changed += vuid != raw
                pair = (_text(row.get("county")), _text(row.get("precinct")))
                pid = pair_ids.get(pair)
                if pid is None:
                    pid = pair_ids[pair] = len(pairs)
                    pairs.append(pair)
                    pair_rows.append(0)
                pair_rows[pid] += 1
                state = voters.get(vuid)
                if state is None:
                    voters[vuid] = pid << _FLAG_SHIFT
                    continue
                first = state >> _FLAG_SHIFT
                if pid != first:
                    more = FLAG_MULTI_PRECINCT | (
                        FLAG_MULTI_COUNTY if pairs[first][0] != pair[0] else 0
                    )
                    voters[vuid] = state | more
    return RosterScan(live, changed, voters, pairs, pair_rows)


def _reason(error: ValidationError) -> str:
    return ",".join(
        f"{'.'.join(str(part) for part in item['loc'])}:{item['type']}" for item in error.errors()
    )


def voter_geo_record(vuid: str, place: Place, flags: int, hashed: int, matched_at: str) -> VoterGeo:
    """The row for one VUID. Raises ``EarlyVoteSyncError`` naming fields only."""
    fields_ = {
        "vuid": vuid,
        "county_fips": place.county_fips,
        "pctkey": place.pctkey,
        "cd": place.cd,
        "sd": place.sd,
        "hd": place.hd,
        "split_flag": place.split_flag,
        "flags": flags,
        "src_hash": hashed,
        "matched_at": matched_at,
    }
    try:
        return VoterGeo.model_validate(fields_)
    except ValidationError as error:
        raise EarlyVoteSyncError(f"voter_geo row failed validation: {_reason(error)}") from None


def derived_hash(vuid: str, place: Place, flags: int) -> int:
    return src_hash(
        [
            VOTER_GEO_VERSION,
            vuid,
            place.county_fips,
            place.pctkey,
            place.cd,
            place.sd,
            place.hd,
            place.split_flag,
            flags,
        ]
    )


class _Sender:
    """Bounded statements, each carrying one ``json_each`` payload.

    A dry run sends nothing and counts the statements an apply would send.
    """

    def __init__(
        self, store: VoterGeoStore, database_id: str, *, dry_run: bool, batch_rows: int
    ) -> None:
        self.store, self.database_id = store, database_id
        self.dry_run, self.batch_rows = dry_run, batch_rows
        self.statements = 0

    def send(self, sql: str, rows: Sequence[object]) -> None:
        if not rows:
            return
        if self.dry_run:
            self.statements += -(-len(rows) // self.batch_rows)
            return
        for payload in payload_batches(rows, batch_rows=self.batch_rows):
            self.store._statements(self.database_id, [(sql, [payload])])  # noqa: SLF001
            self.statements += 1


class _Buffer:
    """Rows for one statement, flushed the moment ``batch_rows`` of them are waiting."""

    def __init__(self, sender: _Sender, sql: str) -> None:
        self.sender, self.sql = sender, sql
        self.rows: list[object] = []
        self.count = 0

    def add(self, row: object) -> None:
        self.count += 1
        if self.sender.dry_run:
            return
        self.rows.append(row)
        if len(self.rows) >= self.sender.batch_rows:
            self.flush()

    def flush(self) -> None:
        self.sender.send(self.sql, self.rows)
        self.rows = []

    def finish(self) -> None:
        if self.sender.dry_run:
            self.sender.statements += -(-self.count // self.sender.batch_rows)
        else:
            self.flush()


def _stored_hashes(store: VoterGeoStore, database_id: str) -> dict[str, int]:
    hashes: dict[str, int] = {}
    after = ""
    while True:
        page = store._query_rows(database_id, VOTER_GEO_STORED_HASHES_SQL, [after])  # noqa: SLF001
        for row in page:
            hashes[_text(row.get("vuid"))] = int(_text(row.get("src_hash")))
        if len(page) < PAGE_ROWS:
            return hashes
        after = _text(page[-1].get("vuid"))


def _stored_meta(store: VoterGeoStore, database_id: str) -> dict[str, str]:
    return {
        _text(row.get("name")): _text(row.get("value"))
        for row in store._query_rows(database_id, VOTER_GEO_META_SELECT_SQL, [])  # noqa: SLF001
    }


def _tables_present(store: VoterGeoStore, database_id: str) -> bool:
    return _count(store, database_id, VOTER_GEO_PRESENT_SQL) == VOTER_GEO_TABLE_COUNT


def _geo_present(store: VoterGeoStore, database_id: str) -> bool:
    return _count(store, database_id, GEO_TABLES_PRESENT_SQL) == GEO_TABLE_COUNT


def build_voter_geo(  # noqa: PLR0913
    store: VoterGeoStore,
    database_id: str,
    election_id: str,
    signature: str,
    geography: Geography,
    *,
    dry_run: bool,
    tables_present: bool = True,
    batch_rows: int = VOTER_GEO_ROWS_PER_STATEMENT,
    matched_at: str,
) -> VoterGeoResult:
    """Bring ``voter_geo`` to the live roster and the loaded geography; see the module."""
    scan = scan_roster(store, database_id, election_id)
    places = [place_of(pair, geography) for pair in scan.pairs]
    hashes = _stored_hashes(store, database_id) if tables_present else {}
    sender = _Sender(store, database_id, dry_run=dry_run, batch_rows=batch_rows)
    upserts = _Buffer(sender, VOTER_GEO_UPSERT_SQL)
    tally = dict.fromkeys(META_COUNT_NAMES, 0)
    failed, first_failure = 0, ""
    for vuid in sorted(scan.voters):
        state = scan.voters[vuid]
        place, flags = (
            places[state >> _FLAG_SHIFT],
            state & (FLAG_MULTI_PRECINCT | FLAG_MULTI_COUNTY),
        )
        _tally(tally, place, flags)
        hashed = derived_hash(vuid, place, flags)
        if hashes.pop(vuid, None) == hashed:
            continue
        try:
            record = voter_geo_record(vuid, place, flags, hashed, matched_at)
        except EarlyVoteSyncError as error:
            failed += 1
            first_failure = first_failure or str(error)
            continue
        upserts.add([getattr(record, column) for column in VOTER_GEO_COLUMNS])
    if failed:
        raise EarlyVoteSyncError(
            f"{failed} voter_geo rows failed validation; first: {first_failure}"
        )
    upserts.finish()
    stale = sorted(hashes)
    sender.send(VOTER_GEO_DELETE_SQL, stale)
    tally["live_rows"] = scan.live_rows
    tally["vuids"] = len(scan.voters)
    tally["vuid_normalized_changed"] = scan.normalized_changed
    tally["matched_rows"] = sum(
        n for n, place in zip(scan.pair_rows, places, strict=True) if place.pctkey is not None
    )
    meta = {"signature": signature, **{name: str(n) for name, n in tally.items()}}
    for name, value in meta.items():
        VoterGeoMeta.model_validate({"name": name, "value": value})
    sender.send(VOTER_GEO_META_UPSERT_SQL, sorted([name, value] for name, value in meta.items()))
    return VoterGeoResult(
        "dry_run" if dry_run else "wrote",
        **tally,
        rows_pending=upserts.count,
        rows_stale=len(stale),
        statements=sender.statements,
    )


def _tally(tally: dict[str, int], place: Place, flags: int) -> None:
    tally["matched_vuids"] += place.pctkey is not None
    tally["unmatched_alias_vuids"] += place.alias == "unmatched_alias"
    tally["no_alias_vuids"] += place.alias == "no_alias"
    tally["unknown_county_vuids"] += place.county_fips is None
    if place.pctkey is not None:
        tally["null_cd_vuids"] += place.cd is None
        tally["null_sd_vuids"] += place.sd is None
        tally["null_hd_vuids"] += place.hd is None
    tally["split_vuids"] += bool(place.split_flag)
    tally["multi_precinct_vuids"] += bool(flags & FLAG_MULTI_PRECINCT)
    tally["multi_county_vuids"] += bool(flags & FLAG_MULTI_COUNTY)


def _counts_from_meta(meta: Mapping[str, str]) -> dict[str, int]:
    return {name: int(meta.get(name) or 0) for name in META_COUNT_NAMES}


def _log(election_id: str, result: VoterGeoResult) -> None:
    summary = " ".join(f"{f.name}={getattr(result, f.name)}" for f in fields(result))
    logger.info(
        "voter_geo build election_id=%s %s matched_row_share=%.4f",
        election_id,
        summary,
        result.matched_row_share,
    )


def run_voter_geo_build_on_store(
    store: VoterGeoStore,
    election_id: str,
    *,
    dry_run: bool,
    batch_rows: int = VOTER_GEO_ROWS_PER_STATEMENT,
    now: datetime | None = None,
) -> VoterGeoResult | None:
    """The whole build for one election. Without an election database there is nothing to do."""
    started = time.monotonic()
    election_id = require_election_id(election_id)
    counted = _CountingStore(store)
    database_id = counted._database_uuid_if_exists(election_id)  # noqa: SLF001
    if database_id is None:
        logger.info("voter_geo build election_id=%s has no D1 database", election_id)
        return None
    if dry_run:
        present = _tables_present(counted, database_id)
        if not _geo_present(counted, database_id):
            logger.info("voter_geo build dry run: geography tables absent, nothing is read")
            return replace(
                VoterGeoResult("geo_not_loaded"),
                queries=counted.queries,
                seconds=time.monotonic() - started,
            )
        if not present:
            logger.info("voter_geo build dry run: voter_geo tables absent, nothing is created")
    else:
        counted._ensure_schema(database_id)  # noqa: SLF001
        present = True
    aliases = read_aliases(counted, database_id)
    geography = read_geography(counted, database_id, aliases)
    if not dry_run and not (geography.county_fips and aliases):
        raise EarlyVoteSyncError("geography is not loaded; run geo_load before voter_geo_build")
    signature = build_signature(counted, database_id, election_id, aliases)
    stored = _stored_meta(counted, database_id) if present else {}
    if not dry_run and stored.get("signature") == signature:
        result = VoterGeoResult("unchanged", **_counts_from_meta(stored))
    else:
        result = build_voter_geo(
            counted,
            database_id,
            election_id,
            signature,
            geography,
            dry_run=dry_run,
            tables_present=present,
            batch_rows=batch_rows,
            matched_at=_stamp(now or datetime.now(timezone.utc)),
        )
    result = replace(result, queries=counted.queries, seconds=time.monotonic() - started)
    _log(election_id, result)
    return result


def output_lines(result: VoterGeoResult) -> list[str]:
    """``key=value`` lines of counts only."""
    names = (
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
    )
    return [
        f"status={result.status}",
        *(f"{name}={getattr(result, name)}" for name in names),
        f"matched_row_share={result.matched_row_share:.4f}",
        f"rows_pending={result.rows_pending}",
        f"rows_stale={result.rows_stale}",
        f"statements={result.statements}",
        f"queries={result.queries}",
        f"seconds={result.seconds:.1f}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Place every live roster VUID in D1 by precinct.")
    parser.add_argument("--election-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run == args.apply:
        parser.error("pass exactly one of --dry-run or --apply")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = _cloudflare_config_from_env()
    if config is None:
        raise EarlyVoteSyncError(
            "voter_geo build requires CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, "
            "and CLOUDFLARE_KV_NAMESPACE_ID"
        )
    account_id, api_token, namespace_id = config
    store = CloudflareEarlyVoteStore(
        account_id=account_id, api_token=api_token, kv_namespace_id=namespace_id
    )
    try:
        result = run_voter_geo_build_on_store(store, args.election_id, dry_run=args.dry_run)
    finally:
        store.close()
    if result is None:
        sys.stdout.write("status=no_database\n")
        return 0
    sys.stdout.write("\n".join(output_lines(result)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
