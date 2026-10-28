"""Build ``early_vote_first_seen`` in one election's D1 from its live roster rows.

Eleven set-based queries (one per VUID partition, see ``first_seen.PARTITIONS``) return counts
per first-appearance ``(day, county, method)``; the whole table is then replaced in one batch
with the signature written last. When the stored signature already matches the roster versions,
the run reads two small tables and stops. Output carries counts only, never a VUID.

    python -m texas_turnout_scraper.first_seen_build --election-id 53815 --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    EarlyVoteSyncError,
    _cloudflare_config_from_env,
)
from texas_turnout_scraper.first_seen import (
    PARTITIONS,
    SIGNATURE_SQL,
    TABLES_PRESENT_SQL,
    VERSIONS_SQL,
    Coord,
    add_partition_rows,
    replace_statements,
    signature,
)

logger = logging.getLogger(__name__)

Status = Literal["dry_run", "wrote", "unchanged"]


class FirstSeenStore(Protocol):
    def _query_rows(
        self, database_id: str, sql: str, params: list[str]
    ) -> list[dict[str, object]]: ...

    def _statements(self, database_id: str, statements: list[tuple[str, list[str]]]) -> None: ...

    def _database_uuid_if_exists(self, election_id: str) -> str | None: ...


@dataclass(frozen=True)
class FirstSeenResult:
    status: Status
    distinct_voters: int = 0
    coordinates: int = 0
    unknown_method_voters: int = 0


def _stored_signature(store: FirstSeenStore, database_id: str) -> str | None:
    present = store._query_rows(database_id, TABLES_PRESENT_SQL, [])  # noqa: SLF001
    if not present or int(str(present[0].get("n") or 0)) != 2:
        return None
    rows = store._query_rows(database_id, SIGNATURE_SQL, [])  # noqa: SLF001
    value = rows[0].get("value") if rows else None
    return value if isinstance(value, str) else None


def build_first_seen(
    store: FirstSeenStore, database_id: str, election_id: str, *, dry_run: bool
) -> FirstSeenResult:
    versions = store._query_rows(database_id, VERSIONS_SQL, [election_id])  # noqa: SLF001
    sig = signature(versions)
    if _stored_signature(store, database_id) == sig:
        return FirstSeenResult("unchanged")
    counts: dict[Coord, int] = {}
    unknown = 0
    for index, (sql, params) in enumerate(PARTITIONS):
        started = time.perf_counter()
        unknown += add_partition_rows(counts, store._query_rows(database_id, sql, params))  # noqa: SLF001
        logger.info(
            "first_seen partition=%s ms=%s", index, int((time.perf_counter() - started) * 1000)
        )
    if not dry_run:
        store._statements(database_id, replace_statements(counts, sig))  # noqa: SLF001
    return FirstSeenResult(
        "dry_run" if dry_run else "wrote",
        distinct_voters=sum(counts.values()),
        coordinates=len(counts),
        unknown_method_voters=unknown,
    )


def run_first_seen_build_on_store(
    store: FirstSeenStore, election_id: str, *, dry_run: bool
) -> FirstSeenResult | None:
    election = require_election_id(election_id)
    database_id = store._database_uuid_if_exists(election)  # noqa: SLF001
    if database_id is None:
        return None
    return build_first_seen(store, database_id, election, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build first-appearance voter counts in D1.")
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
            "first-seen build requires CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, "
            "and CLOUDFLARE_KV_NAMESPACE_ID"
        )
    account_id, api_token, namespace_id = config
    store = CloudflareEarlyVoteStore(
        account_id=account_id, api_token=api_token, kv_namespace_id=namespace_id
    )
    try:
        result = run_first_seen_build_on_store(store, args.election_id, dry_run=args.dry_run)
    finally:
        store.close()
    if result is None:
        sys.stdout.write("status=no_database\n")
        return 0
    sys.stdout.write(
        f"status={result.status}\n"
        f"distinct_voters={result.distinct_voters}\n"
        f"coordinates={result.coordinates}\n"
        f"unknown_method_voters={result.unknown_method_voters}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
