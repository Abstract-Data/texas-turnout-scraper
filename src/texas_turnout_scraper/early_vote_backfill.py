"""Backfill all published Civix EV roster dates into one election D1 database.

KV ``ev:<election>:version`` bumps **once** after every day in the run succeeds
or is skipped as unchanged. The KV ``sha256`` value is a manifest digest over
sorted per-day content hashes (not one day's file hash).

Per-day idempotency uses D1 ``early_vote_day`` rows; re-running backfill does
not duplicate chunk rows for a day whose redacted payload hash is unchanged.

No voterfile joins (Tex. Elec. Code §18.066). Logs and reports are aggregates only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal

from texas_turnout_scraper.civix.evr import CivixClient
from texas_turnout_scraper.civix.ids import require_election_id
from texas_turnout_scraper.early_vote_sync import (
    EarlyVotePayloadKind,
    EarlyVoteStore,
    EarlyVoteSyncError,
    backfill_manifest_digest,
    iso_ev_date,
    pull_statewide_roster_file,
    sync_early_vote,
)

logger = logging.getLogger(__name__)

DEFAULT_NOV_2026_GENERAL_ID = "53815"
DEFAULT_NOV_2026_GENERAL_NAME = "2026 NOVEMBER GENERAL ELECTION"

SyncMode = Literal["single", "backfill", "latest"]


@dataclass(frozen=True)
class BackfillDayResult:
    """One EV date in a backfill run (counts only)."""

    ev_date_iso: str
    status: str
    sha256: str
    decoded_bytes: int


@dataclass(frozen=True)
class BackfillResult:
    """Outcome of a multi-day backfill."""

    election_id: str
    payload_kind: EarlyVotePayloadKind
    days: tuple[BackfillDayResult, ...]
    days_unchanged: int
    days_wrote: int
    days_dry_run: int
    manifest_sha256: str
    kv_version: str | None
    exit_code: int


def find_election_ev_dates(
    election_id: str,
    *,
    elections: list[Any] | None = None,
    client: CivixClient | None = None,
) -> tuple[str, date, tuple[date, ...]]:
    """Return election name, election day, and sorted published EV dates.

    When ``elections`` is omitted, ``client`` or a short-lived ``CivixClient``
    loads the live ``EVR_ELECTION`` index.
    """
    election_id = require_election_id(election_id)
    if elections is None:
        owned = client is None
        active = client or CivixClient()
        try:
            elections = active.list_elections()
        finally:
            if owned:
                active.close()
    match = None
    for item in elections:
        sid = getattr(item, "source_election_id", None) or str(getattr(item, "id", ""))
        if sid == election_id:
            match = item
            break
    if match is None:
        raise EarlyVoteSyncError(f"election_id {election_id} not found in Civix EVR index")
    name = str(getattr(match, "election_name", ""))
    election_day = getattr(match, "election_date", None)
    if not isinstance(election_day, date):
        raise EarlyVoteSyncError(f"election_id {election_id} missing election_date in index")
    ev_dates = []
    for entry in getattr(match, "early_voting_dates", ()) or ():
        day = getattr(entry, "date", None)
        if isinstance(day, date):
            ev_dates.append(day)
    if not ev_dates:
        raise EarlyVoteSyncError(f"election_id {election_id} has no early_voting_dates in index")
    return name, election_day, tuple(sorted(ev_dates))


def latest_published_ev_date(ev_dates: tuple[date, ...], *, today: date | None = None) -> date:
    """Latest EV date in the index that is not after ``today`` (UTC calendar)."""
    if not ev_dates:
        raise EarlyVoteSyncError("no EV dates to choose from")
    cutoff = today or datetime.now(tz=timezone.utc).date()
    eligible = [day for day in ev_dates if day <= cutoff]
    if not eligible:
        return ev_dates[-1]
    return max(eligible)


def run_backfill(
    election_id: str,
    store: EarlyVoteStore,
    ev_dates: tuple[date, ...],
    *,
    dry_run: bool = False,
    payload_kind: EarlyVotePayloadKind = "statewide_roster",
    http_backend: str = "httpx",
) -> BackfillResult:
    """Ingest every ``ev_dates`` entry; bump KV once at the end when not dry-run."""
    election_id = require_election_id(election_id)
    if payload_kind != "statewide_roster":
        raise EarlyVoteSyncError("backfill supports payload_kind=statewide_roster only")
    backend = "cloudscraper" if http_backend == "cloudscraper" else "httpx"
    day_results: list[BackfillDayResult] = []
    manifest_inputs: dict[str, str] = {}
    wrote = 0
    unchanged = 0
    dry_runs = 0

    for ev_day in ev_dates:
        iso = iso_ev_date(ev_day)
        response = pull_statewide_roster_file(
            election_id,
            ev_day,
            http_backend=backend,
        )
        result = sync_early_vote(
            election_id,
            response,
            store,
            dry_run=dry_run,
            payload_kind=payload_kind,
            ev_date_iso=iso,
            defer_kv_bump=True,
        )
        manifest_inputs[iso] = result.sha256
        day_results.append(
            BackfillDayResult(
                ev_date_iso=iso,
                status=result.status,
                sha256=result.sha256,
                decoded_bytes=result.decoded_bytes,
            )
        )
        if result.status == "wrote":
            wrote += 1
        elif result.status == "unchanged":
            unchanged += 1
        elif result.status == "dry_run":
            dry_runs += 1
        logger.info(
            "early-vote backfill day election_id=%s ev_date=%s status=%s decoded_bytes=%s",
            election_id,
            iso,
            result.status,
            result.decoded_bytes,
        )

    manifest = backfill_manifest_digest(manifest_inputs)
    kv_version: str | None = None
    if not dry_run and (wrote > 0 or unchanged > 0):
        kv_version = store.bump_version(election_id, manifest)
        logger.info(
            "early-vote backfill complete election_id=%s days=%s wrote=%s unchanged=%s version=%s",
            election_id,
            len(ev_dates),
            wrote,
            unchanged,
            kv_version,
        )
    elif dry_run:
        logger.info(
            "early-vote backfill dry-run election_id=%s days=%s manifest_sha256=%s",
            election_id,
            len(ev_dates),
            manifest,
        )

    return BackfillResult(
        election_id=election_id,
        payload_kind=payload_kind,
        days=tuple(day_results),
        days_unchanged=unchanged,
        days_wrote=wrote,
        days_dry_run=dry_runs,
        manifest_sha256=manifest,
        kv_version=kv_version,
        exit_code=0,
    )


def format_backfill_report(result: BackfillResult) -> str:
    """Operator report — day list shows ISO dates and statuses only."""
    lines = [
        f"election_id={result.election_id}",
        f"payload_kind={result.payload_kind}",
        "sync_mode=backfill",
        f"ev_days_total={len(result.days)}",
        f"days_wrote={result.days_wrote}",
        f"days_unchanged={result.days_unchanged}",
        f"days_dry_run={result.days_dry_run}",
        f"manifest_sha256={result.manifest_sha256}",
        f"exit_code={result.exit_code}",
    ]
    if result.kv_version is not None:
        lines.append(f"kv_version={result.kv_version}")
        lines.append("kv_bump=once_at_end")
    else:
        lines.append("kv_bump=deferred_or_dry_run")
    for day in result.days:
        lines.append(
            f"day ev_date={day.ev_date_iso} status={day.status} "
            f"decoded_bytes={day.decoded_bytes} sha256={day.sha256}"
        )
    return "\n".join(lines)
