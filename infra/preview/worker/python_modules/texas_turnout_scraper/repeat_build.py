"""Flag VUIDs counted more than once or removed, and count the excess rows each slice holds.

Loader-only (``loader`` extra): imports ``repeat_models`` (sqlmodel). Runs in the scheduled sync
after the geography step. Steps:

1. Read the build signature (the roster version rows and the change log's size and last entry).
   On an apply whose stored signature matches, stop: nothing is read or sent.
2. Read the live roster rows one day (ascending) and one row-hash range (in key order) at a time.
   A voter's first appearance is the earliest day, then the lowest ``(row_hash, occ)``; every
   later appearance counts one excess row at its ``(day, county, method)``.
3. Read the change log's removal events, one day and one keyset page at a time.
4. Build one row per voter counted more than once or removed at least once, compare its
   ``src_hash`` with D1, upsert only new or changed rows and delete rows that no longer qualify,
   in bounded ``json_each`` batches. The excess table is replaced by diff. Meta is written last,
   so an interrupted build is redone in full by the next run.

A dry run does steps 1 to 4 without the early stop, compares against D1 when the tables exist,
creates nothing and writes nothing. Logs and output carry counts only, never VUIDs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import ValidationError

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_roster_rows import day_number, key_ranges
from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    EarlyVoteSyncError,
    _cloudflare_config_from_env,
)
from texas_turnout_scraper.geo_tables import ROSTER_DAYS_SQL
from texas_turnout_scraper.repeat_models import RepeatExcess, RepeatMeta, VuidRepeat
from texas_turnout_scraper.repeat_tables import (
    CHANGE_LOG_SQL,
    EXCESS_DELETE_SQL,
    EXCESS_SELECT_SQL,
    EXCESS_UPSERT_SQL,
    FLAG_CROSS_DAY,
    FLAG_CROSS_METHOD,
    FLAG_REMOVED,
    FLAG_SAME_DAY,
    LIVE_ROWS_SQL,
    META_NAMES,
    META_SELECT_SQL,
    META_UPSERT_SQL,
    NEXT_CHANGE_DAY_SQL,
    PAGE_ROWS,
    REMOVALS_SQL,
    REMOVED_ROWS_SQL,
    REPEAT_COLUMNS,
    REPEAT_DELETE_SQL,
    REPEAT_TABLE_COUNT,
    REPEAT_TABLES_PRESENT_SQL,
    REPEAT_UPSERT_SQL,
    REPEAT_VERSION,
    ROSTER_VERSIONS_SQL,
    STORED_HASHES_SQL,
    VUID_WIDTH,
)
from texas_turnout_scraper.voter_link import payload_batches
from texas_turnout_scraper.voter_scores import src_hash

logger = logging.getLogger(__name__)

REPEAT_ROWS_PER_STATEMENT = 2000
# Each roster read covers one day and 1/REPEAT_KEY_RANGES of the row-hash space (about 21k rows
# of a 340k-row day).
REPEAT_KEY_RANGES = 16

BuildStatus = Literal["dry_run", "wrote", "unchanged"]
# (ev_date, county, voting_method). Interned: every voter at the same coordinates holds one tuple.
Coord = tuple[str, str, str]


class RepeatStore(Protocol):
    def _query_rows(
        self, database_id: str, sql: str, params: list[str]
    ) -> list[dict[str, object]]: ...

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None: ...


class RepeatBuildStore(RepeatStore, Protocol):
    def _database_uuid_if_exists(self, election_id: str) -> str | None: ...

    def _ensure_schema(self, database_id: str) -> None: ...


@dataclass(frozen=True)
class RepeatResult:
    """Counts of one build. ``*_pending`` and ``*_stale`` are what differs from D1."""

    status: BuildStatus
    raw_rows: int = 0
    distinct_voters: int = 0
    repeat_voters: int = 0
    excess_rows: int = 0
    removed_voters: int = 0
    removed_rows: int | None = None
    rows_pending: int = 0
    rows_stale: int = 0
    excess_pending: int = 0
    excess_stale: int = 0

    @property
    def rows_written(self) -> int:
        return self.rows_pending if self.status == "wrote" else 0

    @property
    def rows_deleted(self) -> int:
        return self.rows_stale if self.status == "wrote" else 0


@dataclass(frozen=True)
class RosterScan:
    """First appearances of every live voter, all appearances of repeated ones, excess counts."""

    raw_rows: int
    first: dict[str, Coord]
    repeats: dict[str, list[Coord]]
    excess: dict[Coord, int]


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _vuid_key(value: str) -> str:
    return value.strip().zfill(VUID_WIDTH)


def _dump(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def build_signature(store: RepeatStore, database_id: str, election_id: str) -> str:
    """sha256 of the roster versions and the change log's size and last entry."""
    versions = sorted(
        tuple(
            _text(row.get(c)) for c in ("ev_date", "version", "sha256", "row_count", "reject_count")
        )
        for row in store._query_rows(database_id, ROSTER_VERSIONS_SQL, [election_id])  # noqa: SLF001
    )
    log = store._query_rows(database_id, CHANGE_LOG_SQL, [election_id])  # noqa: SLF001
    head = log[0] if log else {}
    payload = [REPEAT_VERSION, versions, _text(head.get("n")), _text(head.get("last_recorded"))]
    return hashlib.sha256(_dump(payload).encode()).hexdigest()


