"""One-shot idempotent backfill of loader precomputed aggregate rows in D1."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import Literal

from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_d1_read import decode_chunk_payloads
from texas_turnout_scraper.early_vote_sync import (
    CloudflareEarlyVoteStore,
    EarlyVoteSyncError,
    _cloudflare_config_from_env,
)

logger = logging.getLogger(__name__)

_CHUNK_SQL = (
    "SELECT chunk_index, payload_b64 FROM early_vote_chunk "
    "WHERE source_election_id = ? AND file_id = ? ORDER BY chunk_index ASC"
)
_FILES_SQL = "SELECT file_id, sha256 FROM early_vote_file WHERE source_election_id = ?"
_AGG_EXISTS_SQL = (
    "SELECT file_id FROM early_vote_file_aggregate "
    "WHERE source_election_id = ? AND file_id = ? LIMIT 1"
)
_DAY_BY_FILE_SQL = (
    "SELECT ev_date FROM early_vote_day WHERE source_election_id = ? AND file_id = ? LIMIT 1"
)

BackfillStatus = Literal["dry_run", "wrote", "unchanged"]


@dataclass(frozen=True)
class PrecomputeBackfillResult:
    election_id: str
    files_considered: int
    files_written: int
    files_skipped: int
    status: BackfillStatus


def _chunks_b64(rows: list[dict[str, object]]) -> list[str]:
    ordered: list[tuple[int, str]] = []
    for row in rows:
        index = row.get("chunk_index")
        payload = row.get("payload_b64")
        if not isinstance(payload, str) or payload == "":
            continue
        if not isinstance(index, int):
            try:
                index = int(index)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
        ordered.append((index, payload))
    ordered.sort(key=lambda item: item[0])
    return [payload for _, payload in ordered]


def run_precompute_backfill_on_store(
    store: CloudflareEarlyVoteStore,
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
        considered += 1
        existing = store._query_rows(database_id, _AGG_EXISTS_SQL, [election_id, file_id])  # noqa: SLF001
        if existing:
            skipped += 1
            continue
        if dry_run:
            written += 1
            continue
        chunk_rows = store._query_rows(database_id, _CHUNK_SQL, [election_id, file_id])  # noqa: SLF001
        chunks = _chunks_b64(chunk_rows)
        if not chunks:
            skipped += 1
            continue
        decoded = decode_chunk_payloads(chunks)
        ev_date_iso: str | None = None
        day_rows = store._query_rows(database_id, _DAY_BY_FILE_SQL, [election_id, file_id])  # noqa: SLF001
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
    finally:
        store.close()
    sys.stdout.write(format_backfill_report(result) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
