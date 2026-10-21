"""One-shot idempotent backfill of loader precomputed aggregate rows in D1."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import Literal, Protocol

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_d1_precompute_store import (
    aggregate_body_from_decoded,
    precompute_row_count,
)
from texas_turnout_scraper.early_vote_d1_read import decode_chunk_payloads
from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    EarlyVoteSyncError,
    _cloudflare_config_from_env,
)

logger = logging.getLogger(__name__)

_CHUNK_PAGE_SIZE = 50

_CHUNK_INDICES_SQL = (
    "SELECT chunk_index FROM early_vote_chunk "
    "WHERE source_election_id = ? AND file_id = ? ORDER BY chunk_index ASC "
    "LIMIT ? OFFSET ?"
)
_CHUNK_PAYLOAD_SQL = (
    "SELECT payload_b64 FROM early_vote_chunk "
    "WHERE source_election_id = ? AND file_id = ? AND chunk_index = ?"
)
_FILES_SQL = (
    "SELECT file_id, sha256, chunk_count FROM early_vote_file WHERE source_election_id = ?"
)
_FILE_AGG_EXISTS_SQL = (
    "SELECT file_id FROM early_vote_file_aggregate "
    "WHERE source_election_id = ? AND file_id = ? LIMIT 1"
)
_COUNTY_COUNT_SQL = (
    "SELECT COUNT(*) AS n FROM early_vote_county_aggregate "
    "WHERE source_election_id = ? AND file_id = ?"
)
_DAY_BY_FILE_SQL = (
    "SELECT ev_date FROM early_vote_day "
    "WHERE source_election_id = ? AND file_id = ? ORDER BY ev_date ASC LIMIT 1"
)

BackfillStatus = Literal["dry_run", "wrote", "unchanged"]


class PrecomputeBackfillStore(Protocol):
    def _database_uuid_if_exists(self, election_id: str) -> str | None: ...  # noqa: SLF001

    def _ensure_schema(self, database_id: str) -> None: ...  # noqa: SLF001

    def _query_rows(
        self,
        database_id: str,
        sql: str,
        params: list[str],
    ) -> list[dict[str, object]]: ...  # noqa: SLF001

    def _write_precomputed_aggregates(  # noqa: SLF001
        self,
        database_id: str,
        election_id: str,
        file_id: str,
        decoded: bytes,
        *,
        ev_date_iso: str | None,
    ) -> None: ...

    def bump_version(self, election_id: str, digest: str) -> str: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class PrecomputeBackfillResult:
    election_id: str
    files_considered: int
    files_written: int
    files_skipped: int
    status: BackfillStatus


def _coerce_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _load_chunks_b64_paged(
    store: PrecomputeBackfillStore,
    database_id: str,
    election_id: str,
    file_id: str,
    *,
    expected_count: int,
) -> list[str] | None:
    """Load every chunk payload in ``chunk_index`` order; refuse partial sets."""
    indices: list[int] = []
    offset = 0
    while True:
        page = store._query_rows(  # noqa: SLF001
            database_id,
            _CHUNK_INDICES_SQL,
            [election_id, file_id, str(_CHUNK_PAGE_SIZE), str(offset)],
        )
        if not page:
            break
        for row in page:
            index = _coerce_int(row.get("chunk_index"))
            if index is not None:
                indices.append(index)
        if len(page) < _CHUNK_PAGE_SIZE:
            break
        offset += _CHUNK_PAGE_SIZE
    if len(indices) != expected_count:
        return None
    indices.sort()
    if indices != list(range(expected_count)):
        return None
    payloads: list[str] = []
    for index in indices:
        rows = store._query_rows(  # noqa: SLF001
            database_id,
            _CHUNK_PAYLOAD_SQL,
            [election_id, file_id, str(index)],
        )
        if not rows:
            return None
        payload = rows[0].get("payload_b64")
        if not isinstance(payload, str) or payload == "":
            return None
        payloads.append(payload)
    return payloads


def _expected_county_rows(body: dict[str, object]) -> int:
    by_county = body.get("by_county")
    if not isinstance(by_county, list):
        return 0
    return sum(
        1
        for row in by_county
        if isinstance(row, dict)
        and isinstance(row.get("county"), str)
        and str(row.get("county")).strip()
    )


def _precompute_complete(
    store: PrecomputeBackfillStore,
    database_id: str,
    election_id: str,
    file_id: str,
    *,
    expected_county_rows: int,
) -> bool:
    file_rows = store._query_rows(  # noqa: SLF001
        database_id,
        _FILE_AGG_EXISTS_SQL,
        [election_id, file_id],
    )
    if not file_rows:
        return False
    count_rows = store._query_rows(  # noqa: SLF001
        database_id,
        _COUNTY_COUNT_SQL,
        [election_id, file_id],
    )
    if not count_rows:
        return False
    actual = _coerce_int(count_rows[0].get("n"))
    return actual is not None and actual == expected_county_rows


def run_precompute_backfill_on_store(
    store: PrecomputeBackfillStore,
    election_id: str,
    *,
    dry_run: bool,
) -> PrecomputeBackfillResult:
    """Write aggregate rows for every complete file missing precompute data."""
    election_id = require_election_id(election_id)
    database_id = store._database_uuid_if_exists(election_id)  # noqa: SLF001
    if database_id is None:
        return PrecomputeBackfillResult(
            election_id=election_id,
            files_considered=0,
            files_written=0,
            files_skipped=0,
            status="unchanged",
        )
    store._ensure_schema(database_id)  # noqa: SLF001
    files = store._query_rows(database_id, _FILES_SQL, [election_id])  # noqa: SLF001
    considered = 0
    written = 0
    skipped = 0
    for row in files:
        file_id = row.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            continue
        chunk_count = _coerce_int(row.get("chunk_count"))
        if chunk_count is None or chunk_count < 1:
            skipped += 1
            continue
        considered += 1
        chunks = _load_chunks_b64_paged(
            store,
            database_id,
            election_id,
            file_id,
            expected_count=chunk_count,
        )
        if chunks is None:
            skipped += 1
            continue
        decoded = decode_chunk_payloads(chunks)
        try:
            body = aggregate_body_from_decoded(decoded, source_election_id=election_id)
        except ValueError:
            skipped += 1
            continue
        expected_counties = _expected_county_rows(body)
        if _precompute_complete(
            store,
            database_id,
            election_id,
            file_id,
            expected_county_rows=expected_counties,
        ):
            skipped += 1
            continue
        if dry_run:
            written += 1
            continue
        ev_date_iso: str | None = None
        day_rows = store._query_rows(  # noqa: SLF001
            database_id,
            _DAY_BY_FILE_SQL,
            [election_id, file_id],
        )
        if day_rows:
            ev = day_rows[0].get("ev_date")
            if isinstance(ev, str) and ev:
                ev_date_iso = ev
        store._write_precomputed_aggregates(  # noqa: SLF001
            database_id,
            election_id,
            file_id,
            decoded,
            ev_date_iso=ev_date_iso,
        )
        written += 1
        logger.info(
            "precompute backfill wrote election_id=%s file_id=%s rows=%s",
            election_id,
            file_id,
            precompute_row_count(body),
        )
    status: BackfillStatus = "dry_run" if dry_run else ("wrote" if written else "unchanged")
    logger.info(
        "precompute backfill election_id=%s considered=%s written=%s skipped=%s status=%s",
        election_id,
        considered,
        written,
        skipped,
        status,
    )
    return PrecomputeBackfillResult(
        election_id=election_id,
        files_considered=considered,
        files_written=written,
        files_skipped=skipped,
        status=status,
    )


def format_backfill_report(result: PrecomputeBackfillResult) -> str:
    return (
        f"election_id={result.election_id}\n"
        f"files_considered={result.files_considered}\n"
        f"files_written={result.files_written}\n"
        f"files_skipped={result.files_skipped}\n"
        f"status={result.status}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill early_vote_*_aggregate rows for files already in D1.",
    )
    parser.add_argument("--election-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--finalize-kv-digest",
        default="",
        help="After a successful apply, bump KV version with this file content digest",
    )
    args = parser.parse_args(argv)
    if args.dry_run == args.apply:
        parser.error("pass exactly one of --dry-run or --apply")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = _cloudflare_config_from_env()
    if config is None:
        raise EarlyVoteSyncError(
            "apply requires CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN, "
            "and CLOUDFLARE_KV_NAMESPACE_ID"
        )
    account_id, api_token, namespace_id = config
    store = CloudflareEarlyVoteStore(
        account_id=account_id,
        api_token=api_token,
        kv_namespace_id=namespace_id,
    )
    try:
        result = run_precompute_backfill_on_store(
            store,
            args.election_id,
            dry_run=args.dry_run,
        )
        if args.apply and args.finalize_kv_digest.strip():
            version = store.bump_version(args.election_id, args.finalize_kv_digest.strip())
            logger.info(
                "precompute backfill finalized KV election_id=%s version=%s",
                args.election_id,
                version,
            )
    finally:
        store.close()
    sys.stdout.write(format_backfill_report(result) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