def scan_roster(
    store: RepeatStore, database_id: str, election_id: str, *, ranges: int = REPEAT_KEY_RANGES
) -> RosterScan:
    """One pass over the live rows in first-appearance order: day ascending, then key order."""
    coords: dict[Coord, Coord] = {}
    first: dict[str, Coord] = {}
    repeats: dict[str, list[Coord]] = {}
    excess: dict[Coord, int] = {}
    raw = 0
    for day_row in store._query_rows(database_id, ROSTER_DAYS_SQL, [election_id]):  # noqa: SLF001
        ev_date = _text(day_row.get("ev_date"))
        day = str(day_number(ev_date))
        for low, high in key_ranges(ranges):
            for row in store._query_rows(database_id, LIVE_ROWS_SQL, [day, low, high]):  # noqa: SLF001
                raw += 1
                seen = (ev_date, _text(row.get("county")), _text(row.get("voting_method")))
                coord = coords.setdefault(seen, seen)
                vuid = _vuid_key(_text(row.get("id_voter")))
                earlier = first.get(vuid)
                if earlier is None:
                    first[vuid] = coord
                    continue
                appearances = repeats.get(vuid)
                if appearances is None:
                    repeats[vuid] = [earlier, coord]
                else:
                    appearances.append(coord)
                excess[coord] = excess.get(coord, 0) + 1
    return RosterScan(raw, first, repeats, excess)


def read_removals(store: RepeatStore, database_id: str, election_id: str) -> dict[str, list[str]]:
    """The day of every removal event in the change log, per VUID, days ascending."""
    removed: dict[str, list[str]] = {}
    ev_date = ""
    while True:
        rows = store._query_rows(database_id, NEXT_CHANGE_DAY_SQL, [election_id, ev_date])  # noqa: SLF001
        if not rows or rows[0].get("ev_date") is None:
            return removed
        ev_date = _text(rows[0].get("ev_date"))
        after = ["", ""]
        while True:
            page = store._query_rows(  # noqa: SLF001
                database_id, REMOVALS_SQL, [election_id, ev_date, *after]
            )
            for row in page:
                removed.setdefault(_vuid_key(_text(row.get("id_voter"))), []).append(ev_date)
            if len(page) < PAGE_ROWS:
                break
            after = [_text(page[-1].get("new_sha256")), _text(page[-1].get("id_voter"))]


def _reason(error: ValidationError) -> str:
    return ",".join(
        f"{'.'.join(str(part) for part in item['loc'])}:{item['type']}" for item in error.errors()
    )


def repeat_record(vuid: str, appearances: Sequence[Coord], removals: Sequence[str]) -> VuidRepeat:
    """The row for one voter. Raises ``EarlyVoteSyncError`` naming fields only."""
    counted_days = sorted({day for day, _, _ in appearances})
    flags = (
        (FLAG_SAME_DAY if len(counted_days) < len(appearances) else 0)
        | (FLAG_CROSS_DAY if len(counted_days) > 1 else 0)
        | (FLAG_CROSS_METHOD if len({method for _, _, method in appearances}) > 1 else 0)
        | (FLAG_REMOVED if removals else 0)
    )
    first_day = appearances[0][0] if appearances else None
    items = [{"day": d, "method": m, "county": c} for d, c, m in appearances]
    removed_days = sorted(set(removals))
    n_counted, n_removed = len(appearances), len(removals)
    hashed = src_hash(
        [
            REPEAT_VERSION,
            vuid,
            n_counted,
            n_removed,
            first_day,
            _dump(counted_days),
            _dump(items),
            _dump(removed_days),
            flags,
        ]
    )
    fields = {
        "vuid": vuid,
        "n_counted": n_counted,
        "n_removed": n_removed,
        "first_day": first_day,
        "counted_days": counted_days,
        "appearances": items,
        "removed_days": removed_days,
        "flags": flags,
        "src_hash": hashed,
    }
    try:
        return VuidRepeat.model_validate(fields)
    except ValidationError as error:
        raise EarlyVoteSyncError(f"repeat row failed validation: {_reason(error)}") from None


