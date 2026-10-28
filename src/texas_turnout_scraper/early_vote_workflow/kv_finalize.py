from __future__ import annotations

from texas_turnout_scraper.early_vote_d1_precompute_backfill import should_finalize_kv_version


def workflow_should_bump_kv(
    *,
    published_digest: str,
    files_written: int,
    finalize_after_sync: bool,
    pending_before: bool,
    pending_after: bool,
    kv_stored_hash: str | None,
) -> bool:
    """KV bump rule from #48 / ``should_finalize_kv_version`` (Workflow ``finish``)."""
    return should_finalize_kv_version(
        published_digest=published_digest,
        files_written=files_written,
        finalize_after_sync=finalize_after_sync,
        pending_before=pending_before,
        pending_after=pending_after,
        kv_stored_hash=kv_stored_hash,
    )
