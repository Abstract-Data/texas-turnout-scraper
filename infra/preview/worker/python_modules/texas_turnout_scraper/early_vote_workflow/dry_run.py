from __future__ import annotations

from dataclasses import dataclass

from texas_turnout_scraper.early_vote_roster import summarize_roster_csv
from texas_turnout_scraper.early_vote_sync import prepare_payload_bytes, sha256_decoded
from texas_turnout_scraper.early_vote_workflow.hash_gate import day_unchanged


@dataclass(frozen=True)
class DryRunDayReport:
    ev_date: str
    status: str
    sha256: str
    decoded_bytes: int
    est_rows: int
    unchanged: bool


def dry_run_day_from_bytes(
    ev_date: str,
    raw_csv: bytes,
    *,
    stored_sha256: str | None,
) -> DryRunDayReport:
    """Hash and count a synthetic roster payload without any store writes."""
    redacted, summary = prepare_payload_bytes(raw_csv, "statewide_roster")
    digest = sha256_decoded(redacted)
    unchanged = day_unchanged(stored_sha256, digest)
    est_rows = summary.est_rows if summary is not None else 0
    if est_rows == 0:
        status = "skipped_empty"
    elif unchanged:
        status = "unchanged"
    else:
        status = "changed"
    _ = summarize_roster_csv(redacted)  # validates shape; result not logged
    return DryRunDayReport(
        ev_date=ev_date,
        status=status,
        sha256=digest,
        decoded_bytes=len(redacted),
        est_rows=est_rows,
        unchanged=unchanged,
    )