def _validated_excess(rows: list[list[object]]) -> list[list[object]]:
    keys = ("ev_date", "county", "voting_method", "n")
    try:
        for row in rows:
            RepeatExcess.model_validate(dict(zip(keys, row, strict=True)))
    except ValidationError as error:
        raise EarlyVoteSyncError(f"repeat excess failed validation: {_reason(error)}") from None
    return rows


class _Writes:
    """Bounded statements, each carrying one ``json_each`` payload. A dry run sends nothing."""

    def __init__(
        self, store: RepeatStore, database_id: str, *, dry_run: bool, batch_rows: int
    ) -> None:
        self.store, self.database_id = store, database_id
        self.dry_run, self.batch_rows = dry_run, batch_rows

    def send(self, sql: str, rows: Sequence[object]) -> None:
        if self.dry_run or not rows:
            return
        for payload in payload_batches(rows, batch_rows=self.batch_rows):
            self.store._statements(self.database_id, [(sql, [payload])])  # noqa: SLF001


class _Buffer:
    """Rows for one statement, flushed the moment ``batch_rows`` of them are waiting."""

    def __init__(self, writes: _Writes, sql: str) -> None:
        self.writes, self.sql = writes, sql
        self.rows: list[object] = []
        self.count = 0

    def add(self, row: object) -> None:
        self.count += 1
        if self.writes.dry_run:
            return
        self.rows.append(row)
        if len(self.rows) >= self.writes.batch_rows:
            self.flush()

    def flush(self) -> None:
        self.writes.send(self.sql, self.rows)
        self.rows = []


def _stored_hashes(store: RepeatStore, database_id: str) -> dict[str, int]:
    hashes: dict[str, int] = {}
    after = ""
    while True:
        page = store._query_rows(database_id, STORED_HASHES_SQL, [after])  # noqa: SLF001
        for row in page:
            hashes[_text(row.get("vuid"))] = int(_text(row.get("src_hash")))
        if len(page) < PAGE_ROWS:
            return hashes
        after = _text(page[-1].get("vuid"))


def _stored_excess(store: RepeatStore, database_id: str) -> dict[Coord, int]:
    return {
        (_text(row.get("ev_date")), _text(row.get("county")), _text(row.get("voting_method"))): int(
            _text(row.get("n"))
        )
        for row in store._query_rows(database_id, EXCESS_SELECT_SQL, [])  # noqa: SLF001
    }


def _stored_meta(store: RepeatStore, database_id: str) -> dict[str, str]:
    return {
        _text(row.get("name")): _text(row.get("value"))
        for row in store._query_rows(database_id, META_SELECT_SQL, [])  # noqa: SLF001
    }


def _tables_present(store: RepeatStore, database_id: str) -> bool:
    rows = store._query_rows(database_id, REPEAT_TABLES_PRESENT_SQL, [])  # noqa: SLF001
    return bool(rows) and int(_text(rows[0].get("n")) or 0) == REPEAT_TABLE_COUNT


def build_repeats(
    store: RepeatStore,
    database_id: str,
    election_id: str,
    signature: str,
    *,
    dry_run: bool,
    tables_present: bool = True,
    batch_rows: int = REPEAT_ROWS_PER_STATEMENT,
) -> RepeatResult:
    """Bring the repeat tables to the live roster and the change log; see the module."""
    scan = scan_roster(store, database_id, election_id)
    removed = read_removals(store, database_id, election_id)
    hashes = _stored_hashes(store, database_id) if tables_present else {}
    stored_excess = _stored_excess(store, database_id) if tables_present else {}
    writes = _Writes(store, database_id, dry_run=dry_run, batch_rows=batch_rows)
    upserts = _Buffer(writes, REPEAT_UPSERT_SQL)
    failed, first_failure = 0, ""
    for vuid in sorted(scan.repeats.keys() | removed.keys()):
        appearances = scan.repeats.get(vuid)
        if appearances is None:
            first = scan.first.get(vuid)
            appearances = [] if first is None else [first]
        try:
            record = repeat_record(vuid, appearances, removed.get(vuid, ()))
        except EarlyVoteSyncError as error:
            failed += 1
            first_failure = first_failure or str(error)
            continue
        if hashes.pop(vuid, None) != record.src_hash:
            upserts.add([getattr(record, column) for column in REPEAT_COLUMNS])
    if failed:
        raise EarlyVoteSyncError(f"{failed} repeat rows failed validation; first: {first_failure}")
    upserts.flush()
    stale = sorted(hashes)
    writes.send(REPEAT_DELETE_SQL, stale)
    changed_excess = _validated_excess(
        sorted([*coord, n] for coord, n in scan.excess.items() if stored_excess.get(coord) != n)
    )
    stale_excess = sorted([*coord] for coord in stored_excess if coord not in scan.excess)
    writes.send(EXCESS_UPSERT_SQL, changed_excess)
    writes.send(EXCESS_DELETE_SQL, stale_excess)
    counts = {
        "raw_rows": scan.raw_rows,
        "distinct_voters": len(scan.first),
        "repeat_voters": len(scan.repeats),
        "excess_rows": sum(scan.excess.values()),
        "removed_voters": len(removed),
    }
    meta = {"signature": signature, **{name: str(n) for name, n in counts.items()}}
    for name, value in meta.items():
        RepeatMeta.model_validate({"name": name, "value": value})
    writes.send(META_UPSERT_SQL, sorted([name, value] for name, value in meta.items()))
    removed_rows = _query_count(store, database_id, REMOVED_ROWS_SQL)
    return RepeatResult(
        "dry_run" if dry_run else "wrote",
        **counts,
        removed_rows=removed_rows,
        rows_pending=upserts.count,
        rows_stale=len(stale),
        excess_pending=len(changed_excess),
        excess_stale=len(stale_excess),
    )


def _query_count(store: RepeatStore, database_id: str, sql: str) -> int:
    rows = store._query_rows(database_id, sql, [])  # noqa: SLF001
    return int(rows[0].get("n") or 0) if rows else 0


def _counts_from_meta(meta: Mapping[str, str]) -> dict[str, int]:
    return {name: int(meta.get(name) or 0) for name in META_NAMES if name != "signature"}


def run_repeat_build_on_store(
    store: RepeatBuildStore,
    election_id: str,
    *,
    dry_run: bool,
    batch_rows: int = REPEAT_ROWS_PER_STATEMENT,
) -> RepeatResult | None:
    """The whole build for one election. Without an election database there is nothing to do."""
    election_id = require_election_id(election_id)
    database_id = store._database_uuid_if_exists(election_id)  # noqa: SLF001
    if database_id is None:
        logger.info("repeat build election_id=%s has no D1 database", election_id)
        return None
    if dry_run:
        present = _tables_present(store, database_id)
        if not present:
            logger.info("repeat build dry run: repeat tables absent, nothing is created")
    else:
        store._ensure_schema(database_id)  # noqa: SLF001
        present = True
    signature = build_signature(store, database_id, election_id)
    stored = _stored_meta(store, database_id) if present else {}
    if not dry_run and stored.get("signature") == signature:
        result = RepeatResult("unchanged", **_counts_from_meta(stored))
    else:
        result = build_repeats(
            store,
            database_id,
            election_id,
            signature,
            dry_run=dry_run,
            tables_present=present,
            batch_rows=batch_rows,
        )
    logger.info(
        "repeat build election_id=%s status=%s raw_rows=%s distinct_voters=%s repeat_voters=%s "
        "excess_rows=%s removed_voters=%s removed_rows=%s rows_pending=%s rows_stale=%s "
        "excess_pending=%s excess_stale=%s",
        election_id,
        result.status,
        result.raw_rows,
        result.distinct_voters,
        result.repeat_voters,
        result.excess_rows,
        result.removed_voters,
        result.removed_rows,
        result.rows_pending,
        result.rows_stale,
        result.excess_pending,
        result.excess_stale,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build repeat-VUID flags and excess counts in D1.")
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
            "repeat build requires CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, "
            "and CLOUDFLARE_KV_NAMESPACE_ID"
        )
    account_id, api_token, namespace_id = config
    store = CloudflareEarlyVoteStore(
        account_id=account_id, api_token=api_token, kv_namespace_id=namespace_id
    )
    try:
        result = run_repeat_build_on_store(store, args.election_id, dry_run=args.dry_run)
    finally:
        store.close()
    if result is None:
        sys.stdout.write("status=no_database\n")
        return 0
    lines = [
        f"status={result.status}",
        f"raw_rows={result.raw_rows}",
        f"distinct_voters={result.distinct_voters}",
        f"repeat_voters={result.repeat_voters}",
        f"excess_rows={result.excess_rows}",
        f"removed_voters={result.removed_voters}",
        f"rows_pending={result.rows_pending}",
        f"rows_stale={result.rows_stale}",
        f"excess_pending={result.excess_pending}",
        f"excess_stale={result.excess_stale}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
